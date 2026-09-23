"""Phase 10 authentication tests: login, logout, session lifecycle,
current-user, password change. Uses the seeded ADMIN account
(intern@spincotech.com / the SPINCO_ADMIN_INITIAL_PASSWORD dev default,
"ChangeMe123!") that every fresh test database gets via seed_admin_user().
"""

from app.database import SessionLocal
from app.models.logs import AuditLog

ADMIN_EMAIL = "intern@spincotech.com"
ADMIN_PASSWORD = "ChangeMe123!"


def _login(client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


# ---------- 1-4: login outcomes ----------


def test_1_correct_login_succeeds_and_sets_cookie(client):
    resp = _login(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["email"] == ADMIN_EMAIL
    assert body["user"]["role"] == "ADMIN"
    assert "csrf_token" in body and len(body["csrf_token"]) > 10
    assert "spinco_session" in resp.cookies


def test_2_wrong_password_is_generic_401(client):
    resp = _login(client, password="wrong-password-entirely")
    assert resp.status_code == 401
    assert "spinco_session" not in resp.cookies


def test_3_unknown_email_is_the_same_generic_401_as_wrong_password(client):
    """Never reveals whether an account exists - both failure modes return
    the exact same status code and message."""
    wrong_password_resp = _login(client, password="wrong-password-entirely")
    unknown_user_resp = _login(client, email="nobody@spincotech.com", password="whatever12345")
    assert unknown_user_resp.status_code == 401 == wrong_password_resp.status_code
    assert unknown_user_resp.json()["detail"] == wrong_password_resp.json()["detail"]


def test_4_deactivated_user_cannot_login(client):
    _login(client)  # admin session
    created = client.post(
        "/api/users",
        json={"name": "Deactivated Person", "email": "deactivated@spincotech.com", "role": "VIEWER", "password": "SomePassword123"},
        headers={"X-CSRF-Token": _csrf(client)},
    ).json()
    client.put(
        f"/api/users/{created['id']}/status", json={"is_active": False}, headers={"X-CSRF-Token": _csrf(client)}
    )

    resp = _login(client, email="deactivated@spincotech.com", password="SomePassword123")
    assert resp.status_code == 401


def _csrf(client):
    """Re-derives the current session's CSRF token by hitting login again is
    wasteful - tests that need it call _login and keep the returned token."""
    resp = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    return resp.json()["csrf_token"]


# ---------- 5-6: logout ----------


def test_5_logout_clears_session(client):
    login_resp = _login(client)
    csrf = login_resp.json()["csrf_token"]
    logout_resp = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert logout_resp.status_code == 200


def test_6_old_session_cannot_continue_after_logout(client):
    login_resp = _login(client)
    csrf = login_resp.json()["csrf_token"]
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

    me_resp = client.get("/api/auth/me")
    assert me_resp.status_code == 401


# ---------- 7-8: protected endpoints / current user ----------


def test_7_protected_endpoint_without_authentication_is_401(client):
    client.cookies.clear()  # the `client` fixture auto-logs-in as ADMIN by default
    resp = client.get("/api/dashboard/kpis")
    assert resp.status_code == 401


def test_8_current_user_endpoint_returns_safe_fields_only(client):
    _login(client)
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    body = resp.json()["user"]
    assert body["email"] == ADMIN_EMAIL
    assert body["role"] == "ADMIN"
    assert "password" not in body
    assert "password_hash" not in body


# ---------- 9-12: password change ----------


def test_9_10_11_password_change_then_old_password_rejected_new_accepted(client):
    login_resp = _login(client)
    csrf = login_resp.json()["csrf_token"]

    change_resp = client.post(
        "/api/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "BrandNewPassword123", "confirm_new_password": "BrandNewPassword123"},
        headers={"X-CSRF-Token": csrf},
    )
    assert change_resp.status_code == 200

    old_login = _login(client, password=ADMIN_PASSWORD)
    assert old_login.status_code == 401

    new_login = _login(client, password="BrandNewPassword123")
    assert new_login.status_code == 200


def test_12_wrong_current_password_rejected(client):
    login_resp = _login(client)
    csrf = login_resp.json()["csrf_token"]
    resp = client.post(
        "/api/auth/change-password",
        json={"current_password": "totally-wrong", "new_password": "BrandNewPassword123", "confirm_new_password": "BrandNewPassword123"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_13_mismatched_confirmation_rejected(client):
    login_resp = _login(client)
    csrf = login_resp.json()["csrf_token"]
    resp = client.post(
        "/api/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "BrandNewPassword123", "confirm_new_password": "SomethingElse123"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


# ---------- 14: session invalidation on password change ----------


def test_14_password_change_invalidates_other_sessions(client):
    from fastapi.testclient import TestClient
    from app.main import app

    login_resp = _login(client)
    csrf = login_resp.json()["csrf_token"]

    # A second, independent "browser" logged in as the same user.
    with TestClient(app) as other_client:
        other_login = other_client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert other_login.status_code == 200
        assert other_client.get("/api/auth/me").status_code == 200

        client.post(
            "/api/auth/change-password",
            json={"current_password": ADMIN_PASSWORD, "new_password": "AnotherNewPassword123", "confirm_new_password": "AnotherNewPassword123"},
            headers={"X-CSRF-Token": csrf},
        )

        # The other session must now be dead.
        assert other_client.get("/api/auth/me").status_code == 401

    # The session that performed the change stays alive.
    assert client.get("/api/auth/me").status_code == 200


# ---------- 15: initial admin ----------


def test_15_initial_admin_account_and_must_change_password_flag(client):
    resp = _login(client)
    body = resp.json()["user"]
    assert body["email"] == ADMIN_EMAIL
    assert body["role"] == "ADMIN"
    assert body["must_change_password"] is True


# ---------- CSRF ----------


def test_16_mutating_request_without_csrf_header_is_rejected(client):
    _login(client)
    resp = client.post(
        "/api/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "IrrelevantNew123", "confirm_new_password": "IrrelevantNew123"},
    )
    assert resp.status_code == 403


def test_17_mutating_request_with_wrong_csrf_header_is_rejected(client):
    _login(client)
    resp = client.post(
        "/api/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "IrrelevantNew123", "confirm_new_password": "IrrelevantNew123"},
        headers={"X-CSRF-Token": "not-the-real-token"},
    )
    assert resp.status_code == 403


# ---------- audit: login events, no secret leakage ----------


def test_18_successful_login_creates_audit_event(client):
    db = SessionLocal()
    try:
        before = db.query(AuditLog).filter(AuditLog.action == "AUTH_LOGIN_SUCCESS").count()
    finally:
        db.close()

    _login(client)  # the `client` fixture already logged in once during setup

    db = SessionLocal()
    try:
        events = db.query(AuditLog).filter(AuditLog.action == "AUTH_LOGIN_SUCCESS").all()
        assert len(events) == before + 1
        assert all(ADMIN_PASSWORD not in (e.details or "") for e in events)
    finally:
        db.close()


def test_19_failed_login_creates_audit_event_without_leaking_password(client):
    _login(client, password="some-guessed-password")
    db = SessionLocal()
    try:
        events = db.query(AuditLog).filter(AuditLog.action == "AUTH_LOGIN_FAILURE").all()
        assert len(events) == 1
        assert "some-guessed-password" not in (events[0].details or "")
    finally:
        db.close()


def test_20_logout_creates_audit_event(client):
    login_resp = _login(client)
    csrf = login_resp.json()["csrf_token"]
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    db = SessionLocal()
    try:
        assert db.query(AuditLog).filter(AuditLog.action == "AUTH_LOGOUT").count() == 1
    finally:
        db.close()
