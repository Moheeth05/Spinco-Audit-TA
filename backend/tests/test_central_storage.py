"""Central storage (modes off / shadow / serve) at the application level: uploads, duplicates, timestamps, listing, verified preview/download, delete/restore and the
two-PC convergence. FAKE-DRIVE tests of OUR logic (tests/fake_drive.py): they are not evidence about Google's real service - the staging harness
(tools/central_staging_check.py) is what runs against the real staging Shared Drive."""

import threading
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from fastapi import BackgroundTasks, UploadFile
from sqlalchemy import func, select

from app.database import SessionLocal, engine
from app.models import storage_record as sr
from app.models.expense_report import ExpenseReport
from app.models.processing_job import EXTRACT, SHADOW_COPY, ProcessingJob
from app.models.storage_record import DocumentStorageRecord
from app.models.trip import TripReport
from app.models.upload_attempt import UploadAttempt
from app.models.user import User
from app.services import central_storage, processing_jobs, upload_history
from app.services.drive.storage import parse_location

from drive_testkit import make_stack, pdf_bytes

PASSWORD = "SyntheticPass123!"


@pytest.fixture()
def drive(client, tmp_path, monkeypatch):
    for name in ("SPINCO_GDRIVE_ENABLED", "SPINCO_GDRIVE_ROOT_FOLDER_ID", "SPINCO_GDRIVE_CREDENTIALS_FILE", "SPINCO_CENTRAL_STORAGE_MODE", "SPINCO_DUPLICATE_RETENTION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SPINCO_ENVIRONMENT", "dev")  # the fake Drive's client is unguarded, which is allowed only in dev / staging
    stack = make_stack(tmp_path)
    monkeypatch.setattr(central_storage, "manager", lambda: stack.manager)  # the guarded manager of the fake Drive (the real one needs credentials)
    monkeypatch.setattr(central_storage, "CONVERGE_WAIT_SECONDS", 10.0)
    return stack


@pytest.fixture()
def serve(drive, monkeypatch):
    monkeypatch.setenv("SPINCO_CENTRAL_STORAGE_MODE", "serve")
    return drive


@pytest.fixture()
def shadow(drive, monkeypatch):
    monkeypatch.setenv("SPINCO_CENTRAL_STORAGE_MODE", "shadow")
    return drive


def upload(client, content, name="ER-10001.pdf", kind="expense-reports"):
    response = client.post(f"/api/uploads/{kind}", files=[("files", (name, content, "application/pdf"))])
    assert response.status_code == 200, response.text
    return response.json()["results"][0]


def drive_files(stack, folder_name=None):
    folders = {f["id"]: f["name"] for f in stack.fake.files.values() if f["mimeType"] == "application/vnd.google-apps.folder"}
    return [f for f in stack.fake.files.values() if f["mimeType"] != "application/vnd.google-apps.folder"
            and (folder_name is None or any(folders.get(p) == folder_name for p in f["parents"]))]


def count(model, *where):
    with SessionLocal() as db:
        return db.execute(select(func.count()).select_from(model).where(*where)).scalar_one()


def make_viewer(client):
    response = client.post("/api/users", json={"name": "Viewer Central", "email": "viewer.central@example.com", "role": "VIEWER", "password": PASSWORD})
    assert response.status_code == 201, response.text
    return "viewer.central@example.com"


def login(client, email, password):
    client.cookies.clear()
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    client.csrf_token = response.json()["csrf_token"]


# ------------------------------------------------------------------------------------------------------------------------------- configuration


def test_central_storage_is_off_by_default_and_an_invalid_setting_fails_safe():
    assert central_storage.config({}).mode == "off" and not central_storage.config({}).active
    assert central_storage.config({"SPINCO_CENTRAL_STORAGE_MODE": "SERVE"}).mode == "serve"
    bad = central_storage.config({"SPINCO_CENTRAL_STORAGE_MODE": "yes-please", "SPINCO_DUPLICATE_RETENTION": "always"})
    assert bad.mode == "off" and bad.duplicate_retention == "metadata" and "SPINCO_CENTRAL_STORAGE_MODE" in bad.error and "SPINCO_DUPLICATE_RETENTION" in bad.error  # never switched ON by a typo


def test_status_endpoint_tells_everyone_the_mode_and_only_administrators_the_rest(client, serve):
    admin = client.get("/api/central/status").json()
    assert admin["mode"] == "serve" and admin["duplicate_retention"] == "metadata" and admin["security_warnings"] == []
    email = make_viewer(client)
    login(client, email, PASSWORD)
    viewer = client.get("/api/central/status").json()
    assert viewer["mode"] == "serve" and viewer["duplicate_retention"] is None and viewer["job_counts"] is None and viewer["security_warnings"] is None
    client.cookies.clear()
    assert client.get("/api/central/status").status_code == 401


# ------------------------------------------------------------------------------------------------------------------------------------- mode off


def test_mode_off_behaves_as_before_and_still_records_the_attempt(client, drive):
    content = pdf_bytes("off")
    result = upload(client, content)
    assert result["success"] and result["result"] == "SUCCESS" and result["storage_state"] == "NOT_CENTRAL"
    assert drive_files(drive) == [] and count(ProcessingJob) == 0 and count(DocumentStorageRecord) == 0  # nothing touched Drive; no durable job (BackgroundTasks as before)
    listed = client.get("/api/uploads").json()[0]
    assert listed["storage_state"] == "NOT_CENTRAL" and listed["processing_state"] == "NONE" and listed["uploaded_by_name"] == "Intern"
    assert client.get(f"/api/documents/{result['document_id']}/file").content == content  # the local file, exactly as before


# ---------------------------------------------------------------------------------------------------------------- serve: store, list, preview


def test_serve_mode_stores_the_original_in_drive_before_reporting_success(client, serve):
    content = pdf_bytes("serve-1")
    result = upload(client, content, "ER-20001.pdf")
    assert result["success"] and result["result"] == "SUCCESS" and result["storage_state"] == "STORED"
    with SessionLocal() as db:
        record = db.execute(select(DocumentStorageRecord)).scalar_one()
        document = db.get(ExpenseReport, int(result["document_id"].split(":")[1]))
        assert record.kind == "expense_report" and record.state == sr.STORED and record.sha256 == document.file_hash and record.size == len(content)
        assert record.md5 and record.location.startswith("gdrive:")  # the Drive id, checksum and size are recorded centrally
        job = db.execute(select(ProcessingJob)).scalar_one()
        assert (job.job_type, job.state, job.document_pk) == (EXTRACT, "QUEUED", document.id)  # OCR is a durable job, not part of the request
        assert document.original_filename == "ER-20001.pdf"  # the original name is kept in the database whatever the Drive file is called
    assert len(drive_files(serve, "Expense Reports")) == 1


def test_serve_mode_lists_and_serves_the_central_copy_with_verification(client, serve):
    content = pdf_bytes("serve-2")
    result = upload(client, content)
    doc = result["document_id"]
    listed = next(i for i in client.get("/api/uploads").json() if i["document_id"] == doc)
    assert listed["storage_state"] == "STORED" and listed["processing_state"] == "QUEUED" and listed["duplicate_count"] == 0
    detail = client.get(f"/api/documents/{doc}").json()
    assert detail["storage_state"] == "STORED" and detail["uploaded_by_name"] == "Intern" and detail["uploaded_at_ist"].endswith("+05:30")
    response = client.get(f"/api/documents/{doc}/file")
    assert response.status_code == 200 and response.headers["content-type"] == "application/pdf" and response.content == content
    assert response.headers["content-disposition"].startswith("inline") and "ER-10001.pdf" in response.headers["content-disposition"]
    assert client.get(f"/api/documents/{doc}/file?download=true").headers["content-disposition"].startswith("attachment")
    ranged = client.get(f"/api/documents/{doc}/file", headers={"Range": "bytes=0-9"})
    assert ranged.status_code == 206 and ranged.content == content[:10]


def test_serve_mode_never_substitutes_a_local_copy_when_the_central_copy_is_unavailable(client, serve):
    content = pdf_bytes("serve-3")
    doc = upload(client, content)["document_id"]
    with SessionLocal() as db:
        record = db.execute(select(DocumentStorageRecord)).scalar_one()
        file_id = parse_location(record.location)
    assert client.get(f"/api/documents/{doc}/file").status_code == 200

    serve.fake.fail_next(times=1000, status=503)  # Drive outage
    outage = client.get(f"/api/documents/{doc}/file")
    assert outage.status_code == 503 and outage.content != content and "temporarily unavailable" in outage.json()["detail"]
    serve.fake.faults.clear()

    serve.fake.external_modify(file_id, b"%PDF-tampered")  # the bytes changed in Drive: the verified download refuses to serve them
    tampered = client.get(f"/api/documents/{doc}/file")
    assert tampered.status_code == 502 and tampered.content != b"%PDF-tampered"
    serve.fake.external_modify(file_id, content)

    serve.fake.external_delete(file_id)  # the file was deleted outside the application
    missing = client.get(f"/api/documents/{doc}/file")
    assert missing.status_code in (404, 410) and missing.content != content  # explicit error; the local working copy is NOT served instead

    with SessionLocal() as db:
        db.execute(DocumentStorageRecord.__table__.update().values(state=sr.MISSING_EXTERNAL))
        db.commit()
    assert client.get(f"/api/documents/{doc}/file").status_code == 410
    with SessionLocal() as db:
        db.execute(DocumentStorageRecord.__table__.delete())
        db.commit()
    assert client.get(f"/api/documents/{doc}/file").status_code == 404  # no central record at all: not a file this deployment may serve


def test_serve_mode_reports_deferred_and_creates_nothing_when_drive_is_down(client, serve):
    serve.fake.fail_next(times=1000, status=503)
    result = upload(client, pdf_bytes("down"))
    assert result["success"] is False and result["result"] == "DEFERRED" and result["retryable"] is True and "temporarily unavailable" in result["error_message"]
    assert count(ExpenseReport) == 0 and count(ProcessingJob) == 0  # never claimed as success, no document without its central original
    with SessionLocal() as db:
        attempt = db.execute(select(UploadAttempt)).scalar_one()
        assert attempt.result == "DEFERRED" and attempt.uploaded_by_id is not None and attempt.sha256
    serve.fake.faults.clear()
    again = upload(client, pdf_bytes("down"))  # after recovery the same file uploads normally, once
    assert again["result"] == "SUCCESS" and count(ExpenseReport) == 1 and len(drive_files(serve)) == 1


def test_serve_mode_supports_trip_reports_with_the_same_service(client, serve):
    result = upload(client, pdf_bytes("trip"), "TRIP-30001_Trip_To_Pune.pdf", kind="trip-reports")
    assert result["result"] == "SUCCESS" and result["document_id"].startswith("trip:") and result["storage_state"] == "STORED"
    assert len(drive_files(serve, "Trip Reports")) == 1 and count(TripReport) == 1


def test_serve_mode_delete_archives_the_central_copy_and_a_reupload_restores_it(client, serve):
    content = pdf_bytes("archive")
    doc = upload(client, content)["document_id"]
    assert client.delete(f"/api/documents/{doc}").status_code == 204
    with SessionLocal() as db:
        record = db.execute(select(DocumentStorageRecord)).scalar_one()
        assert record.state == sr.ARCHIVED  # reversible: nothing was destroyed in Drive
    assert len(drive_files(serve)) == 1
    assert client.get(f"/api/documents/{doc}/file").status_code == 404
    result = upload(client, content)  # the same file again, authorised: the central copy comes back, no second Drive file
    assert result["result"] == "SUCCESS" and result["storage_state"] == "STORED" and len(drive_files(serve)) == 1
    with SessionLocal() as db:
        assert db.execute(select(DocumentStorageRecord)).scalar_one().state == sr.STORED


def test_serve_mode_delete_refuses_when_drive_is_down_and_deletes_nothing(client, serve):
    doc = upload(client, pdf_bytes("keep"))["document_id"]
    serve.fake.fail_next(times=1000, status=503)
    assert client.delete(f"/api/documents/{doc}").status_code == 503
    serve.fake.faults.clear()
    assert count(ExpenseReport) == 1 and client.get(f"/api/documents/{doc}").status_code == 200


# ------------------------------------------------------------------------------------------------------------------------------------- duplicates


def test_an_exact_duplicate_creates_no_second_document_and_is_recorded_with_its_relationship(client, serve):
    content = pdf_bytes("dup")
    first = upload(client, content, "ER-40001.pdf")
    second = upload(client, content, "renamed-copy.pdf")
    assert first["result"] == "SUCCESS" and second["result"] == "DUPLICATE" and second["success"] is False and second["error_message"] == "File already exists."
    assert second["document_id"] == first["document_id"] and second["duplicate_of"]["document_id"] == first["document_id"] and second["duplicate_of"]["original_filename"] == "ER-40001.pdf"
    assert count(ExpenseReport) == 1 and len(drive_files(serve)) == 1  # one canonical document, one canonical Drive file
    with SessionLocal() as db:
        attempts = db.execute(select(UploadAttempt).order_by(UploadAttempt.id)).scalars().all()
        assert [a.result for a in attempts] == ["SUCCESS", "DUPLICATE"]
        dup = attempts[1]
        assert dup.original_filename == "renamed-copy.pdf" and dup.file_size == len(content) and dup.uploaded_by_id and dup.sha256 == attempts[0].sha256
        assert (dup.canonical_kind, dup.canonical_pk) == ("expense", int(first["document_id"].split(":")[1])) and dup.duplicate_reason and dup.retained_copy is False
        assert dup.client_id and dup.document_type == "EXPENSE_REPORT"
    assert drive_files(serve, "Duplicates") == []  # default policy: metadata only (no second PDF is stored)
    history = client.get("/api/uploads/attempts?result=DUPLICATE").json()
    assert len(history) == 1 and history[0]["duplicate_of"]["document_id"] == first["document_id"] and history[0]["original_filename"] == "renamed-copy.pdf"
    assert next(i for i in client.get("/api/uploads").json() if i["document_id"] == first["document_id"])["duplicate_count"] == 1


def test_duplicate_retention_copy_keeps_the_bytes_in_the_duplicates_folder_without_touching_the_original(client, serve, monkeypatch):
    monkeypatch.setenv("SPINCO_DUPLICATE_RETENTION", "copy")
    content = pdf_bytes("dup-copy")
    first = upload(client, content)
    canonical_before = [(f["id"], f["name"], tuple(f["parents"]), f["content"]) for f in drive_files(serve, "Expense Reports")]
    second = upload(client, content, "again.pdf")
    third = upload(client, content, "third.pdf")
    assert second["result"] == third["result"] == "DUPLICATE" and count(ExpenseReport) == 1
    assert [(f["id"], f["name"], tuple(f["parents"]), f["content"]) for f in drive_files(serve, "Expense Reports")] == canonical_before  # never moved, renamed or overwritten
    retained = drive_files(serve, "Duplicates")
    assert len(retained) == 1 and retained[0]["content"] == content  # one retained copy per distinct bytes, however often they are re-sent
    with SessionLocal() as db:
        attempts = db.execute(select(UploadAttempt).where(UploadAttempt.result == "DUPLICATE")).scalars().all()
        assert len(attempts) == 2 and all(a.retained_copy and a.storage_record_id for a in attempts) and attempts[0].storage_record_id == attempts[1].storage_record_id
        retained_record = db.get(DocumentStorageRecord, attempts[0].storage_record_id)
        canonical_record = db.execute(select(DocumentStorageRecord).where(DocumentStorageRecord.kind == "expense_report")).scalar_one()
        assert retained_record.kind == "duplicate" and retained_record.id != canonical_record.id and attempts[0].canonical_pk == int(first["document_id"].split(":")[1])


def test_the_same_file_name_with_different_bytes_is_not_a_duplicate(client, serve):
    a = upload(client, pdf_bytes("name-a"), "ER-50001.pdf")
    b = upload(client, pdf_bytes("name-b"), "ER-50001.pdf")
    assert a["result"] == b["result"] == "SUCCESS" and a["document_id"] != b["document_id"]
    assert count(ExpenseReport) == 2 and len(drive_files(serve, "Expense Reports")) == 2 and count(UploadAttempt, UploadAttempt.result == "DUPLICATE") == 0


def test_an_invalid_file_is_recorded_as_a_failed_attempt(client, serve):
    result = upload(client, b"this is not a pdf", "ER-60001.pdf")
    assert result["success"] is False and result["result"] == "FAILED" and result["attempt_id"]
    with SessionLocal() as db:
        attempt = db.execute(select(UploadAttempt)).scalar_one()
        assert attempt.result == "FAILED" and attempt.sha256 is None and attempt.original_filename == "ER-60001.pdf" and attempt.error_message
    assert count(ExpenseReport) == 0 and drive_files(serve) == []


def test_duplicate_history_survives_a_restart(client, serve):
    content = pdf_bytes("restart")
    upload(client, content)
    upload(client, content, "dup.pdf")
    engine.dispose()  # every pooled connection is dropped, as a process restart would
    history = client.get("/api/uploads/attempts?result=DUPLICATE").json()
    assert len(history) == 1 and history[0]["original_filename"] == "dup.pdf" and history[0]["duplicate_of"]["original_filename"] == "ER-10001.pdf"
    import sqlite3

    con = sqlite3.connect(engine.url.database)  # and it is really in the database file, not in memory
    try:
        assert con.execute("select count(*) from upload_attempts where result = 'DUPLICATE'").fetchone()[0] == 1
    finally:
        con.close()


# -------------------------------------------------------------------------------------------------------------------------------------- timestamps


def test_the_upload_time_is_the_servers_timezone_aware_utc_time_with_an_ist_display_value(client, serve):
    before = datetime.now(timezone.utc) - timedelta(seconds=2)
    result = upload(client, pdf_bytes("clock"))
    after = datetime.now(timezone.utc) + timedelta(seconds=2)
    moment = datetime.fromisoformat(result["uploaded_at"])
    assert moment.tzinfo is not None and moment.utcoffset() == timedelta(0) and before <= moment <= after
    ist = datetime.fromisoformat(result["uploaded_at_ist"])
    assert ist.utcoffset() == timedelta(hours=5, minutes=30) and ist == moment
    attempt = client.get("/api/uploads/attempts").json()[0]
    assert datetime.fromisoformat(attempt["uploaded_at"]).utcoffset() == timedelta(0) and attempt["uploaded_at_ist"].endswith("+05:30")
    item = client.get("/api/uploads").json()[0]
    assert item["uploaded_at_ist"].endswith("+05:30") and datetime.fromisoformat(item["uploaded_at_ist"]) == datetime.fromisoformat(item["uploaded_at"])


def test_ist_conversion_treats_a_naive_database_value_as_utc():
    assert upload_history.to_ist_iso(datetime(2026, 11, 6, 0, 0)) == "2026-11-06T05:30:00+05:30"
    assert upload_history.to_ist_iso(datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)) == "2026-11-07T01:30:00+05:30"
    assert upload_history.to_ist_iso(None) is None


# ------------------------------------------------------------------------------------------------------------------------- two PCs, same file


def test_two_pcs_uploading_the_same_file_at_the_same_moment_converge_on_one_canonical_document(client, serve):
    content = pdf_bytes("race")
    results = {}

    def one_pc(name):
        with SessionLocal() as db:
            user = db.execute(select(User)).scalars().first()
            upload_file = UploadFile(file=BytesIO(content), filename=name)
            from app.api import uploads

            results[name] = uploads._process_one(db, upload_file, expense=True, user=user, background_tasks=BackgroundTasks(), client_id=f"pc-{name}")

    threads = [threading.Thread(target=one_pc, args=(n,)) for n in ("pc1.pdf", "pc2.pdf")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(r.result for r in results.values()) == ["DUPLICATE", "SUCCESS"]  # never two successes, never a false failure
    assert count(ExpenseReport) == 1 and len(drive_files(serve, "Expense Reports")) == 1 and count(DocumentStorageRecord) == 1
    with SessionLocal() as db:
        attempts = db.execute(select(UploadAttempt).order_by(UploadAttempt.id)).scalars().all()
        assert sorted(a.result for a in attempts) == ["DUPLICATE", "SUCCESS"] and {a.client_id for a in attempts} == {"pc-pc1.pdf", "pc-pc2.pdf"}
        success, duplicate = (next(a for a in attempts if a.result == r) for r in ("SUCCESS", "DUPLICATE"))
        assert duplicate.canonical_pk == success.canonical_pk and duplicate.sha256 == success.sha256


def test_a_second_pc_sees_the_same_document_without_a_second_upload(client, serve):
    content = pdf_bytes("two-pcs")
    doc = upload(client, content)["document_id"]  # PC 1
    pc2 = client  # PC 2 is just another signed-in client of the same backend and database
    assert [i["document_id"] for i in pc2.get("/api/uploads").json()] == [doc]
    assert pc2.get(f"/api/documents/{doc}/file").content == content  # it previews/downloads the verified central copy
    assert pc2.post("/api/uploads/expense-reports", files=[("files", ("x.pdf", content, "application/pdf"))]).json()["results"][0]["result"] == "DUPLICATE"
    assert len(drive_files(serve)) == 1


# ------------------------------------------------------------------------------------------------------------------------------------- shadow


def test_shadow_mode_keeps_local_authoritative_and_copies_to_drive_by_a_durable_job(client, shadow):
    content = pdf_bytes("shadow")
    result = upload(client, content)
    assert result["result"] == "SUCCESS" and result["storage_state"] == "PENDING" and drive_files(shadow) == []  # the local upload is complete; Drive is not touched yet
    assert client.get(f"/api/documents/{result['document_id']}/file").content == content  # local copy is what is served in shadow mode
    with SessionLocal() as db:
        assert {j.job_type for j in db.execute(select(ProcessingJob)).scalars()} == {EXTRACT, SHADOW_COPY}
    processing_jobs.run_pending_jobs_once()
    assert len(drive_files(shadow, "Expense Reports")) == 1
    listed = client.get("/api/uploads").json()[0]
    assert listed["storage_state"] == "STORED"
    with SessionLocal() as db:
        assert db.execute(select(ProcessingJob).where(ProcessingJob.job_type == SHADOW_COPY)).scalar_one().state == "DONE"
    processing_jobs.run_pending_jobs_once()
    assert len(drive_files(shadow)) == 1  # nothing is uploaded twice


def test_shadow_mode_a_drive_failure_never_fails_the_local_upload_and_the_copy_is_retried(client, shadow):
    shadow.fake.fail_next(times=1000, status=503)
    result = upload(client, pdf_bytes("shadow-down"))
    assert result["result"] == "SUCCESS" and count(ExpenseReport) == 1
    processing_jobs.run_pending_jobs_once()
    with SessionLocal() as db:
        job = db.execute(select(ProcessingJob).where(ProcessingJob.job_type == SHADOW_COPY)).scalar_one()
        assert job.state == "DEFERRED" and job.next_attempt_at is not None and job.failure_class == "RETRYABLE"
        job.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)  # the wait is over
        db.commit()
    assert client.get("/api/uploads").json()[0]["storage_state"] in ("PENDING", "NOT_CENTRAL")  # shown honestly: not stored yet
    shadow.fake.faults.clear()
    processing_jobs.run_pending_jobs_once()
    assert len(drive_files(shadow)) == 1 and client.get("/api/uploads").json()[0]["storage_state"] == "STORED"
    processing_jobs.run_pending_jobs_once()
    assert len(drive_files(shadow)) == 1  # retries never duplicate the Drive file


# ------------------------------------------------------------------------------------------------------------------------------------- security


def test_the_admin_storage_views_are_not_available_to_ordinary_users(client, serve):
    upload(client, pdf_bytes("sec"))
    assert client.get("/api/storage/jobs").status_code == 200 and client.get("/api/storage/records?state=all").status_code == 200
    admin_record = client.get("/api/storage/records?state=all").json()[0]
    assert "attempts" in admin_record and admin_record["attempts"] is not None
    email = make_viewer(client)
    login(client, email, PASSWORD)
    assert client.get("/api/storage/jobs").status_code == 403
    assert client.post("/api/storage/jobs/1/retry").status_code == 403
    assert client.post("/api/storage/records/1/retry").status_code == 403
    assert client.get("/api/storage/records?state=all").status_code == 403
    active = client.get("/api/storage/records").json()  # a viewer may list ACTIVE documents, without any progress details or Drive internals
    assert active and active[0]["attempts"] is None and active[0]["failure_class"] is None and "location" not in active[0] and "sha256" not in active[0]
    assert client.get("/api/uploads/attempts").status_code == 200 and client.get("/api/uploads").status_code == 200  # but may see the upload/duplicate history
