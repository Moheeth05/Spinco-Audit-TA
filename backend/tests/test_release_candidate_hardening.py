"""Release-candidate hardening: sign-in throttling, health / readiness / environment, upload limits, CORS configuration, log redaction, and the central copy of the
Excel export. FAKE-DRIVE tests of OUR logic where Drive is involved (tests/fake_drive.py) - not evidence about Google's real service."""

import asyncio
import logging
from io import BytesIO

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.logging_config import configure_logging, redact
from app.models.logs import AuditLog
from app.services import hardening
from app.services.hardening import CorsConfigError, LoginThrottle

from test_central_storage import drive, serve, upload  # noqa: F401  (fixtures + helper of the central-storage tests)
from drive_testkit import pdf_bytes

from conftest import ADMIN_EMAIL as ADMIN  # noqa: E402


# ------------------------------------------------------------------------------------------------------------------------- throttle (unit, fake clock)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


ENV = {"SPINCO_LOGIN_MAX_FAILURES": "3", "SPINCO_LOGIN_WINDOW_SECONDS": "60", "SPINCO_LOGIN_LOCKOUT_SECONDS": "120"}


def test_throttle_locks_after_the_configured_failures_and_releases_after_the_lockout():
    clock = Clock()
    t = LoginThrottle(clock)
    for _ in range(2):
        t.record_failure("a@x.com", "1.1.1.1", ENV)
    assert t.retry_after("a@x.com", "1.1.1.1") is None
    t.record_failure("a@x.com", "1.1.1.1", ENV)
    wait = t.retry_after("a@x.com", "1.1.1.1")
    assert wait is not None and 1 <= wait <= 120
    clock.now += 121
    assert t.retry_after("a@x.com", "1.1.1.1") is None


def test_throttle_window_slides_so_old_failures_do_not_count():
    clock = Clock()
    t = LoginThrottle(clock)
    t.record_failure("a@x.com", "1.1.1.1", ENV)
    t.record_failure("a@x.com", "1.1.1.1", ENV)
    clock.now += 61  # both are outside the 60 s window now
    t.record_failure("a@x.com", "1.1.1.1", ENV)
    assert t.retry_after("a@x.com", "1.1.1.1") is None


def test_throttle_treats_unknown_accounts_exactly_like_known_ones_and_isolates_pairs():
    t = LoginThrottle(Clock())
    for _ in range(3):
        t.record_failure("nobody@x.com", "1.1.1.1", ENV)
    assert t.retry_after("nobody@x.com", "1.1.1.1") is not None
    # a stranger at another address does not lock the real user out of their own address
    assert t.retry_after("nobody@x.com", "2.2.2.2") is None
    assert t.retry_after("someone.else@x.com", "1.1.1.1") is None


def test_throttle_catches_a_guesser_who_rotates_addresses_and_one_spraying_many_accounts():
    t = LoginThrottle(Clock())
    for i in range(18):  # 3 * 6: the account-level limit
        t.record_failure("victim@x.com", f"10.0.0.{i}", ENV)
    assert t.retry_after("victim@x.com", "10.0.0.99") is not None
    t2 = LoginThrottle(Clock())
    for i in range(30):  # 3 * 10: the address-level limit
        t2.record_failure(f"user{i}@x.com", "9.9.9.9", ENV)
    assert t2.retry_after("brand.new@x.com", "9.9.9.9") is not None


def test_a_correct_sign_in_clears_the_pair_history():
    t = LoginThrottle(Clock())
    t.record_failure("a@x.com", "1.1.1.1", ENV)
    t.record_failure("a@x.com", "1.1.1.1", ENV)
    t.record_success("a@x.com", "1.1.1.1")
    t.record_failure("a@x.com", "1.1.1.1", ENV)
    assert t.retry_after("a@x.com", "1.1.1.1") is None


def test_throttle_settings_are_bounded_and_garbage_falls_back_to_defaults():
    assert LoginThrottle.settings({}) == (5, 300, 300)
    assert LoginThrottle.settings({"SPINCO_LOGIN_MAX_FAILURES": "abc"})[0] == 5
    assert LoginThrottle.settings({"SPINCO_LOGIN_MAX_FAILURES": "0"})[0] == 1
    assert LoginThrottle.settings({"SPINCO_LOGIN_LOCKOUT_SECONDS": "1"})[2] == 5


# ------------------------------------------------------------------------------------------------------------------------- throttle (HTTP)


def _login(client, email, password):
    client.cookies.clear()
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_repeated_wrong_passwords_are_answered_with_429_and_even_the_right_password_is_refused_during_the_lockout(client, monkeypatch):
    monkeypatch.setenv("SPINCO_LOGIN_MAX_FAILURES", "3")
    for _ in range(3):
        assert _login(client, ADMIN, "wrong-password-1").status_code == 401
    refused = _login(client, ADMIN, "wrong-password-1")
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1
    assert "Too many failed sign-in attempts" in refused.json()["detail"]
    from conftest import ADMIN_PASSWORD

    assert _login(client, ADMIN, ADMIN_PASSWORD).status_code == 429  # locked, whoever asks
    hardening.login_throttle.reset()
    assert _login(client, ADMIN, ADMIN_PASSWORD).status_code == 200  # (the lock-out ends - here simulated by a reset)


def test_an_unknown_account_is_throttled_the_same_way_and_the_audit_log_records_the_lockout_once(client, monkeypatch):
    monkeypatch.setenv("SPINCO_LOGIN_MAX_FAILURES", "2")
    for _ in range(2):
        assert _login(client, "ghost@example.com", "x").status_code == 401
    for _ in range(5):
        assert _login(client, "ghost@example.com", "x").status_code == 429
    with SessionLocal() as db:
        locked = [r for r in db.execute(select(AuditLog)).scalars() if "locked for a while" in (r.details or "")]
    assert len(locked) == 1, "later refused attempts must not each write an audit row"


# ------------------------------------------------------------------------------------------------------------------------- health / readiness


def test_health_reports_what_kind_of_backend_this_is_without_any_secret(client, monkeypatch):
    monkeypatch.delenv("SPINCO_ENVIRONMENT", raising=False)
    monkeypatch.delenv("SPINCO_BIND_HOST", raising=False)
    body = client.get("/api/health").json()
    assert body == {"status": "ok", "environment": "local", "central_storage": "off"}
    monkeypatch.setenv("SPINCO_ENVIRONMENT", "Staging")
    monkeypatch.setenv("SPINCO_CENTRAL_STORAGE_MODE", "serve")
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and body["environment"] == "staging" and body["central_storage"] == "serve"


def test_a_network_reachable_backend_without_a_name_is_called_unspecified(client, monkeypatch):
    monkeypatch.delenv("SPINCO_ENVIRONMENT", raising=False)
    monkeypatch.setenv("SPINCO_BIND_HOST", "0.0.0.0")
    assert client.get("/api/health").json()["environment"] == "unspecified"


def test_health_is_available_without_signing_in(client):
    client.cookies.clear()
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health/ready").status_code in (200, 503)


def test_readiness_off_mode_needs_only_the_database(client):
    response = client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": True}}


def test_readiness_serve_mode_reports_a_missing_drive_configuration_as_a_boolean_not_as_text(client, monkeypatch):
    for name in ("SPINCO_GDRIVE_ENABLED", "SPINCO_GDRIVE_ROOT_FOLDER_ID", "SPINCO_GDRIVE_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SPINCO_CENTRAL_STORAGE_MODE", "serve")
    response = client.get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] is True
    assert body["checks"]["drive_configured"] is False
    assert set(body) == {"status", "checks"} and all(isinstance(v, bool) for v in body["checks"].values())


# ------------------------------------------------------------------------------------------------------------------------- upload limits


def test_the_per_file_limit_is_configurable_and_refuses_an_oversized_pdf(client, monkeypatch):
    monkeypatch.setenv("SPINCO_MAX_UPLOAD_MB", "1")
    big = pdf_bytes("big") + b"0" * (2 * 1024 * 1024)
    response = client.post("/api/uploads/expense-reports", files=[("files", ("ER-1.pdf", big, "application/pdf"))])
    assert response.status_code == 200
    item = response.json()["results"][0]
    assert item["success"] is False
    assert "1 MB upload limit" in item["error_message"]


def test_the_default_limit_is_25_mb_and_the_setting_is_bounded():
    assert hardening.max_upload_bytes({}) == 25 * 1024 * 1024
    assert hardening.max_upload_bytes({"SPINCO_MAX_UPLOAD_MB": "junk"}) == 25 * 1024 * 1024
    assert hardening.max_upload_bytes({"SPINCO_MAX_UPLOAD_MB": "100000"}) == 500 * 1024 * 1024


def test_only_real_pdfs_are_accepted(client):
    for name, content in (("notes.txt", b"hello"), ("fake.pdf", b"this is not a pdf at all"), ("empty.pdf", b"")):
        response = client.post("/api/uploads/expense-reports", files=[("files", (name, content, "application/pdf"))])
        item = response.json()["results"][0]
        assert item["success"] is False, name


def _run_middleware(content_length, path="/api/uploads/expense-reports", method="POST", env=None):
    calls = []

    async def inner(scope, receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": method, "path": path, "headers": [(b"content-length", str(content_length).encode())] if content_length is not None else []}
    asyncio.run(hardening.RequestSizeLimitMiddleware(inner, env)(scope, None, send))
    return calls, sent


def test_an_upload_request_declaring_too_large_a_body_is_refused_before_it_is_read():
    calls, sent = _run_middleware(600 * 1024 * 1024)
    assert calls == [] and sent[0]["status"] == 413


def test_the_request_cap_is_configurable_and_does_not_touch_other_paths_or_normal_requests():
    env = {"SPINCO_MAX_REQUEST_MB": "2"}
    assert _run_middleware(3 * 1024 * 1024, env=env)[1][0]["status"] == 413
    assert _run_middleware(1 * 1024 * 1024, env=env)[1][0]["status"] == 200
    assert _run_middleware(900 * 1024 * 1024, path="/api/auth/login", env=env)[1][0]["status"] == 200
    assert _run_middleware(None, env=env)[1][0]["status"] == 200


# ------------------------------------------------------------------------------------------------------------------------- CORS


def test_cors_defaults_dev_origins_on_loopback_and_nothing_on_a_network_reachable_backend():
    assert hardening.cors_origins({}) == hardening.DEV_CORS_ORIGINS
    assert hardening.cors_origins({"SPINCO_BIND_HOST": "0.0.0.0"}) == []


def test_cors_accepts_an_explicit_list_and_refuses_wildcards_paths_and_credentials():
    assert hardening.cors_origins({"SPINCO_CORS_ORIGINS": "https://spinco.corp.example, https://other.example:8443/"}) == ["https://spinco.corp.example", "https://other.example:8443"]
    for bad in ("*", "https://a.example/app", "ftp://a.example", "https://u:p@a.example", "a.example", "https://a.example?x=1"):
        with pytest.raises(CorsConfigError):
            hardening.cors_origins({"SPINCO_CORS_ORIGINS": bad})


# ------------------------------------------------------------------------------------------------------------------------- log redaction


@pytest.mark.parametrize(
    "text, secret",
    [
        ("login failed password=Hunter2!x for user", "Hunter2!x"),
        ("Authorization: Bearer ya29.A0ARrdaMabcdefghijklmnopqrstuvwxyz0123", "ya29.A0ARrdaM"),
        ('{"access_token": "ya29.abcdefghijklmnopqrstuvwxyz", "x": 1}', "ya29.abcdefghijklmnopqrstuvwxyz"),
        ("refresh 1//0gAbCdEfGhIjKlMnOpQrStUvWxYz0123456789 stored", "1//0gAbCdEf"),
        ("jwt eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij here", "eyJhbGciOiJSUzI1NiJ9"),
        ("key -----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY----- done", "MIIEvQIBADANBgkq"),
        ("client_secret=GOCSPX-abcdefghijk&other=1", "GOCSPX-abcdefghijk"),
        ("csrf_token: 3f9a8b7c6d5e4f3a2b1c", "3f9a8b7c6d5e4f3a2b1c"),
    ],
)
def test_redact_removes_secrets_but_keeps_the_rest(text, secret):
    cleaned = redact(text)
    assert secret not in cleaned
    assert "<redacted" in cleaned


def test_redact_leaves_ordinary_messages_alone():
    for text in ("Workbook generation completed: 3 reports in 0.12s", "tokens refreshed: 5", "upload of ER-10001.pdf stored", "the password policy requires 12 characters"):
        assert redact(text) == text


def test_log_file_gets_redacted_messages_and_is_configured_only_once(tmp_path):
    env = {"SPINCO_LOG_DIR": str(tmp_path / "logs"), "SPINCO_LOG_LEVEL": "INFO"}
    saved_levels = {name: logging.getLogger(name).level for name in ("", "app")}  # configure_logging changes logger levels: put them back so no later test sees the change
    configure_logging(env)
    configure_logging(env)
    root = logging.getLogger()
    handlers = [h for h in root.handlers if getattr(h, "_spinco_file", None)]
    try:
        assert len(handlers) == 1
        logging.getLogger("app.test").warning("sign-in with password=TopSecret123 and token=abc123def456 failed")
        logging.getLogger("uvicorn.error").error("Authorization: Bearer abcdefghijklmnop1234")
        for h in handlers:
            h.flush()
        text = (tmp_path / "logs" / "spinco-backend.log").read_text(encoding="utf-8")
        assert "sign-in with" in text
        assert "TopSecret123" not in text and "abc123def456" not in text and "abcdefghijklmnop1234" not in text
    finally:
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)
        for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
            lg = logging.getLogger(name)
            for h in [h for h in lg.handlers if getattr(h, "_spinco_file", None)]:
                lg.removeHandler(h)
                h.close()


# ------------------------------------------------------------------------------------------------------------------------- Excel export, central copy


def test_excel_generation_in_serve_mode_keeps_a_verified_copy_in_the_excel_exports_folder(client, serve):
    response = client.post("/api/excel-files/generate")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["central_copy"] == "STORED"
    folders = {f["id"]: f["name"] for f in serve.fake.files.values() if f["mimeType"] == "application/vnd.google-apps.folder"}
    exports = [f for f in serve.fake.files.values() if any(folders.get(p) == "Excel Exports" for p in f["parents"])]
    assert len(exports) == 1
    assert exports[0]["mimeType"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert exports[0]["name"].endswith(body["filename"])
    # the workbook users preview / download is still the central backend's own file
    assert client.get("/api/excel-files/download").status_code == 200


def test_excel_generation_off_mode_is_unchanged_and_reports_not_central(client, drive):
    body = client.post("/api/excel-files/generate").json()
    assert body["central_copy"] == "NOT_CENTRAL"
    assert not [f for f in drive.fake.files.values() if f["mimeType"] != "application/vnd.google-apps.folder"]


def test_a_drive_failure_never_breaks_workbook_generation(client, serve, monkeypatch):
    from app.services import central_storage

    def boom():
        raise central_storage.CentralUnavailable("down")

    monkeypatch.setattr(central_storage, "manager", boom)
    response = client.post("/api/excel-files/generate")
    assert response.status_code == 200
    assert response.json()["central_copy"] == "FAILED"
    assert client.get("/api/excel-files/download").status_code == 200
