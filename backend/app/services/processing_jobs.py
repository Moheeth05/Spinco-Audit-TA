"""Durable background work for centrally stored documents (table `processing_jobs`).

Replaces "FastAPI BackgroundTasks" for the central modes. A job is a database row, so it survives a restart; it is claimed with the same compare-and-swap lease the
Drive uploads use (one UPDATE ... WHERE the row is still claimable; exactly one claimant sees rowcount 1), kept alive by a heartbeat while it runs (a long OCR run can
never lose its lease to a second worker - F12), and settled as DONE, DEFERRED (try again later: outage, sign-in problem) or FAILED (permanent). The original PDF is
never touched by a failing job.

  EXTRACT       run the existing OCR / extraction / audit pipeline (run_extraction) on a document; its PDF is fetched through the central storage layer.
  SHADOW_COPY   mode `shadow`: send the local original to Google Drive (idempotent: the same bytes never create a second Drive file).

`run_pending_jobs_once()` processes what is due and returns; `start_worker()` runs it on a daemon thread (the application starts it in the central modes; tests call
`run_pending_jobs_once` directly so nothing runs behind their back).
"""

import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.expense_report import ExpenseReport
from app.models.processing_job import DEFERRED, DONE, EXTRACT, FAILED, QUEUED, RUNNING, ProcessingJob
from app.models.trip import TripReport
from app.services import central_storage
from app.services.document_ref import EXPENSE

log = logging.getLogger("app.processing_jobs")

LEASE_SECONDS = 120.0  # short on purpose: the heartbeat keeps a LIVE job's lease alive, so a dead worker's job is recovered within about two minutes
MAX_ATTEMPTS = 8  # claims of one job before a persistent (retryable) problem is reported as FAILED for an administrator
BACKOFF_BASE_SECONDS = 30.0
BACKOFF_MAX_SECONDS = 1800.0
POLL_SECONDS = 5.0
WORKER_ENV = "SPINCO_JOB_WORKER"  # "off" disables the background worker thread (tests; a dedicated worker process)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC, like every DateTime column in this schema


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0)), BACKOFF_MAX_SECONDS))


def _model(kind: str):
    return ExpenseReport if kind == EXPENSE else TripReport


# ------------------------------------------------------------------------------------------------------------------------------------------- creating


def enqueue(db: Session, kind: str, pk: int, job_type: str = EXTRACT, *, requeue: bool = False) -> ProcessingJob:
    """One job per (document, kind of work). Returns the existing job when there already is one (a retried upload never creates a second). With `requeue` a finished or
    failed job is put back to QUEUED (reprocess, administrator retry); a job that is running or waiting is left alone. The caller commits."""
    job = db.execute(select(ProcessingJob).where(ProcessingJob.document_kind == kind, ProcessingJob.document_pk == pk, ProcessingJob.job_type == job_type)).scalar_one_or_none()
    if job is None:
        job = ProcessingJob(document_kind=kind, document_pk=pk, job_type=job_type, state=QUEUED, attempts=0)
        try:
            with db.begin_nested():
                db.add(job)
                db.flush()
        except IntegrityError:  # two requests created it at the same moment: the unique constraint picked one
            job = db.execute(select(ProcessingJob).where(ProcessingJob.document_kind == kind, ProcessingJob.document_pk == pk, ProcessingJob.job_type == job_type)).scalar_one()
        return job
    if requeue and job.state in (DONE, FAILED):
        job.state, job.attempts, job.failure_class, job.last_error, job.next_attempt_at = QUEUED, 0, None, None, None
        job.lease_owner = job.lease_expires_at = None
    return job


# -------------------------------------------------------------------------------------------------------------------------------------------- claiming


def _claimable(now: datetime):
    return or_(
        ProcessingJob.state == QUEUED,
        (ProcessingJob.state == DEFERRED) & (or_(ProcessingJob.next_attempt_at.is_(None), ProcessingJob.next_attempt_at <= now)),
        (ProcessingJob.state == RUNNING) & (or_(ProcessingJob.lease_expires_at.is_(None), ProcessingJob.lease_expires_at <= now)),  # a worker that died or was restarted
    )


def claim_next(db: Session, *, lease_seconds: float = LEASE_SECONDS, owner: str | None = None) -> tuple[int, str] | None:
    """Atomically claims ONE due job -> (job id, owner token), or None. The claim is a single compare-and-swap UPDATE, so two workers (or two processes) can never both
    hold the same job; the loser simply tries the next candidate."""
    owner = owner or secrets.token_hex(8)
    now = _now()
    candidates = db.execute(select(ProcessingJob.id).where(_claimable(now)).order_by(ProcessingJob.id).limit(20)).scalars().all()
    for job_id in candidates:
        result = db.execute(
            update(ProcessingJob).where(ProcessingJob.id == job_id, _claimable(now))
            .values(state=RUNNING, lease_owner=owner, lease_expires_at=now + timedelta(seconds=lease_seconds), attempts=ProcessingJob.attempts + 1, updated_at=now)
        )
        db.commit()
        if result.rowcount == 1:
            return job_id, owner
    return None


class _Heartbeat:
    """While a job runs, extends its lease (owner-checked UPDATE on its OWN session) every third of a lease, so a long OCR run is never taken over; stops by itself when
    the lease is no longer this owner's."""

    def __init__(self, job_id: int, owner: str, lease_seconds: float, session_factory):
        self._job_id, self._owner, self._lease, self._factory = job_id, owner, lease_seconds, session_factory
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"job-heartbeat-{job_id}", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(max(self._lease / 3, 0.05)):
            try:
                with self._factory() as session:
                    now = _now()
                    result = session.execute(
                        update(ProcessingJob).where(ProcessingJob.id == self._job_id, ProcessingJob.lease_owner == self._owner, ProcessingJob.state == RUNNING)
                        .values(lease_expires_at=now + timedelta(seconds=self._lease))
                    )
                    session.commit()
                    if result.rowcount != 1:
                        return
            except Exception:  # noqa: BLE001 - a missed beat is retried on the next tick; the lease only expires if beats keep failing
                log.warning("Job heartbeat could not extend the lease of job %s", self._job_id)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        return False


# -------------------------------------------------------------------------------------------------------------------------------------------- running


def _settle(db: Session, job_id: int, owner: str, *, state: str, failure_class: str | None = None, error: str | None = None, retry_in: timedelta | None = None) -> None:
    """Owner-checked: a worker that lost its lease can never overwrite the new holder's result."""
    now = _now()
    db.execute(
        update(ProcessingJob).where(ProcessingJob.id == job_id, ProcessingJob.lease_owner == owner)
        .values(state=state, failure_class=failure_class, last_error=(error or None) and error[:500], next_attempt_at=(now + retry_in) if retry_in else None,
                lease_owner=None, lease_expires_at=None, updated_at=now)
    )
    db.commit()


def _do_extract(db: Session, job: ProcessingJob) -> tuple[str, str | None]:
    from app.services.extraction.pipeline import EXTRACTION_FAILED, EXTRACTION_OK, run_extraction

    if db.get(_model(job.document_kind), job.document_pk) is None:
        return "gone", "the document no longer exists"
    outcome = run_extraction(db, job.document_kind, job.document_pk)
    if outcome == EXTRACTION_OK:
        return "done", None
    if outcome == EXTRACTION_FAILED:
        return "failed", "extraction failed (see the processing log of the document)"
    return "retry", "central storage was not available"


def _do_shadow_copy(db: Session, job: ProcessingJob) -> tuple[str, str | None]:
    row = db.get(_model(job.document_kind), job.document_pk)
    if row is None:
        return "gone", "the document no longer exists"
    try:
        content = Path(row.source_path).read_bytes()
    except OSError:
        return "failed", "the local original could not be read"
    try:
        result = central_storage.store_document(db, job.document_kind, content, row.original_filename or "document.pdf", None)
    except central_storage.CentralUnavailable as exc:
        return ("retry" if exc.retryable else "failed"), exc.detail
    if result.label == central_storage.STORED:
        return "done", None
    return ("retry" if result.retryable else "failed"), result.message or "the copy to central storage is not complete yet"


def run_job(job_id: int, owner: str, *, session_factory=SessionLocal, lease_seconds: float = LEASE_SECONDS) -> str:
    """Runs one CLAIMED job to a settled state; returns the state it was settled in. Never raises (a worker must outlive any one job)."""
    with session_factory() as db:
        job = db.get(ProcessingJob, job_id)
        if job is None or job.lease_owner != owner:
            return "lost"
        attempts = job.attempts
        try:
            with _Heartbeat(job_id, owner, lease_seconds, session_factory):
                verdict, message = (_do_extract if job.job_type == EXTRACT else _do_shadow_copy)(db, job)
        except Exception as exc:  # noqa: BLE001 - one bad job must never stop the worker; its class (not its text) is stored
            db.rollback()
            log.exception("Job %s failed unexpectedly", job_id)
            verdict, message = "retry" if attempts < MAX_ATTEMPTS else "failed", f"unexpected {type(exc).__name__}"
        if verdict in ("done", "gone"):
            _settle(db, job_id, owner, state=DONE)
            return DONE
        if verdict == "retry" and attempts < MAX_ATTEMPTS:
            _settle(db, job_id, owner, state=DEFERRED, failure_class="RETRYABLE", error=message, retry_in=_backoff(attempts))
            return DEFERRED
        _settle(db, job_id, owner, state=FAILED, failure_class="PERMANENT" if verdict == "failed" else "EXHAUSTED", error=message)
        return FAILED


def run_pending_jobs_once(*, limit: int = 50, session_factory=SessionLocal, lease_seconds: float = LEASE_SECONDS) -> int:
    """Claims and runs due jobs one after another (at most `limit`); returns how many were run."""
    ran = 0
    while ran < limit:
        with session_factory() as db:
            claimed = claim_next(db, lease_seconds=lease_seconds)
        if claimed is None:
            break
        run_job(claimed[0], claimed[1], session_factory=session_factory, lease_seconds=lease_seconds)
        ran += 1
    return ran


# ---------------------------------------------------------------------------------------------------------------------------------------- the worker

_worker_lock = threading.Lock()
_worker: "_Worker | None" = None


class _Worker:
    def __init__(self, session_factory, poll_seconds: float):
        self._factory, self._poll = session_factory, poll_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="processing-job-worker", daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                run_pending_jobs_once(session_factory=self._factory)
            except Exception:  # noqa: BLE001 - the worker outlives a database hiccup
                log.exception("Job worker pass failed")
            self._wake.wait(self._poll)
            self._wake.clear()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.thread.join(timeout=10)


def worker_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(WORKER_ENV, "")).strip().lower() != "off" and central_storage.config(env).active


def start_worker(*, session_factory=SessionLocal, poll_seconds: float = POLL_SECONDS) -> bool:
    """Starts the background worker once (idempotent). Returns whether one is running."""
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = _Worker(session_factory, poll_seconds)
            _worker.thread.start()
        return True


def worker_running() -> bool:
    with _worker_lock:
        return _worker is not None and _worker.thread.is_alive()


def stop_worker() -> None:
    global _worker
    with _worker_lock:
        worker, _worker = _worker, None
    if worker is not None:
        worker.stop()


def wake_worker() -> None:
    with _worker_lock:
        if _worker is not None:
            _worker.wake()


# --------------------------------------------------------------------------------------------------------------------------------------- reporting


def state_for(db: Session, documents: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
    """{(kind, pk): QUEUED|RUNNING|DONE|DEFERRED|FAILED} for the EXTRACT jobs of the given documents, in one query. A document with no job is absent."""
    if not documents:
        return {}
    pks = {pk for _kind, pk in documents}
    rows = db.execute(select(ProcessingJob.document_kind, ProcessingJob.document_pk, ProcessingJob.state).where(ProcessingJob.job_type == EXTRACT, ProcessingJob.document_pk.in_(pks))).all()
    return {(kind, pk): state for kind, pk, state in rows}
