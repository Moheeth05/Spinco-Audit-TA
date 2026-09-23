import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TEST_DIR = Path(tempfile.mkdtemp(prefix="spinco_test_"))
os.environ["SPINCO_DB_PATH"] = str(_TEST_DIR / "test.db")
os.environ["SPINCO_STORAGE_ROOT"] = str(_TEST_DIR / "storage")

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

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

ADMIN_EMAIL = "intern@spincotech.com"
ADMIN_PASSWORD = "ChangeMe123!"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_dir():
    yield
    shutil.rmtree(_TEST_DIR, ignore_errors=True)


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
