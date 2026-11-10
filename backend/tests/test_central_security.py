"""Central backend security: start-up refusals for a network-reachable backend, strict schema mode (the backend never creates or alters tables for central storage),
generic user-facing errors, no secrets in responses or logs, and the central gate in front of Drive."""

import json
import logging

import pytest
from sqlalchemy import select, text

from app import main as app_main
from app.database import Base, SessionLocal, engine
from app.models.expense_report import ExpenseReport
from app.models.user import User
from app.services import central_storage, startup_checks
from app.services.auth import hash_password
from app.services.drive import schema as drive_schema

from drive_testkit import pdf_bytes
from test_central_storage import count, upload

GOOD = {"SPINCO_BIND_HOST": "0.0.0.0", "SPINCO_ADMIN_INITIAL_PASSWORD": "A-Strong-Initial-Pass-9", "SPINCO_COOKIE_SECURE": "true", "SPINCO_ALLOWED_HOSTS": "ta-central.example.test"}


def env(**overrides):
    merged = {**GOOD, **overrides}
    return {k: v for k, v in merged.items() if v is not None}


# ------------------------------------------------------------------------------------------------------------------------------- start-up checks


def test_a_loopback_backend_has_no_extra_requirements():
    for host in ("127.0.0.1", "localhost", "::1", ""):
        assert startup_checks.network_exposed({"SPINCO_BIND_HOST": host}) is False
    assert startup_checks.network_exposed({}) is False  # the default is loopback
    startup_checks.run_checks(None, {})  # the development default password is still fine on a local desktop
    assert startup_checks.warnings({}) == []


@pytest.mark.parametrize("password", [None, "", "ChangeMe123!", "short-1A!"])
def test_a_network_backend_refuses_a_missing_default_or_weak_initial_admin_password(password):
    with pytest.raises(startup_checks.StartupSecurityError, match="SPINCO_ADMIN_INITIAL_PASSWORD"):
        startup_checks.run_checks(None, env(SPINCO_ADMIN_INITIAL_PASSWORD=password))


def test_a_network_backend_refuses_plain_http_unless_it_is_acknowledged_for_development():
    with pytest.raises(startup_checks.StartupSecurityError, match="HTTPS"):
        startup_checks.run_checks(None, env(SPINCO_COOKIE_SECURE=None))
    acknowledged = env(SPINCO_COOKIE_SECURE=None, SPINCO_ALLOW_INSECURE_HTTP="true")
    startup_checks.run_checks(None, acknowledged)  # allowed for development / synthetic documents ...
    assert any("INSECURE" in w and "synthetic" in w for w in startup_checks.warnings(acknowledged))  # ... but administrators are told so
    assert not any("INSECURE" in w for w in startup_checks.warnings(env()))  # HTTPS + secure cookies: no such warning


def test_host_name_settings_are_checked():
    assert any("SPINCO_ALLOWED_HOSTS is not set" in w for w in startup_checks.warnings(env(SPINCO_ALLOWED_HOSTS=None)))
    assert any("IP address" in w for w in startup_checks.warnings(env(SPINCO_ALLOWED_HOSTS="10.1.2.3")))
    assert startup_checks.warnings(env()) == []
    assert startup_checks.allowed_hosts(env(SPINCO_ALLOWED_HOSTS="a.example.test, b.example.test")) == ["a.example.test", "b.example.test"]


def test_an_existing_account_with_the_development_default_password_stops_a_network_backend(client):
    with SessionLocal() as db:
        with pytest.raises(startup_checks.StartupSecurityError, match="development default password"):
            startup_checks.run_checks(db, env())  # the seeded administrator of the test fixture still has ChangeMe123!
        admin = db.execute(select(User).where(User.email == "intern@spincotech.com")).scalar_one()
        admin.password_hash = hash_password("A-Different-Strong-Pass-7")
        db.commit()
        startup_checks.run_checks(db, env())  # changed: the backend may start


def test_no_check_ever_puts_a_password_in_its_message(caplog):
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(startup_checks.StartupSecurityError) as error:
            startup_checks.run_checks(None, env(SPINCO_ADMIN_INITIAL_PASSWORD="ChangeMe123!"))
    assert "ChangeMe123!" not in str(error.value) and "ChangeMe123!" not in caplog.text


# ---------------------------------------------------------------------------------------------------------------------------- strict schema mode


def test_strict_mode_accepts_a_ready_database_and_creates_nothing(client, monkeypatch):
    monkeypatch.setenv("SPINCO_CENTRAL_STORAGE_MODE", "serve")
    monkeypatch.setenv("SPINCO_JOB_WORKER", "off")
    monkeypatch.setattr(Base.metadata, "create_all", lambda *a, **k: pytest.fail("strict mode must never call create_all"))
    monkeypatch.setattr(app_main, "run_light_migrations", lambda *a, **k: pytest.fail("strict mode must never run the light migrations"))
    app_main.on_startup()  # raises SchemaNotReady if the (create_all-built) test database were not exactly right


def test_strict_mode_refuses_a_database_without_the_central_tables_and_does_not_create_them(client, monkeypatch):
    monkeypatch.setenv("SPINCO_CENTRAL_STORAGE_MODE", "shadow")
    monkeypatch.setenv("SPINCO_JOB_WORKER", "off")
    with engine.begin() as connection:
        connection.execute(text("drop table upload_attempts"))
        connection.execute(text("drop table processing_jobs"))
    try:
        with pytest.raises(app_main.SchemaNotReady, match="central tables: absent"):
            app_main.on_startup()
        with engine.connect() as connection:
            names = {r[0] for r in connection.execute(text("select name from sqlite_master where type='table'"))}
        assert not {"upload_attempts", "processing_jobs"} & names  # refused, not repaired
    finally:
        Base.metadata.create_all(bind=engine)  # the next test's fixture rebuilds everything anyway; leave the shared database consistent
        drive_schema.reset_schema_cache()


def test_strict_mode_can_be_requested_without_central_storage(client, monkeypatch):
    monkeypatch.setenv("SPINCO_SCHEMA_MODE", "strict")
    monkeypatch.setenv("SPINCO_JOB_WORKER", "off")
    with engine.begin() as connection:
        connection.execute(text("drop table processing_jobs"))
    try:
        with pytest.raises(app_main.SchemaNotReady):
            app_main.on_startup()
    finally:
        Base.metadata.create_all(bind=engine)
        drive_schema.reset_schema_cache()


def test_the_default_mode_still_upgrades_the_schema_as_before(client, monkeypatch):
    monkeypatch.delenv("SPINCO_CENTRAL_STORAGE_MODE", raising=False)
    monkeypatch.delenv("SPINCO_SCHEMA_MODE", raising=False)
    with engine.begin() as connection:
        connection.execute(text("drop table processing_jobs"))
    app_main.on_startup()  # off mode: the legacy behaviour (create_all adds missing tables), unchanged
    with engine.connect() as connection:
        assert connection.execute(text("select count(*) from sqlite_master where name='processing_jobs'")).scalar_one() == 1


# ---------------------------------------------------------------------------------------------------------- database contention (measured finding)


def test_a_locked_database_is_a_retryable_503_not_a_500(client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.services import permissions

    def locked(db, token):
        raise OperationalError("UPDATE auth_sessions SET last_seen_at=?", {}, Exception("database is locked"))

    monkeypatch.setattr(permissions, "get_valid_session", locked)
    response = client.get("/api/uploads")
    assert response.status_code == 503 and response.headers["retry-after"] == "2" and "busy" in response.json()["detail"] and "UPDATE" not in response.text  # no SQL in the answer


def test_any_other_database_error_is_still_an_error_not_swallowed(client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.services import permissions

    def broken(db, token):
        raise OperationalError("SELECT 1", {}, Exception("disk I/O error"))

    monkeypatch.setattr(permissions, "get_valid_session", broken)
    with pytest.raises(OperationalError):  # the test client re-raises an unhandled server error: it was NOT turned into a friendly 503
        client.get("/api/uploads")


def _journal_and_timeout(tmp_path, mode):
    import subprocess
    import sys

    env = {k: v for k, v in __import__("os").environ.items() if k != "SPINCO_CENTRAL_STORAGE_MODE"}
    env.update({"SPINCO_DB_PATH": str(tmp_path / f"tuning-{mode or 'off'}.db"), "PYTHONDONTWRITEBYTECODE": "1"})
    if mode:
        env["SPINCO_CENTRAL_STORAGE_MODE"] = mode
    code = ("from app.database import engine\nfrom sqlalchemy import text\n"
            "with engine.connect() as c:\n    print(c.execute(text('PRAGMA journal_mode')).scalar(), c.execute(text('PRAGMA busy_timeout')).scalar())\n")
    backend = __import__("pathlib").Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=backend, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-500:]
    return result.stdout.split()


def test_sqlite_is_tuned_for_concurrent_writers_only_in_the_central_modes(tmp_path):
    assert _journal_and_timeout(tmp_path, "serve") == ["wal", "30000"]
    assert _journal_and_timeout(tmp_path, "shadow") == ["wal", "30000"]
    journal, timeout = _journal_and_timeout(tmp_path, None)
    assert journal == "delete" and int(timeout) == 5000  # the default mode (desktop app, existing databases) is exactly as before


# --------------------------------------------------------------------------------------------------- the gate in front of Drive, generic errors


def test_serve_mode_without_a_configured_drive_is_refused_generically_and_saves_nothing(client, monkeypatch):
    for name in ("SPINCO_GDRIVE_ENABLED", "SPINCO_GDRIVE_ROOT_FOLDER_ID", "SPINCO_GDRIVE_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SPINCO_CENTRAL_STORAGE_MODE", "serve")
    result = upload(client, pdf_bytes("unconfigured"))
    assert result["success"] is False and result["result"] == "FAILED" and result["retryable"] is False
    assert result["error_message"] == central_storage.NOT_CONFIGURED and "SPINCO_" not in result["error_message"]  # the setting names are for the log, not for users
    assert count(ExpenseReport) == 0  # nothing was saved locally as a stand-in for the missing central copy


def test_serve_mode_refuses_when_the_central_schema_is_not_ready(client, monkeypatch):
    monkeypatch.setenv("SPINCO_CENTRAL_STORAGE_MODE", "serve")
    with engine.begin() as connection:
        connection.execute(text("drop table processing_jobs"))
    drive_schema.reset_schema_cache()
    try:
        with pytest.raises(central_storage.CentralUnavailable) as refused:
            central_storage.manager()
        assert refused.value.status == 503 and refused.value.detail == central_storage.NOT_CONFIGURED and refused.value.retryable is False
    finally:
        Base.metadata.create_all(bind=engine)
        drive_schema.reset_schema_cache()


def test_map_drive_error_never_returns_the_underlying_text():
    from app.services.drive.client import DriveError, DriveIntegrityError, DriveNotFound

    for exc, status in ((DriveIntegrityError("md5 mismatch for gdrive:SECRETID"), 502), (DriveNotFound("file SECRETID"), 404), (DriveError("boom SECRETID", retryable=True), 503)):
        mapped = central_storage.map_drive_error(exc)
        assert mapped.status == status and "SECRETID" not in mapped.detail


def test_no_response_or_log_line_contains_drive_identifiers_or_local_paths(client, monkeypatch, caplog):
    for name in ("SPINCO_GDRIVE_ENABLED", "SPINCO_GDRIVE_ROOT_FOLDER_ID", "SPINCO_GDRIVE_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SPINCO_CENTRAL_STORAGE_MODE", "serve")
    with caplog.at_level(logging.DEBUG):
        result = upload(client, pdf_bytes("leak-check"))
        listing = client.get("/api/uploads").text + client.get("/api/uploads/attempts").text + client.get("/api/central/status").text
    blob = json.dumps(result) + listing + caplog.text
    assert "gdrive:" not in blob and "spinco_test_" not in blob and "ChangeMe123" not in blob and "SPINCO_GDRIVE" not in json.dumps(result)
