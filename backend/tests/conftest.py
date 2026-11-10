import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TEST_DIR = Path(tempfile.mkdtemp(prefix="spinco_test_"))
if os.environ.get("SPINCO_TEST_DATABASE_URL_FILE"):
    # Run the suite against a DISPOSABLE PostgreSQL database (already initialised with tools/init_central_staging_db.py --database-url-file). Never point this at a
    # database that holds anything: every test drops and recreates the tables.
    os.environ.pop("SPINCO_DB_PATH", None)
    os.environ["SPINCO_DATABASE_URL_FILE"] = os.environ["SPINCO_TEST_DATABASE_URL_FILE"]
else:
    os.environ["SPINCO_DB_PATH"] = str(_TEST_DIR / "test.db")
os.environ["SPINCO_STORAGE_ROOT"] = str(_TEST_DIR / "storage")
# The app copies accepted uploads to a TA Backend on 127.0.0.1:4000 by default. Tests upload synthetic AND reference documents
# through the API, so they must NEVER reach a real TA Backend that happens to be running on the developer's machine.
# (tests/test_ta_backend_forward.py opts back in against its own throw-away local server.)
os.environ["TA_BACKEND_URL"] = "off"

# The `client` fixture reseeds (hashes a password) and logs in (verifies a
# password) on every single test. Argon2's production cost parameters are
# intentionally expensive, which is correct for real logins but turns into
# minutes of pure overhead across a large test suite - use the algorithm's
# minimum viable cost here instead. Never do this outside the test process
# (app/services/auth.py defaults to full strength unless these are set).
os.environ.setdefault("SPINCO_ARGON2_TIME_COST", "1")
os.environ.setdefault("SPINCO_ARGON2_MEMORY_COST", "8")
os.environ.setdefault("SPINCO_ARGON2_PARALLELISM", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_all  # noqa: E402

if os.environ.get("SPINCO_TEST_DATABASE_URL_FILE"):
    # PostgreSQL test mode: many tests call Base.metadata.drop_all(bind=engine) to start clean. On a server schema that is impossible (bills <-> expenses reference each other, so
    # SQLAlchemy cannot order the DROPs) and unwanted (the strict schema must stay as the initialiser made it), so "drop all" means "empty every table" here.
    from sqlalchemy import text as _text

    def _truncate_instead_of_drop(bind=None, tables=None, checkfirst=True):
        with (bind or engine).begin() as connection:
            names = ", ".join('"%s"' % name for name in Base.metadata.tables)
            connection.execute(_text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))

    Base.metadata.drop_all = _truncate_instead_of_drop

    def _heal_postgres_schema():
        """A test that deliberately breaks the shared schema (drops a table, builds a legacy table shape, adds/drops a column to prove the strict check refuses it) must not leave it
        broken for the next test: when the strict check reports drift, rebuild the schema exactly as tools/init_central_staging_db.py does."""
        from alembic import command
        from alembic.config import Config

        from app.services import central_storage

        try:
            drifted = bool(central_storage.schema_problems())
        except Exception:  # noqa: BLE001
            drifted = True
        if not drifted:
            return
        with engine.begin() as connection:
            connection.execute(_text("DROP SCHEMA public CASCADE"))
            connection.execute(_text("CREATE SCHEMA public"))
        Base.metadata.create_all(bind=engine)
        alembic_config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
        alembic_config.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "migrations"))
        command.stamp(alembic_config, "head")

    @pytest.fixture(autouse=True)
    def _postgres_schema_is_healthy():
        _heal_postgres_schema()
        yield

    def pytest_collection_modifyitems(config, items):
        """PostgreSQL mode only: skip the tests that are specific to SQLite (tests/postgres_skips.py lists each with its reason). Nothing is skipped on SQLite."""
        import shutil

        from postgres_skips import NEEDS_PG_DUMP, REASONS

        for item in items:
            reason = REASONS.get(item.nodeid.replace("\\", "/"))
            if reason and not (item.nodeid in NEEDS_PG_DUMP and shutil.which("pg_dump")):
                item.add_marker(pytest.mark.skip(reason="PostgreSQL mode: " + reason))

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

ADMIN_EMAIL = "intern@spincotech.com"
ADMIN_PASSWORD = "ChangeMe123!"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_dir():
    yield
    shutil.rmtree(_TEST_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _hosted_providers_off_and_no_live_http(monkeypatch):
    """Every test starts with all hosted-provider configuration cleared (so a
    developer's real key/flag in their shell can never leak into a test) and
    with httpx.post - the only call the provider modules use - replaced by a
    function that fails the test. A test that exercises a provider path
    mocks httpx.post itself (monkeypatch.setattr runs after this fixture and
    wins), so a real outbound request from the test suite is impossible."""
    import httpx

    monkeypatch.setenv("TA_BACKEND_URL", "off")  # a test that wants the TA Backend copy opts in with its own setenv (runs after this)
    for name in list(os.environ):
        if name.startswith(("SPINCO_NVIDIA_", "SPINCO_DEEPSEEK_")):
            monkeypatch.delenv(name, raising=False)

    def _no_live_http(*args, **kwargs):
        raise AssertionError("a test attempted a real httpx.post call; mock it explicitly")

    monkeypatch.setattr(httpx, "post", _no_live_http)


@pytest.fixture(autouse=True)
def _drive_tests_run_as_dev(request, monkeypatch):
    """The Drive destination guard's opt-out (a harness or fake building a DriveClient with `unguarded=True`) exists only when SPINCO_ENVIRONMENT is
    explicitly dev or staging. The Drive test modules say so here; a test of the refusal sets or clears the variable itself (it runs after this)."""
    if request.module.__name__.rsplit(".", 1)[-1].startswith("test_drive"):
        monkeypatch.setenv("SPINCO_ENVIRONMENT", "dev")


@pytest.fixture(scope="session")
def _shared_test_client():
    """One TestClient - and therefore one ASGI lifespan/anyio blocking-portal
    thread - for the entire test session, reused by every test.

    Originally each test entered `with TestClient(app) as c:` on its own, which
    worked fine pre-Phase-10. Once the `client` fixture below started also
    logging in (an extra request) on every one of ~250 tests, the suite began
    intermittently deadlocking partway through (confirmed via CPU sampling: the
    hung process burns 0% CPU, i.e. genuinely blocked, not just slow) - almost
    certainly portal creation/teardown churn on Windows, not app logic. Reusing
    one portal for the whole session removes that churn entirely.
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client(_shared_test_client):
    """Fresh schema + fresh seed per test - Phase 2 has no migration framework
    yet, so tests that need isolation just recreate the tables. Since the
    ASGI lifespan (and therefore its startup-time seed_all()) now only runs
    once per session (see _shared_test_client above), this fixture calls
    seed_all() itself after every reset instead of relying on a fresh
    lifespan cycle to do it.

    Phase 10 added session-cookie auth + CSRF to every previously-public
    route, so this fixture logs in as the seeded ADMIN by default and
    auto-attaches the X-CSRF-Token header on mutating requests - this keeps
    every pre-Phase-10 business-logic test working unmodified. Tests that
    specifically exercise unauthenticated/CSRF-failure behavior (test_auth.py,
    test_rbac.py) pass their own explicit X-CSRF-Token header (which always
    wins over the auto-injected one) or clear client.cookies to start from a
    logged-out state.
    """
    from app.services.hardening import login_throttle

    login_throttle.reset()  # the throttle is process-wide state: one test's failed sign-ins must never lock out the next test's admin
    if os.environ.get("SPINCO_TEST_DATABASE_URL_FILE"):
        # PostgreSQL: the schema was created once by tools/init_central_staging_db.py. bills <-> expenses reference each other, so drop_all cannot order the DROPs
        # (SQLAlchemy raises CircularDependencyError) - and a server schema is never dropped anyway: each test starts from empty tables instead.
        from sqlalchemy import text

        from app.services import central_storage

        try:
            drifted = bool(central_storage.schema_problems())
        except Exception:  # noqa: BLE001
            drifted = True
        if drifted:
            # A test that deliberately breaks the schema (drops a table, adds/drops a column to prove the strict check refuses it) left the shared database altered: rebuild it exactly as
            # tools/init_central_staging_db.py does, so the next test starts from the real schema.
            from alembic import command
            from alembic.config import Config

            with engine.begin() as connection:
                connection.execute(text("DROP SCHEMA public CASCADE"))
                connection.execute(text("CREATE SCHEMA public"))
            Base.metadata.create_all(bind=engine)
            alembic_config = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
            alembic_config.set_main_option("script_location", str(REPO_ROOT / "backend" / "migrations"))
            command.stamp(alembic_config, "head")
        with engine.begin() as connection:
            tables = ", ".join('"%s"' % name for name in Base.metadata.tables)
            connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    else:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
    seed_all()

    c = _shared_test_client
    c.cookies.clear()
    login_resp = c.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    csrf_token = login_resp.json()["csrf_token"]
    c.csrf_token = csrf_token

    if not getattr(c, "_csrf_patched", False):
        _orig_request = c.request

        def _request_with_csrf(method, url, **kwargs):
            if method.upper() not in _SAFE_METHODS:
                headers = kwargs.get("headers") or {}
                if not any(k.lower() == "x-csrf-token" for k in headers):
                    headers = {**headers, "X-CSRF-Token": c.csrf_token}
                    kwargs["headers"] = headers
            return _orig_request(method, url, **kwargs)

        c.request = _request_with_csrf
        c._csrf_patched = True

    yield c


@pytest.fixture()
def sample_pdf_path():
    return REPO_ROOT / "ER-39832.pdf"


@pytest.fixture()
def sample_trip_pdf_path():
    return REPO_ROOT / "TRIP-24935_Trip_To_Indore_Pithampur.pdf"
