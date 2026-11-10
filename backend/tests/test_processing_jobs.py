"""Durable background processing (processing_jobs): claiming, no duplicate processing, restart recovery, heartbeat (a long job cannot lose its lease),
retryable-vs-permanent classification, the original PDF never lost, extraction through the central layer, and the minimal administrator retry."""

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.models.expense_report import ExpenseReport
from app.models.processing_job import DEFERRED, DONE, EXTRACT, FAILED, QUEUED, RUNNING, ProcessingJob
from app.services import central_storage, processing_jobs

from drive_testkit import pdf_bytes
from test_central_storage import PASSWORD, drive, drive_files, login, make_viewer, serve, upload  # noqa: F401  (drive and serve are pytest fixtures, used by name)


def naive_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def job_for(doc_id, job_type=EXTRACT):
    pk = int(doc_id.split(":")[1])
    with SessionLocal() as db:
        return db.execute(select(ProcessingJob).where(ProcessingJob.document_pk == pk, ProcessingJob.job_type == job_type)).scalar_one()


def make_job(pk=1, state=QUEUED, **fields):
    with SessionLocal() as db:
        job = ProcessingJob(document_kind="expense", document_pk=pk, job_type=EXTRACT, state=state, **fields)
        db.add(job)
        db.commit()
        return job.id


# ---------------------------------------------------------------------------------------------------------------------------------- enqueue


def test_enqueue_is_idempotent_and_requeue_only_touches_finished_jobs(client):
    with SessionLocal() as db:
        first = processing_jobs.enqueue(db, "expense", 7)
        second = processing_jobs.enqueue(db, "expense", 7)
        db.commit()
        assert first.id == second.id and db.execute(select(ProcessingJob)).scalars().all().__len__() == 1
        first.state = RUNNING
        assert processing_jobs.enqueue(db, "expense", 7, requeue=True).state == RUNNING  # a running job is never reset underneath its worker
        first.state, first.attempts, first.last_error = FAILED, 5, "x"
        again = processing_jobs.enqueue(db, "expense", 7, requeue=True)
        assert again.state == QUEUED and again.attempts == 0 and again.last_error is None


def test_two_workers_never_claim_the_same_job(client):
    ids = [make_job(pk=n) for n in range(1, 4)]
    claimed, lock = [], threading.Lock()

    def worker():
        for _ in range(5):
            with SessionLocal() as db:
                got = processing_jobs.claim_next(db)
            if got:
                with lock:
                    claimed.append(got[0])

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(claimed) == sorted(ids)  # every job claimed exactly once


def test_a_running_job_with_a_live_lease_is_not_claimable_but_an_expired_one_is_the_restart_recovery(client):
    job_id = make_job(state=RUNNING, lease_owner="dead-worker", lease_expires_at=naive_now() + timedelta(seconds=60), attempts=1)
    with SessionLocal() as db:
        assert processing_jobs.claim_next(db) is None  # the owner may still be alive
    with SessionLocal() as db:
        db.get(ProcessingJob, job_id).lease_expires_at = naive_now() - timedelta(seconds=1)  # the worker died / the server restarted
        db.commit()
    with SessionLocal() as db:
        claimed = processing_jobs.claim_next(db)
        assert claimed is not None and claimed[0] == job_id
        job = db.get(ProcessingJob, job_id)
        assert job.lease_owner == claimed[1] != "dead-worker" and job.attempts == 2


def test_a_deferred_job_waits_for_its_schedule(client):
    job_id = make_job(state=DEFERRED, next_attempt_at=naive_now() + timedelta(minutes=5))
    with SessionLocal() as db:
        assert processing_jobs.claim_next(db) is None
        db.get(ProcessingJob, job_id).next_attempt_at = naive_now() - timedelta(seconds=1)
        db.commit()
    with SessionLocal() as db:
        assert processing_jobs.claim_next(db)[0] == job_id


def test_a_worker_that_lost_its_lease_cannot_overwrite_the_new_holders_result(client):
    job_id = make_job()
    with SessionLocal() as db:
        _, old_owner = processing_jobs.claim_next(db, lease_seconds=60)
    with SessionLocal() as db:
        job = db.get(ProcessingJob, job_id)
        job.lease_owner, job.lease_expires_at = "new-owner", naive_now() + timedelta(seconds=60)  # taken over
        db.commit()
    with SessionLocal() as db:
        processing_jobs._settle(db, job_id, old_owner, state=FAILED, error="late")
    with SessionLocal() as db:
        job = db.get(ProcessingJob, job_id)
        assert job.state == RUNNING and job.lease_owner == "new-owner" and job.last_error is None


# ------------------------------------------------------------------------------------------------------------------------------- heartbeat (F12)


def test_the_heartbeat_keeps_the_lease_of_a_long_running_job_alive(client):
    job_id = make_job()
    with SessionLocal() as db:
        _, owner = processing_jobs.claim_next(db, lease_seconds=0.9)
    with processing_jobs._Heartbeat(job_id, owner, 0.9, SessionLocal):
        time.sleep(2.4)  # far beyond the lease: without beats it would have expired twice over
        with SessionLocal() as db:
            assert processing_jobs.claim_next(db) is None  # nobody can take it over while it is genuinely running
    time.sleep(1.2)  # the holder stopped (finished or died): the lease now runs out by itself
    with SessionLocal() as db:
        assert processing_jobs.claim_next(db)[0] == job_id


# ---------------------------------------------------------------------------------------------------------------------- running real documents


def test_the_worker_extracts_through_the_central_layer_and_rebuilds_a_missing_local_copy(client, serve):
    content = pdf_bytes("job-1")
    doc = upload(client, content)["document_id"]
    pk = int(doc.split(":")[1])
    with SessionLocal() as db:
        row = db.get(ExpenseReport, pk)
        local = row.source_path
    import os

    os.remove(local)  # this machine does not hold the working copy (e.g. it is a different backend host)
    assert processing_jobs.run_pending_jobs_once() == 1
    assert job_for(doc).state == DONE
    with SessionLocal() as db:
        row = db.get(ExpenseReport, pk)
        assert row.extraction_status != "NOT_PROCESSED"  # extraction really ran on the PDF fetched from Drive
        from pathlib import Path

        assert Path(row.source_path).read_bytes() == content  # the working copy was rebuilt from the verified central copy
    assert client.get("/api/uploads").json()[0]["processing_state"] == "DONE"


def test_a_corrupt_working_copy_is_replaced_by_the_verified_central_copy(client, serve):
    content = pdf_bytes("job-2")
    doc = upload(client, content)["document_id"]
    with SessionLocal() as db:
        row = db.get(ExpenseReport, int(doc.split(":")[1]))
        from pathlib import Path

        Path(row.source_path).write_bytes(b"%PDF-garbage")  # a damaged local cache must never be processed
        path = central_storage.local_file_for(db, "expense", row)
        assert path.read_bytes() == content


def test_central_storage_unavailable_defers_the_job_and_recovery_completes_it(client, serve):
    doc = upload(client, pdf_bytes("job-3"))["document_id"]
    with SessionLocal() as db:
        row = db.get(ExpenseReport, int(doc.split(":")[1]))
        import os

        os.remove(row.source_path)
    serve.fake.fail_next(times=1000, status=503)  # Drive outage while the worker needs the PDF
    processing_jobs.run_pending_jobs_once()
    job = job_for(doc)
    assert job.state == DEFERRED and job.failure_class == "RETRYABLE" and job.next_attempt_at > naive_now()
    with SessionLocal() as db:
        row = db.get(ExpenseReport, int(doc.split(":")[1]))
        assert row.extraction_status == "NOT_PROCESSED" and "Waiting for central storage" in row.extraction_notes  # shown honestly, not "FAILED"
    assert len(drive_files(serve)) == 1  # the original is untouched
    serve.fake.faults.clear()
    with SessionLocal() as db:
        db.get(ProcessingJob, job.id).next_attempt_at = naive_now() - timedelta(seconds=1)
        db.commit()
    processing_jobs.run_pending_jobs_once()
    assert job_for(doc).state == DONE


def test_a_permanent_problem_fails_the_job_and_never_loses_the_original(client, serve, monkeypatch):
    doc = upload(client, pdf_bytes("job-4"))["document_id"]

    def gone(db, kind, row):
        raise central_storage.CentralUnavailable("The central copy of this document could not be found.", status=404, retryable=False)

    monkeypatch.setattr(central_storage, "local_file_for", gone)
    processing_jobs.run_pending_jobs_once()
    job = job_for(doc)
    assert job.state == FAILED and job.failure_class == "PERMANENT"
    assert len(drive_files(serve)) == 1 and client.get(f"/api/documents/{doc}/file").status_code == 200  # the original PDF is still served
    pk = int(doc.split(":")[1])
    with SessionLocal() as db:
        assert db.get(ExpenseReport, pk).extraction_status == "FAILED"


def test_a_persistently_retryable_job_is_eventually_reported_failed_not_retried_forever(client, serve, monkeypatch):
    doc = upload(client, pdf_bytes("job-5"))["document_id"]

    def down(db, kind, row):
        raise central_storage.CentralUnavailable("down", status=503, retryable=True)

    monkeypatch.setattr(central_storage, "local_file_for", down)
    for _ in range(processing_jobs.MAX_ATTEMPTS + 1):
        with SessionLocal() as db:
            job = db.get(ProcessingJob, job_for(doc).id)
            job.next_attempt_at = naive_now() - timedelta(seconds=1)
            db.commit()
        processing_jobs.run_pending_jobs_once()
    job = job_for(doc)
    assert job.state == FAILED and job.failure_class == "EXHAUSTED" and job.attempts == processing_jobs.MAX_ATTEMPTS


def test_an_unexpected_error_never_stops_the_worker(client, serve, monkeypatch):
    docs = [upload(client, pdf_bytes(f"job-6{n}"), f"ER-7000{n}.pdf")["document_id"] for n in range(2)]
    original = processing_jobs._do_extract
    calls = []

    def flaky(db, job):
        calls.append(job.id)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return original(db, job)

    monkeypatch.setattr(processing_jobs, "_do_extract", flaky)
    assert processing_jobs.run_pending_jobs_once() == 2  # the second job ran although the first raised
    states = sorted(job_for(d).state for d in docs)
    assert states == [DEFERRED, DONE]


def test_a_job_for_a_deleted_document_finishes_quietly(client, serve):
    doc = upload(client, pdf_bytes("job-7"))["document_id"]
    assert client.delete(f"/api/documents/{doc}").status_code == 204
    assert processing_jobs.run_pending_jobs_once() == 1
    assert job_for(doc).state == DONE


def test_worker_start_and_stop_are_idempotent(client, monkeypatch):
    monkeypatch.setenv("SPINCO_CENTRAL_STORAGE_MODE", "serve")
    assert processing_jobs.worker_enabled() is True
    monkeypatch.setenv("SPINCO_JOB_WORKER", "off")
    assert processing_jobs.worker_enabled() is False
    monkeypatch.delenv("SPINCO_CENTRAL_STORAGE_MODE")
    monkeypatch.delenv("SPINCO_JOB_WORKER")
    assert processing_jobs.worker_enabled() is False  # off mode starts no worker
    processing_jobs.start_worker(poll_seconds=0.05)
    processing_jobs.start_worker(poll_seconds=0.05)
    assert processing_jobs._worker is not None
    processing_jobs.stop_worker()
    processing_jobs.stop_worker()
    assert processing_jobs._worker is None


def test_a_running_worker_thread_processes_queued_jobs_by_itself(client, serve):
    doc = upload(client, pdf_bytes("job-8"))["document_id"]
    processing_jobs.start_worker(poll_seconds=0.1)
    try:
        deadline = time.time() + 30
        while time.time() < deadline and job_for(doc).state != DONE:
            time.sleep(0.2)
    finally:
        processing_jobs.stop_worker()
    assert job_for(doc).state == DONE


# --------------------------------------------------------------------------------------------------------------------------------- admin retry


def test_an_administrator_can_requeue_a_failed_job_and_an_ordinary_user_cannot(client, serve):
    doc = upload(client, pdf_bytes("job-9"))["document_id"]
    job = job_for(doc)
    with SessionLocal() as db:
        db.get(ProcessingJob, job.id).state = FAILED
        db.commit()
    listed = client.get("/api/storage/jobs?state=failed").json()
    assert [j["id"] for j in listed] == [job.id] and listed[0]["document_id"] == doc
    email = make_viewer(client)
    login(client, email, PASSWORD)
    assert client.post(f"/api/storage/jobs/{job.id}/retry").status_code == 403
    login(client, "intern@spincotech.com", "ChangeMe123!")
    retried = client.post(f"/api/storage/jobs/{job.id}/retry")
    assert retried.status_code == 200 and retried.json()["state"] == "QUEUED"
    assert client.post(f"/api/storage/jobs/{job.id}/retry").status_code == 409  # already queued: nothing to retry
    assert client.post("/api/storage/jobs/99999/retry").status_code == 404
    audit = client.get("/api/audit-logs").json()
    assert any(entry["action"] == "STORAGE_JOB_RETRIED" for entry in (audit["items"] if isinstance(audit, dict) else audit))
