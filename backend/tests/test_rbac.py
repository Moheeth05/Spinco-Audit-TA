"""Phase 10 RBAC privilege-escalation matrix: confirms VIEWER / AUDITOR / ADMIN
get exactly the 401/403/200 outcomes the spec calls for, via DIRECT API calls
(not frontend behavior). Uses independent TestClient instances per role so
session cookies never clobber each other.
"""

from fastapi.testclient import TestClient

from app.main import app

ADMIN_EMAIL = "intern@spincotech.com"
ADMIN_PASSWORD = "ChangeMe123!"


def _new_client():
    return TestClient(app)


def _login(c, email, password):
    resp = c.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["csrf_token"]


def _make_role_clients(client):
    """Uses the shared `client` fixture (already migrated/seeded DB) as the
    admin actor to provision one AUDITOR and one VIEWER account, then returns
    three independent (TestClient, csrf_token, role) tuples, each with its
    own cookie jar."""
    admin_csrf = _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    client.post(
        "/api/users",
        json={"name": "RBAC Auditor", "email": "rbac-auditor@spincotech.com", "role": "AUDITOR", "password": "AuditorPass123"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    client.post(
        "/api/users",
        json={"name": "RBAC Viewer", "email": "rbac-viewer@spincotech.com", "role": "VIEWER", "password": "ViewerPass123"},
        headers={"X-CSRF-Token": admin_csrf},
    )

    admin_client = client

    # Plain (never-entered) TestClient instances: the app's DB/lifespan setup
    # already happened via the shared `client` fixture, so these only need an
    # independent cookie jar per role - entering them as context managers
    # would spawn a long-lived ASGI lifespan portal thread per instance that
    # never gets torn down within a test, which stalls the suite once many
    # tests call this helper.
    auditor_client = _new_client()
    auditor_csrf = _login(auditor_client, "rbac-auditor@spincotech.com", "AuditorPass123")

    viewer_client = _new_client()
    viewer_csrf = _login(viewer_client, "rbac-viewer@spincotech.com", "ViewerPass123")

    return {
        "ADMIN": (admin_client, admin_csrf),
        "AUDITOR": (auditor_client, auditor_csrf),
        "VIEWER": (viewer_client, viewer_csrf),
    }


# ---------- unauthenticated: 401 everywhere ----------


def test_unauthenticated_requests_are_401_across_the_board(client):
    client.cookies.clear()  # the `client` fixture auto-logs-in as ADMIN by default
    endpoints = [
        "/api/dashboard/kpis",
        "/api/analytics/summary",
        "/api/expense-reports",
        "/api/trip-reports",
        "/api/excel-files",
        "/api/users",
        "/api/audit-logs",
    ]
    for path in endpoints:
        resp = client.get(path)
        assert resp.status_code == 401, f"{path} expected 401, got {resp.status_code}"


# ---------- read-only endpoints: all 3 roles get 200 ----------


def test_all_roles_can_view_read_only_endpoints(client):
    roles = _make_role_clients(client)
    endpoints = [
        "/api/dashboard/kpis",
        "/api/analytics/summary",
        "/api/expense-reports",
        "/api/trip-reports",
        "/api/excel-files",
    ]
    for role_name, (c, _csrf) in roles.items():
        for path in endpoints:
            resp = c.get(path)
            assert resp.status_code == 200, f"{role_name} GET {path} expected 200, got {resp.status_code}"


# ---------- upload: VIEWER rejected, AUDITOR/ADMIN allowed past the auth layer ----------


def test_viewer_cannot_upload_documents(client):
    roles = _make_role_clients(client)
    viewer_client, viewer_csrf = roles["VIEWER"]
    resp = viewer_client.post(
        "/api/uploads/expense-reports",
        files={"files": ("fake.pdf", b"%PDF-1.4 fake", "application/pdf")},
        headers={"X-CSRF-Token": viewer_csrf},
    )
    assert resp.status_code == 403


def test_auditor_and_admin_pass_the_upload_authorization_layer(client):
    roles = _make_role_clients(client)
    for role_name in ("AUDITOR", "ADMIN"):
        c, csrf = roles[role_name]
        resp = c.post(
            "/api/uploads/expense-reports",
            files={"files": ("fake.pdf", b"%PDF-1.4 fake", "application/pdf")},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code != 401 and resp.status_code != 403, (
            f"{role_name} upload unexpectedly blocked at the auth layer: {resp.status_code}"
        )


# ---------- reprocess: VIEWER rejected ----------


def test_viewer_cannot_reprocess_document(client):
    roles = _make_role_clients(client)
    viewer_client, viewer_csrf = roles["VIEWER"]
    resp = viewer_client.post("/api/documents/999999/reprocess", headers={"X-CSRF-Token": viewer_csrf})
    assert resp.status_code == 403


def test_auditor_and_admin_pass_the_reprocess_authorization_layer(client):
    roles = _make_role_clients(client)
    for role_name in ("AUDITOR", "ADMIN"):
        c, csrf = roles[role_name]
        resp = c.post("/api/documents/999999/reprocess", headers={"X-CSRF-Token": csrf})
        assert resp.status_code != 401 and resp.status_code != 403, (
            f"{role_name} reprocess unexpectedly blocked at the auth layer: {resp.status_code}"
        )


# ---------- validate: VIEWER rejected ----------


def test_viewer_cannot_validate_report(client):
    roles = _make_role_clients(client)
    viewer_client, viewer_csrf = roles["VIEWER"]
    resp = viewer_client.post("/api/expense-reports/999999/validate", headers={"X-CSRF-Token": viewer_csrf})
    assert resp.status_code == 403


# ---------- excel generate: VIEWER rejected, AUDITOR/ADMIN allowed ----------


def test_viewer_cannot_generate_excel(client):
    roles = _make_role_clients(client)
    viewer_client, viewer_csrf = roles["VIEWER"]
    resp = viewer_client.post("/api/excel-files/generate", headers={"X-CSRF-Token": viewer_csrf})
    assert resp.status_code == 403


def test_auditor_and_admin_pass_the_excel_generate_authorization_layer(client):
    roles = _make_role_clients(client)
    for role_name in ("AUDITOR", "ADMIN"):
        c, csrf = roles[role_name]
        resp = c.post("/api/excel-files/generate", headers={"X-CSRF-Token": csrf})
        assert resp.status_code != 401 and resp.status_code != 403, (
            f"{role_name} excel generate unexpectedly blocked at the auth layer: {resp.status_code}"
        )


# ---------- excel download: all 3 roles allowed to view/download ----------


def test_all_roles_can_reach_excel_download_authorization(client):
    roles = _make_role_clients(client)
    for role_name, (c, _csrf) in roles.items():
        resp = c.get("/api/excel-files/download")
        assert resp.status_code != 401 and resp.status_code != 403, (
            f"{role_name} excel download unexpectedly blocked at the auth layer: {resp.status_code}"
        )


# ---------- user management: only ADMIN ----------


def test_viewer_and_auditor_cannot_create_user(client):
    roles = _make_role_clients(client)
    for role_name in ("VIEWER", "AUDITOR"):
        c, csrf = roles[role_name]
        resp = c.post(
            "/api/users",
            json={"name": "Nope", "email": f"nope-{role_name}@spincotech.com", "role": "VIEWER", "password": "SomePass123"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 403, f"{role_name} expected 403 creating a user, got {resp.status_code}"


def test_viewer_and_auditor_cannot_change_role(client):
    roles = _make_role_clients(client)
    admin_client, admin_csrf = roles["ADMIN"]
    target = admin_client.post(
        "/api/users",
        json={"name": "Role Target", "email": "roletarget@spincotech.com", "role": "VIEWER", "password": "SomePass123"},
        headers={"X-CSRF-Token": admin_csrf},
    ).json()

    for role_name in ("VIEWER", "AUDITOR"):
        c, csrf = roles[role_name]
        resp = c.put(f"/api/users/{target['id']}/role", json={"role": "AUDITOR"}, headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 403, f"{role_name} expected 403 changing a role, got {resp.status_code}"


def test_viewer_and_auditor_cannot_deactivate_user(client):
    roles = _make_role_clients(client)
    admin_client, admin_csrf = roles["ADMIN"]
    target = admin_client.post(
        "/api/users",
        json={"name": "Status Target", "email": "statustarget@spincotech.com", "role": "VIEWER", "password": "SomePass123"},
        headers={"X-CSRF-Token": admin_csrf},
    ).json()

    for role_name in ("VIEWER", "AUDITOR"):
        c, csrf = roles[role_name]
        resp = c.put(f"/api/users/{target['id']}/status", json={"is_active": False}, headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 403, f"{role_name} expected 403 deactivating a user, got {resp.status_code}"


# ---------- audit logs: ADMIN only ----------


def test_only_admin_can_view_audit_logs(client):
    roles = _make_role_clients(client)
    for role_name in ("VIEWER", "AUDITOR"):
        c, _csrf = roles[role_name]
        resp = c.get("/api/audit-logs")
        assert resp.status_code == 403, f"{role_name} expected 403 on audit logs, got {resp.status_code}"

    admin_client, _admin_csrf = roles["ADMIN"]
    resp = admin_client.get("/api/audit-logs")
    assert resp.status_code == 200


# ---------- policy: GET allowed for all, no write endpoint exists ----------


def test_all_roles_can_view_policy_master(client):
    roles = _make_role_clients(client)
    for role_name, (c, _csrf) in roles.items():
        resp = c.get("/api/policy-master")
        assert resp.status_code == 200, f"{role_name} GET /api/policy-master expected 200, got {resp.status_code}"
