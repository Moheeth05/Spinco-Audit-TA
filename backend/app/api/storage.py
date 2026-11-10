"""Admin-facing storage operations for documents held in Google Drive (archive, restore, reconcile, authorised streaming).

Disabled (503) unless SPINCO_GDRIVE_ENABLED=true and a valid test configuration exists. Authorisation always happens BEFORE any Drive
call; responses never contain a Drive id, URL or credential; errors are generic (no stack traces, no Google response bodies).
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import storage_record as sr
from app.models.storage_record import DocumentStorageRecord
from app.models.user import User
from datetime import datetime, timezone

from app.models import processing_job as pj
from app.models.processing_job import ProcessingJob
from app.schemas.storage import ProcessingJobOut, StorageRecordAdminOut, StorageRecordOut, StorageStatusOut
from app.services import audit, processing_jobs
from app.services.drive import failures as drive_failures
from app.services.drive import config as drive_config
from app.services.drive.auth import DriveAuthError
from app.services.drive.client import DriveError, DriveIntegrityError, DriveNotFound
from app.services.drive.config import DriveConfigError
from app.services.drive.guard import DriveTargetError
from app.services.drive.lifecycle import StorageManager, StorageStateError, StorageUploadError
from app.services.drive import schema as drive_schema
from app.services.drive.runtime import check_schema_ready, get_storage_manager
from app.services.permissions import (
    DELETE_DOCUMENTS,
    MANAGE_STORAGE,
    RESTORE_DOCUMENTS,
    ROLE_PERMISSIONS,
    VIEW_AUDIT_LOGS,
    VIEW_DOCUMENTS,
    get_current_user,
    require_permission,
)

router = APIRouter(prefix="/api/storage", tags=["storage"])


def _drive_failure(exc: Exception) -> HTTPException:
    if isinstance(exc, (DriveAuthError, DriveConfigError)):
        return HTTPException(status_code=503, detail="Drive storage is unavailable (authentication or configuration).")
    if isinstance(exc, DriveTargetError):  # the destination is not verified: nothing was written. The reason code is for the log, not the client.
        if not exc.retryable:  # an existing object outside the expected Shared Drive was refused (never adopted)
            return HTTPException(status_code=409, detail="Drive storage refused an existing object that is not in the expected Shared Drive.")
        return HTTPException(status_code=503, detail="Drive storage refused the write: the destination could not be verified.")
    if isinstance(exc, DriveIntegrityError):  # the document did not verify against its recorded checksum / size / identity: nothing was served
        return HTTPException(status_code=502, detail="The stored document could not be verified and was not served.")
    if isinstance(exc, DriveNotFound):
        return HTTPException(status_code=404, detail="The stored file could not be found in Drive.")
    retryable = isinstance(exc, DriveError) and exc.retryable
    return HTTPException(status_code=503 if retryable else 502, detail="Drive storage did not complete the request.")


def _record_or_404(db: Session, record_id: int) -> DocumentStorageRecord:
    record = db.get(DocumentStorageRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return record


@router.get("/status", response_model=StorageStatusOut)
def storage_status(db: Session = Depends(get_db), _user: User = Depends(require_permission(VIEW_AUDIT_LOGS))):
    enabled = drive_config.is_enabled()
    configured, error = False, None
    if enabled:
        try:
            drive_config.load_runtime_config()  # includes the mandatory environment and expected Shared Drive id
            configured = True
        except DriveConfigError as exc:
            error = str(exc)
    counts = {state: 0 for state in sr.ALL_STATES}
    for state, count in db.execute(select(DocumentStorageRecord.state, func.count()).group_by(DocumentStorageRecord.state)).all():  # touches `state` only: safe on an older shape
        counts[state] = count
    report = drive_schema.schema_report(db.get_bind())  # read-only; says whether the migration has been applied, never applies it
    return StorageStatusOut(enabled=enabled, configured=configured, config_error=error, counts=counts, schema_ok=report.ready, schema_status=report.status)


def _ordinary_view(record: DocumentStorageRecord) -> StorageRecordAdminOut:
    """Only the base fields are copied: the admin-only progress fields stay None however the record looks (from_attributes would otherwise pick them up)."""
    return StorageRecordAdminOut(**StorageRecordOut.model_validate(record).model_dump())


def _admin_view(record: DocumentStorageRecord) -> StorageRecordAdminOut:
    out = _ordinary_view(record)
    out.failure_class, out.attempts, out.deferrals, out.next_attempt_at = record.failure_class, record.attempts, record.deferrals, record.next_attempt_at
    out.needs_attention = drive_failures.needs_attention(record, datetime.now(timezone.utc))
    return out


@router.get("/records", response_model=list[StorageRecordAdminOut])
def list_records(state: str = sr.STORED, kind: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Active documents (STORED) are visible to anyone who may view documents. Archived / missing / failed ones - which are hidden
    from the active listing - are visible only to administrators."""
    permissions = ROLE_PERMISSIONS.get(user.role, set())
    if state not in sr.ALL_STATES and state != "all":
        raise HTTPException(status_code=400, detail="Unknown state.")
    if state == sr.STORED:
        if VIEW_DOCUMENTS not in permissions:
            raise HTTPException(status_code=403, detail="You do not have permission to perform this action.")
    elif VIEW_AUDIT_LOGS not in permissions:
        raise HTTPException(status_code=403, detail="You do not have permission to perform this action.")
    check_schema_ready(detailed=VIEW_AUDIT_LOGS in permissions)  # only AFTER authentication and authorisation, and only an administrator is told why
    query = select(DocumentStorageRecord).order_by(DocumentStorageRecord.id.desc()).limit(500)
    if state != "all":
        query = query.where(DocumentStorageRecord.state == state)
    if kind:
        query = query.where(DocumentStorageRecord.kind == kind)
    records = db.execute(query).scalars().all()
    if VIEW_AUDIT_LOGS in permissions:
        return [_admin_view(r) for r in records]
    return [_ordinary_view(r) for r in records]  # progress details (failure class, attempts, schedule) are for administrators only


@router.post("/records/{record_id}/archive", response_model=StorageRecordOut)
def archive_record(record_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission(DELETE_DOCUMENTS)), manager: StorageManager = Depends(get_storage_manager)):
    _record_or_404(db, record_id)
    try:
        return manager.archive(db, record_id, user_id=user.id)
    except StorageStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (DriveError, DriveAuthError) as exc:
        raise _drive_failure(exc) from None


@router.post("/records/{record_id}/restore", response_model=StorageRecordOut)
def restore_record(record_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission(RESTORE_DOCUMENTS)), manager: StorageManager = Depends(get_storage_manager)):
    _record_or_404(db, record_id)
    try:
        return manager.restore(db, record_id, user_id=user.id)
    except StorageStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (DriveError, DriveAuthError) as exc:
        raise _drive_failure(exc) from None


@router.post("/records/{record_id}/retry", response_model=StorageRecordAdminOut)
def retry_record(record_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission(MANAGE_STORAGE)), manager: StorageManager = Depends(get_storage_manager)):
    """An administrator's audited retry of an upload that is not progressing (failed, or waiting). Never available to ordinary users."""
    _record_or_404(db, record_id)
    try:
        return _admin_view(manager.retry_upload(db, record_id, user_id=user.id))
    except StorageStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except StorageUploadError as exc:
        if exc.record is not None:
            return _admin_view(exc.record)  # the retry was attempted and the record shows where it stands
        raise HTTPException(status_code=503, detail="Drive storage did not complete the request.") from None
    except (DriveError, DriveAuthError) as exc:
        raise _drive_failure(exc) from None


@router.get("/jobs", response_model=list[ProcessingJobOut])
def list_jobs(state: str | None = None, db: Session = Depends(get_db), _user: User = Depends(require_permission(MANAGE_STORAGE))):
    """Background processing jobs (extraction, shadow copy), newest first. Administrators only."""
    query = select(ProcessingJob).order_by(ProcessingJob.id.desc()).limit(500)
    if state:
        query = query.where(ProcessingJob.state == state.upper())
    return [ProcessingJobOut(id=j.id, document_kind=j.document_kind, document_id=f"{j.document_kind}:{j.document_pk}", job_type=j.job_type, state=j.state, attempts=j.attempts,
                             failure_class=j.failure_class, last_error=j.last_error, next_attempt_at=j.next_attempt_at, updated_at=j.updated_at) for j in db.execute(query).scalars()]


@router.post("/jobs/{job_id}/retry", response_model=ProcessingJobOut)
def retry_job(job_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission(MANAGE_STORAGE))):
    """Re-queues a FAILED or finished job (the document's PDF is untouched). A job that is running or waiting is refused with 409 (it is already being handled)."""
    job = db.get(ProcessingJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.state not in (pj.FAILED, pj.DONE):
        raise HTTPException(status_code=409, detail="This job is already queued, running or waiting for its next attempt.")
    processing_jobs.enqueue(db, job.document_kind, job.document_pk, job.job_type, requeue=True)
    audit.log_action(db, user_id=user.id, action=audit.STORAGE_JOB_RETRIED, entity_type="ProcessingJob", entity_id=str(job.id), details=f"{job.job_type} {job.document_kind}:{job.document_pk}")
    db.commit()
    db.refresh(job)
    processing_jobs.wake_worker()
    return ProcessingJobOut(id=job.id, document_kind=job.document_kind, document_id=f"{job.document_kind}:{job.document_pk}", job_type=job.job_type, state=job.state, attempts=job.attempts,
                            failure_class=job.failure_class, last_error=job.last_error, next_attempt_at=job.next_attempt_at, updated_at=job.updated_at)


@router.post("/reconcile")
def reconcile(db: Session = Depends(get_db), _user: User = Depends(require_permission(MANAGE_STORAGE)), manager: StorageManager = Depends(get_storage_manager)):
    try:
        return manager.reconcile(db).as_dict()
    except (DriveError, DriveAuthError) as exc:
        raise _drive_failure(exc) from None


@router.post("/sync")
def sync_changes(db: Session = Depends(get_db), _user: User = Depends(require_permission(MANAGE_STORAGE)), manager: StorageManager = Depends(get_storage_manager)):
    try:
        report = manager.sync_changes(db)
    except (DriveError, DriveAuthError) as exc:
        raise _drive_failure(exc) from None
    return {"baseline": report.baseline, "changes_seen": report.changes_seen, "tracked_changes": report.tracked_changes, "token_expired": report.token_expired, "reconcile": report.reconcile}


@router.get("/records/{record_id}/file")
def stream_record_file(record_id: int, request: Request, db: Session = Depends(get_db), _user: User = Depends(require_permission(VIEW_DOCUMENTS)), manager: StorageManager = Depends(get_storage_manager)):
    """Serves the PDF (HTTP Range supported). Authorisation (above) and the state check happen BEFORE Drive is contacted. The document is downloaded and
    VERIFIED (identity, recorded checksum and size) before the first byte is sent; a document that fails verification is refused with a generic error."""
    record = _record_or_404(db, record_id)
    if record.state == sr.ARCHIVED:
        raise HTTPException(status_code=410, detail="This document has been archived.")
    if record.state == sr.MISSING_EXTERNAL:
        raise HTTPException(status_code=410, detail="This document is missing from central storage. Contact an administrator.")
    if record.state != sr.STORED:
        raise HTTPException(status_code=404, detail="Document not found.")
    context = manager.drive.open_stream(record.location, expected_md5=record.md5, expected_size=record.size, range_header=request.headers.get("range"))
    try:
        download = context.__enter__()
    except (DriveError, DriveAuthError) as exc:
        raise _drive_failure(exc) from None

    def body():
        try:
            yield from download.iter_bytes()
        finally:
            context.__exit__(None, None, None)

    headers = {"Accept-Ranges": "bytes", "Content-Disposition": "inline", "Cache-Control": "no-store"}
    for name in ("Content-Range", "Content-Length"):
        if name.lower() in {k.lower() for k in download.headers}:
            headers[name] = next(v for k, v in download.headers.items() if k.lower() == name.lower())
    return StreamingResponse(body(), status_code=download.status, media_type="application/pdf", headers=headers)
