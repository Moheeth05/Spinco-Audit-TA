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
from app.schemas.storage import StorageRecordOut, StorageStatusOut
from app.services.drive import config as drive_config
from app.services.drive.auth import DriveAuthError
from app.services.drive.client import DriveError, DriveNotFound
from app.services.drive.config import DriveConfigError
from app.services.drive.lifecycle import StorageManager, StorageStateError
from app.services.drive.runtime import get_storage_manager
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
            drive_config.load_config()
            configured = True
        except DriveConfigError as exc:
            error = str(exc)
    counts = {state: 0 for state in sr.ALL_STATES}
    for state, count in db.execute(select(DocumentStorageRecord.state, func.count()).group_by(DocumentStorageRecord.state)).all():
        counts[state] = count
    return StorageStatusOut(enabled=enabled, configured=configured, config_error=error, counts=counts)


@router.get("/records", response_model=list[StorageRecordOut])
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
    query = select(DocumentStorageRecord).order_by(DocumentStorageRecord.id.desc()).limit(500)
    if state != "all":
        query = query.where(DocumentStorageRecord.state == state)
    if kind:
        query = query.where(DocumentStorageRecord.kind == kind)
    return db.execute(query).scalars().all()


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
    """Streams the PDF (HTTP Range supported). Authorisation (above) and the state check happen BEFORE Drive is contacted."""
    record = _record_or_404(db, record_id)
    if record.state == sr.ARCHIVED:
        raise HTTPException(status_code=410, detail="This document has been archived.")
    if record.state == sr.MISSING_EXTERNAL:
        raise HTTPException(status_code=410, detail="This document is missing from central storage. Contact an administrator.")
    if record.state != sr.STORED:
        raise HTTPException(status_code=404, detail="Document not found.")
    context = manager.drive.open_stream(record.location, range_header=request.headers.get("range"))
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
