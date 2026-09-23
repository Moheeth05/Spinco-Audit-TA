"""Phase 10 user-management tests: CRUD, role changes, activation, and the
final-active-admin protection rules. Uses the seeded ADMIN account
(intern@spincotech.com / ChangeMe123!) as the actor for admin-gated calls.
"""

from app.database import SessionLocal
from app.models.logs import AuditLog

ADMIN_EMAIL = "intern@spincotech.com"
ADMIN_PASSWORD = "ChangeMe123!"


def _login_admin(client):
    resp = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200
    return resp.json()["csrf_token"]


def _create_user(client, csrf, *, name="Auditor One", email="auditor1@spincotech.com", role="AUDITOR", password="InitialPass123"):
    return client.post(
        "/api/users",
        json={"name": name, "email": email, "role": role, "password": password},
        headers={"X-CSRF-Token": csrf},
    )


# ---------- create ----------


def test_create_user_succeeds_and_returns_safe_fields(client):
    csrf = _login_admin(client)
    resp = _create_user(client, csrf)
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "auditor1@spincotech.com"
    assert body["role"] == "AUDITOR"
    assert body["must_change_password"] is True
    assert "password" not in body
    assert "password_hash" not in body


def test_create_user_duplicate_email_rejected(client):
    csrf = _login_admin(client)
    first = _create_user(client, csrf)
    assert first.status_code == 201
    dup = _create_user(client, csrf, name="Someone Else")
    assert dup.status_code == 409


def test_create_user_records_audit_event(client):
    csrf = _login_admin(client)
    _create_user(client, csrf)
    db = SessionLocal()
    try:
        events = db.query(AuditLog).filter(AuditLog.action == "USER_CREATED").all()
        assert len(events) == 1
        assert "InitialPass123" not in (events[0].details or "")
    finally:
        db.close()


# ---------- update ----------


def test_update_user_name_and_email(client):
    csrf = _login_admin(client)
    created = _create_user(client, csrf).json()
    resp = client.put(
        f"/api/users/{created['id']}",
        json={"name": "Renamed Auditor", "email": "renamed@spincotech.com"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed Auditor"
    assert body["email"] == "renamed@spincotech.com"


def test_update_user_email_to_existing_address_rejected(client):
    csrf = _login_admin(client)
    _create_user(client, csrf, name="Person A", email="persona@spincotech.com")
    person_b = _create_user(client, csrf, name="Person B", email="personb@spincotech.com").json()
    resp = client.put(
        f"/api/users/{person_b['id']}",
        json={"email": "persona@spincotech.com"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 409


# ---------- role change ----------


def test_change_role_succeeds_and_is_audited(client):
    csrf = _login_admin(client)
    created = _create_user(client, csrf).json()
    resp = client.put(
        f"/api/users/{created['id']}/role",
        json={"role": "VIEWER"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "VIEWER"

    db = SessionLocal()
    try:
        events = db.query(AuditLog).filter(AuditLog.action == "USER_ROLE_CHANGED").all()
        assert len(events) == 1
        assert "old_role=AUDITOR" in events[0].details
        assert "new_role=VIEWER" in events[0].details
    finally:
        db.close()


def test_cannot_change_role_of_final_active_admin(client):
    csrf = _login_admin(client)
    me = client.get("/api/auth/me").json()["user"]
    resp = client.put(
        f"/api/users/{me['id']}/role",
        json={"role": "VIEWER"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_can_change_role_of_admin_when_another_active_admin_exists(client):
    csrf = _login_admin(client)
    second_admin = _create_user(client, csrf, name="Second Admin", email="admin2@spincotech.com", role="ADMIN").json()
    me = client.get("/api/auth/me").json()["user"]
    resp = client.put(
        f"/api/users/{me['id']}/role",
        json={"role": "VIEWER"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert second_admin["role"] == "ADMIN"


# ---------- activate / deactivate ----------


def test_deactivate_and_reactivate_user(client):
    csrf = _login_admin(client)
    created = _create_user(client, csrf).json()

    off = client.put(f"/api/users/{created['id']}/status", json={"is_active": False}, headers={"X-CSRF-Token": csrf})
    assert off.status_code == 200
    assert off.json()["is_active"] is False

    on = client.put(f"/api/users/{created['id']}/status", json={"is_active": True}, headers={"X-CSRF-Token": csrf})
    assert on.status_code == 200
    assert on.json()["is_active"] is True

    db = SessionLocal()
    try:
        actions = [e.action for e in db.query(AuditLog).filter(AuditLog.entity_id == str(created["id"])).all()]
        assert "USER_DEACTIVATED" in actions
        assert "USER_ACTIVATED" in actions
    finally:
        db.close()


def test_deactivated_user_loses_api_access(client):
    from fastapi.testclient import TestClient
    from app.main import app

    csrf = _login_admin(client)
    created = _create_user(client, csrf).json()

    # Use a second, independent "browser" for the target user so its login
    # doesn't clobber the admin session cookie on the shared `client` fixture.
    with TestClient(app) as target_client:
        target_login = target_client.post(
            "/api/auth/login", json={"email": created["email"], "password": "InitialPass123"}
        )
        assert target_login.status_code == 200

        off = client.put(f"/api/users/{created['id']}/status", json={"is_active": False}, headers={"X-CSRF-Token": csrf})
        assert off.status_code == 200

        relogin = target_client.post("/api/auth/login", json={"email": created["email"], "password": "InitialPass123"})
        assert relogin.status_code == 401


def test_cannot_deactivate_final_active_admin(client):
    csrf = _login_admin(client)
    me = client.get("/api/auth/me").json()["user"]
    resp = client.put(f"/api/users/{me['id']}/status", json={"is_active": False}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400


def test_can_deactivate_admin_when_another_active_admin_exists(client):
    csrf = _login_admin(client)
    _create_user(client, csrf, name="Second Admin", email="admin3@spincotech.com", role="ADMIN")
    me = client.get("/api/auth/me").json()["user"]
    resp = client.put(f"/api/users/{me['id']}/status", json={"is_active": False}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200


# ---------- admin password reset ----------


def test_admin_can_reset_another_users_password(client):
    csrf = _login_admin(client)
    created = _create_user(client, csrf).json()

    resp = client.put(
        f"/api/users/{created['id']}/password",
        json={"new_password": "AdminSetThis123"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200

    login_new = client.post("/api/auth/login", json={"email": created["email"], "password": "AdminSetThis123"})
    assert login_new.status_code == 200

    db = SessionLocal()
    try:
        events = db.query(AuditLog).filter(AuditLog.action == "ADMIN_PASSWORD_RESET").all()
        assert len(events) == 1
        assert "AdminSetThis123" not in (events[0].details or "")
    finally:
        db.close()


# ---------- non-admin cannot manage users ----------


def test_auditor_cannot_create_user(client):
    csrf = _login_admin(client)
    _create_user(client, csrf, name="Plain Auditor", email="plainauditor@spincotech.com", role="AUDITOR", password="AuditorPass123")
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

    auditor_login = client.post("/api/auth/login", json={"email": "plainauditor@spincotech.com", "password": "AuditorPass123"})
    auditor_csrf = auditor_login.json()["csrf_token"]

    resp = _create_user(client, auditor_csrf, name="Nope", email="nope@spincotech.com")
    assert resp.status_code == 403


def test_viewer_cannot_list_users(client):
    csrf = _login_admin(client)
    _create_user(client, csrf, name="Plain Viewer", email="plainviewer@spincotech.com", role="VIEWER", password="ViewerPass123")
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

    client.post("/api/auth/login", json={"email": "plainviewer@spincotech.com", "password": "ViewerPass123"})
    resp = client.get("/api/users")
    assert resp.status_code == 403
