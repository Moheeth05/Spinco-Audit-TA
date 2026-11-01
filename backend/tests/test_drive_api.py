"""The /api/storage router: RBAC, CSRF, authorise-before-Drive, hidden Drive internals, archive/restore/missing behaviour.
FAKE-DRIVE tests of OUR logic (see fake_drive.py): they are not evidence about Google's real service."""

import hashlib
import json

import pytest

from app.database import SessionLocal
from app.main import app
from app.services.drive.runtime import get_storage_manager
from app.services.drive.storage import parse_location

from conftest import ADMIN_EMAIL, ADMIN_PASSWORD
from drive_testkit import make_stack, pdf_bytes, write_service_account_file

KIND = "expense_report"
PASSWORD = "SyntheticPass123!"


@pytest.fixture()
def drive(client, tmp_path, monkeypatch):
    for name in ("SPINCO_GDRIVE_ENABLED", "SPINCO_GDRIVE_ROOT_FOLDER_ID", "SPINCO_GDRIVE_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)
    stack = make_stack(tmp_path)
    app.dependency_overrides[get_storage_manager] = lambda: stack.manager
    yield stack
    app.dependency_overrides.pop(get_storage_manager, None)


def stored(stack, marker="a"):
    content = pdf_bytes(marker)
    with SessionLocal() as db:
        record = stack.manager.store(db, kind=KIND, content=content, original_filename=f"ER-{marker}.pdf").record
        return record.id, content


def login(client, email, password):
    client.cookies.clear()
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    client.csrf_token = response.json()["csrf_token"]  # the fixture's request wrapper attaches this to mutating calls


def make_users(client):
    users = {}
    for role in ("VIEWER", "AUDITOR"):
        email = f"{role.lower()}.drive@example.com"
        response = client.post("/api/users", json={"name": f"{role} Drive", "email": email, "role": role, "password": PASSWORD})
        assert response.status_code == 201, response.text
        users[role] = email
    return users


def as_role(client, users, role):
    if role == "ADMIN":
        login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    elif role == "ANON":
        client.cookies.clear()
        client.csrf_token = "none"
    else:
        login(client, users[role], PASSWORD)


# ------------------------------------------------------------------------------------------------------------ disabled by default


def test_drive_storage_is_off_by_default_and_nothing_else_changes(client, monkeypatch):
    for name in ("SPINCO_GDRIVE_ENABLED", "SPINCO_GDRIVE_ROOT_FOLDER_ID", "SPINCO_GDRIVE_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)
    status = client.get("/api/storage/status").json()
    assert status["enabled"] is False and status["configured"] is False and set(status["counts"]) == {"UPLOADING", "STORED", "ARCHIVED", "MISSING_EXTERNAL", "FAILED"}
    for method, url in (("post", "/api/storage/records/1/archive"), ("post", "/api/storage/records/1/restore"), ("post", "/api/storage/reconcile"), ("post", "/api/storage/sync"), ("get", "/api/storage/records/1/file")):
        response = getattr(client, method)(url)
        assert response.status_code == 503 and response.json()["detail"] == "Drive storage is not enabled."
    assert client.get("/api/health").status_code == 200 and client.get("/api/uploads").status_code == 200  # existing endpoints unaffected


def test_status_reports_misconfiguration_precisely_and_without_secrets(client, tmp_path, monkeypatch):
    monkeypatch.setenv("SPINCO_GDRIVE_ENABLED", "true")
    monkeypatch.delenv("SPINCO_GDRIVE_ROOT_FOLDER_ID", raising=False)
    monkeypatch.delenv("SPINCO_GDRIVE_CREDENTIALS_FILE", raising=False)
    status = client.get("/api/storage/status").json()
    assert status["enabled"] and not status["configured"] and "SPINCO_GDRIVE_ROOT_FOLDER_ID" in status["config_error"]
    monkeypatch.setenv("SPINCO_GDRIVE_ROOT_FOLDER_ID", "1SyntheticRootFolderId_0001")
    monkeypatch.setenv("SPINCO_GDRIVE_CREDENTIALS_FILE", str(write_service_account_file(tmp_path)))
    status = client.get("/api/storage/status").json()
    assert status["configured"] and status["config_error"] is None
    assert "BEGIN" not in json.dumps(status) and "client_email" not in json.dumps(status)
    # (deliberately no call that would build the real manager: that would contact Google, which no test may do)


# ------------------------------------------------------------------------------------------------------------ RBAC


@pytest.mark.parametrize(
    "method, path, allowed",
    [
        ("get", "/api/storage/status", {"ADMIN"}),
        ("get", "/api/storage/records?state=ARCHIVED", {"ADMIN"}),
        ("get", "/api/storage/records?state=MISSING_EXTERNAL", {"ADMIN"}),
        ("get", "/api/storage/records?state=all", {"ADMIN"}),
        ("get", "/api/storage/records", {"ADMIN", "AUDITOR", "VIEWER"}),
        ("post", "/api/storage/reconcile", {"ADMIN"}),
        ("post", "/api/storage/sync", {"ADMIN"}),
    ],
)
def test_role_based_access_on_listing_and_maintenance_endpoints(client, drive, method, path, allowed):
    users = make_users(client)
    for role in ("VIEWER", "AUDITOR", "ADMIN"):
        as_role(client, users, role)
        status = getattr(client, method)(path).status_code
        assert (status == 200) == (role in allowed), f"{role} {method.upper()} {path} -> {status}"
        assert status in (200, 403)
    as_role(client, users, "ANON")
    assert getattr(client, method)(path).status_code == 401


def test_role_based_access_on_archive_restore_and_stream(client, drive):
    users = make_users(client)
    record_id, _ = stored(drive, "rbac")
    for role, archive_ok in (("VIEWER", False), ("AUDITOR", False)):
        as_role(client, users, role)
        assert client.post(f"/api/storage/records/{record_id}/archive").status_code == 403
        assert client.post(f"/api/storage/records/{record_id}/restore").status_code == 403
        assert client.get(f"/api/storage/records/{record_id}/file").status_code == 200  # may view
    as_role(client, users, "ANON")
    assert client.post(f"/api/storage/records/{record_id}/archive").status_code == 401
    assert client.get(f"/api/storage/records/{record_id}/file").status_code == 401
    as_role(client, users, "ADMIN")
    assert client.post(f"/api/storage/records/{record_id}/archive").status_code == 200
    as_role(client, users, "AUDITOR")
    assert client.post(f"/api/storage/records/{record_id}/restore").status_code == 403  # restore is administrator-only
    as_role(client, users, "ADMIN")
    assert client.post(f"/api/storage/records/{record_id}/restore").status_code == 200


def test_mutating_calls_need_the_csrf_token(client, drive):
    record_id, _ = stored(drive, "csrf")
    response = client.post(f"/api/storage/records/{record_id}/archive", headers={"X-CSRF-Token": "forged"})
    assert response.status_code == 403 and "CSRF" in response.json()["detail"]
    with SessionLocal() as db:
        from app.models.storage_record import DocumentStorageRecord

        assert db.get(DocumentStorageRecord, record_id).state == "STORED"


# ------------------------------------------------------------------------------------------------------------ authorise before Drive


def test_unauthorised_requests_never_reach_drive(client, drive):
    users = make_users(client)
    record_id, _ = stored(drive, "gate")
    baseline = len(drive.fake.requests)
    as_role(client, users, "ANON")
    assert client.get(f"/api/storage/records/{record_id}/file").status_code == 401
    assert client.post(f"/api/storage/records/{record_id}/archive").status_code == 401
    as_role(client, users, "VIEWER")
    assert client.post(f"/api/storage/records/{record_id}/archive").status_code == 403
    assert client.post("/api/storage/reconcile").status_code == 403
    assert len(drive.fake.requests) == baseline  # not one call to Drive for any refused request
    assert client.get(f"/api/storage/records/{record_id}/file").status_code == 200
    assert len(drive.fake.requests) > baseline  # an authorised one does


# ------------------------------------------------------------------------------------------------------------ streaming


def test_an_authorised_user_streams_the_exact_bytes_with_range_support(client, drive):
    record_id, content = stored(drive, "stream")
    full = client.get(f"/api/storage/records/{record_id}/file")
    assert full.status_code == 200 and full.content == content
    assert full.headers["content-type"] == "application/pdf" and full.headers["cache-control"] == "no-store" and full.headers["accept-ranges"] == "bytes"
    assert hashlib.sha256(full.content).hexdigest() == hashlib.sha256(content).hexdigest()
    part = client.get(f"/api/storage/records/{record_id}/file", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and part.content == content[:10] and part.headers["content-range"].startswith("bytes 0-9/")


def test_archived_missing_and_unknown_documents_cannot_be_streamed(client, drive):
    record_id, _ = stored(drive, "hidden")
    assert client.post(f"/api/storage/records/{record_id}/archive").status_code == 200
    response = client.get(f"/api/storage/records/{record_id}/file")
    assert response.status_code == 410 and "archived" in response.json()["detail"]
    assert client.post(f"/api/storage/records/{record_id}/restore").status_code == 200
    assert client.get(f"/api/storage/records/{record_id}/file").status_code == 200
    with SessionLocal() as db:
        from app.models.storage_record import DocumentStorageRecord

        file_id = parse_location(db.get(DocumentStorageRecord, record_id).location)
    drive.fake.external_delete(file_id)
    assert client.post("/api/storage/reconcile").status_code == 200
    gone = client.get(f"/api/storage/records/{record_id}/file")
    assert gone.status_code == 410 and "missing" in gone.json()["detail"]
    assert client.get("/api/storage/records/99999/file").status_code == 404


# ------------------------------------------------------------------------------------------------------------ lifecycle through the API


def test_archive_hides_from_the_active_list_and_restore_brings_it_back(client, drive):
    record_id, _ = stored(drive, "list")
    other_id, _ = stored(drive, "list-other-marker")
    active = [r["id"] for r in client.get("/api/storage/records").json()]
    assert set(active) == {record_id, other_id}
    archived = client.post(f"/api/storage/records/{record_id}/archive").json()
    assert archived["state"] == "ARCHIVED" and archived["archived_at"]
    assert [r["id"] for r in client.get("/api/storage/records").json()] == [other_id]
    assert [r["id"] for r in client.get("/api/storage/records?state=ARCHIVED").json()] == [record_id]
    assert client.post(f"/api/storage/records/{record_id}/archive").status_code == 200  # idempotent
    restored = client.post(f"/api/storage/records/{record_id}/restore").json()
    assert restored["state"] == "STORED" and restored["restored_at"]
    assert {r["id"] for r in client.get("/api/storage/records").json()} == {record_id, other_id}


def test_an_external_deletion_shows_up_only_in_the_admin_missing_list(client, drive):
    record_id, _ = stored(drive, "external")
    with SessionLocal() as db:
        from app.models.storage_record import DocumentStorageRecord

        file_id = parse_location(db.get(DocumentStorageRecord, record_id).location)
    drive.fake.external_trash(file_id)
    report = client.post("/api/storage/reconcile").json()
    assert report["missing_external"] == 1
    assert client.get("/api/storage/records").json() == []
    missing = client.get("/api/storage/records?state=MISSING_EXTERNAL").json()
    assert [r["id"] for r in missing] == [record_id] and missing[0]["missing_detected_at"]
    assert client.get("/api/storage/status").json()["counts"]["MISSING_EXTERNAL"] == 1
    assert client.post("/api/storage/sync").status_code == 200  # first call establishes the baseline token
    client.post(f"/api/storage/records/{record_id}/restore")
    assert client.get("/api/storage/records").json()[0]["id"] == record_id


def test_invalid_input_and_invalid_transitions_get_clear_errors(client, drive):
    assert client.get("/api/storage/records?state=BOGUS").status_code == 400
    assert client.post("/api/storage/records/99999/archive").status_code == 404
    assert client.post("/api/storage/records/99999/restore").status_code == 404
    with SessionLocal() as db:
        from app.models.storage_record import DocumentStorageRecord

        row = DocumentStorageRecord(kind=KIND, sha256="0" * 64, original_filename="x.pdf", size=1, md5="0" * 32, location="gdrive:fakeid000000099999", state="FAILED")
        db.add(row)
        db.commit()
        failed_id = row.id
    assert client.post(f"/api/storage/records/{failed_id}/archive").status_code == 409
    assert client.post(f"/api/storage/records/{failed_id}/restore").status_code == 409


# ------------------------------------------------------------------------------------------------------------ nothing internal leaks


def test_no_drive_id_url_or_credential_ever_appears_in_a_response(client, drive):
    record_id, _ = stored(drive, "leak")
    responses = [
        client.get("/api/storage/records"),
        client.get("/api/storage/records?state=all"),
        client.get("/api/storage/status"),
        client.post(f"/api/storage/records/{record_id}/archive"),
        client.post(f"/api/storage/records/{record_id}/restore"),
        client.post("/api/storage/reconcile"),
        client.get(f"/api/storage/records/{record_id}/file"),
    ]
    for response in responses:
        text = response.text if "json" in response.headers.get("content-type", "") else ""
        for forbidden in ("gdrive:", "fakeid", "googleapis", "drive.google", "location", "md5", "BEGIN", "Bearer", "fake-token"):
            assert forbidden not in text, f"{forbidden!r} leaked in {response.request.url.path}"
        assert not {"location", "x-goog-hash"} & {h.lower() for h in response.headers}
    listing = client.get("/api/storage/records?state=all").json()
    assert set(listing[0]) == {"id", "kind", "original_filename", "size", "state", "last_error", "document_kind", "document_pk", "created_at", "stored_at", "archived_at", "restored_at", "missing_detected_at", "last_verified_at"}


def test_drive_outages_surface_as_generic_503_without_google_details(client, drive):
    record_id, _ = stored(drive, "outage")
    drive.fake.fail_next(500, 503)
    drive.client._config = type(drive.client._config)(**{**drive.client._config.__dict__, "max_attempts": 2})
    for response in (client.get(f"/api/storage/records/{record_id}/file"), client.post(f"/api/storage/records/{record_id}/archive")):
        assert response.status_code == 503
        assert response.json()["detail"] == "Drive storage did not complete the request."
        assert "backendError" not in response.text and "injected" not in response.text
    drive.fake.faults.clear()
    assert client.get(f"/api/storage/records/{record_id}/file").status_code == 200  # recovers by itself when Drive returns
