import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models.expense_report import ExpenseReport
from app.models.trip import TripReport
from app.models.upload_attempt import DEFERRED, DUPLICATE, FAILED, SUCCESS, UploadAttempt
from app.schemas.uploads import DuplicateOf, UploadAttemptItem, UploadBatchResponse, UploadListItem, UploadResultItem
from app.services.document_ref import EXPENSE, TRIP
from app.services.extraction.pipeline import run_extraction
from app.services.filename_ids import extract_er_id, extract_trip_id
from app.services import pdf_validation
from app.services.pdf_validation import PdfValidationError, validate_pdf_upload
from app.services.storage_backend import storage, storage_kind_for
from app.services import audit, central_storage, processing_jobs, ta_backend_forward, upload_history
from app.models.processing_job import EXTRACT, SHADOW_COPY
from app.models.user import User
from app.services.permissions import UPLOAD_DOCUMENTS, VIEW_DOCUMENTS, get_current_user, require_permission

router = APIRouter(tags=["uploads"])
logger = logging.getLogger(__name__)

DUPLICATE_REASON_HASH = "identical file content (SHA-256)"


def _run_extraction_in_background(kind: str, pk: int) -> None:
    """Runs on its own DB session (the request's session is closed by the
    time a background task executes - see FastAPI's BackgroundTasks docs),
    after the upload response has already been sent. This is what lets
    upload_expense_reports/upload_trip_reports return as soon as the file is
    validated and stored, instead of blocking on the full OCR/extraction
    pipeline - a real multi-file, multi-page batch upload was measured
    taking long enough synchronously that the app was closed before it ever
    finished (see ocr_engine.py's _WEAK_RESULT_CONFIDENCE comment for the
    other half of this fix). run_extraction already sets its own
    extraction_status=PROCESSING/EXTRACTED/FAILED on the row and never
    raises past its own try/except, but this is still wrapped defensively
    since an uncaught exception in a background task is otherwise silently
    swallowed by Starlette with no trace at all.

    Used when central storage is OFF. In the central modes the same work is a
    durable `processing_jobs` row instead (it survives a restart).
    """
    db = SessionLocal()
    try:
        run_extraction(db, kind, pk)
    except Exception:  # noqa: BLE001 - background task: must never crash the server, but must be visible
        logger.exception("Background extraction failed for %s:%s", kind, pk)
    finally:
        db.close()


def _try_record_attempt(db: Session, **kwargs) -> UploadAttempt | None:
    """The attempt history is a record, not a gate: if it cannot be written (a database problem), the upload is still answered truthfully and the problem is logged."""
    try:
        with db.begin_nested():
            return upload_history.record_attempt(db, **kwargs)
    except SQLAlchemyError:
        logger.exception("The upload attempt could not be recorded")
        return None


def _refusal(filename: str, *, result: str, message: str, attempt: UploadAttempt | None, received_at: datetime, retryable: bool | None = None,
             storage_state: str | None = None) -> UploadResultItem:
    return UploadResultItem(
        filename=filename, success=False, processing_status="FAILED", error_message=message, result=result, storage_state=storage_state,
        attempt_id=attempt.id if attempt is not None else None, uploaded_at=received_at, uploaded_at_ist=upload_history.to_ist_iso(received_at), retryable=retryable,
    )


def _duplicate_item(db: Session, *, existing, expense: bool, filename: str, content: bytes, file_hash: str, user: User, client_id: str | None, received_at: datetime,
                    cfg: central_storage.CentralConfig) -> UploadResultItem:
    """An exact duplicate: no second document, no second Drive original. The attempt is RECORDED (who, when, what name, how big, why it is a duplicate, which
    canonical document) so it is never silently discarded; when the retention policy is `copy` the bytes are also kept in the Drive "Duplicates" folder, linked to
    the canonical document by the attempt - the canonical file is never moved, renamed or overwritten."""
    doc_kind = EXPENSE if expense else TRIP
    retained = None
    if cfg.active and cfg.duplicate_retention == central_storage.RETENTION_COPY:
        retained = central_storage.retain_duplicate_copy(db, content, filename, user.id)
    attempt = _try_record_attempt(
        db, filename=filename, user_id=user.id, size=len(content), sha256=file_hash, document_type="EXPENSE_REPORT" if expense else "TRIP_REPORT", client_id=client_id,
        result=DUPLICATE, canonical_kind=doc_kind, canonical_pk=existing.id, duplicate_reason=DUPLICATE_REASON_HASH, storage_record_id=retained.id if retained else None,
        retained_copy=retained is not None, uploaded_at=received_at,
    )
    audit.log_action(
        db, user_id=user.id, action=audit.DOCUMENT_DUPLICATE_ATTEMPT, entity_type="ExpenseReport" if expense else "TripReport", entity_id=f"{doc_kind}:{existing.id}",
        details=f"filename={filename!r}; retained_copy={retained is not None}",
    )
    db.commit()
    label = central_storage.label_for(central_storage.lookup(db, doc_kind, file_hash)) if cfg.active else central_storage.NOT_CENTRAL
    return UploadResultItem(
        filename=filename, success=False, document_id=f"{doc_kind}:{existing.id}", processing_status=existing.processing_status, error_message="File already exists.",
        result=DUPLICATE, storage_state=label, attempt_id=attempt.id if attempt is not None else None, uploaded_at=received_at,
        uploaded_at_ist=upload_history.to_ist_iso(received_at),
        duplicate_of=DuplicateOf(document_id=f"{doc_kind}:{existing.id}", original_filename=existing.original_filename, uploaded_at=existing.uploaded_at),
    )


def _process_one(
    db: Session, file: UploadFile, *, expense: bool, user: User, background_tasks: BackgroundTasks, client_id: str | None = None
) -> UploadResultItem:
    received_at = datetime.now(timezone.utc)  # the server's clock, never the file's modified time
    content = file.file.read(pdf_validation.max_file_size_bytes() + 1)  # never reads (or holds) more than one byte past the limit; validate_pdf_upload then refuses it
    original_filename = file.filename or "document.pdf"
    document_type = "EXPENSE_REPORT" if expense else "TRIP_REPORT"
    doc_kind = EXPENSE if expense else TRIP
    cfg = central_storage.config()

    try:
        validation = validate_pdf_upload(original_filename, content)
    except PdfValidationError as exc:
        attempt = _try_record_attempt(
            db, filename=original_filename, user_id=user.id, size=len(content), sha256=None, document_type=document_type, client_id=client_id, result=FAILED,
            error_message=str(exc), uploaded_at=received_at,
        )
        db.commit()
        return _refusal(original_filename, result=FAILED, message=str(exc), attempt=attempt, received_at=received_at, retryable=False)

    file_hash = hashlib.sha256(content).hexdigest()
    model = ExpenseReport if expense else TripReport
    kind = storage_kind_for(expense=expense)
    id_extractor = extract_er_id if expense else extract_trip_id

    existing = db.execute(select(model).where(model.file_hash == file_hash)).scalars().first()
    if existing:
        return _duplicate_item(db, existing=existing, expense=expense, filename=original_filename, content=content, file_hash=file_hash, user=user, client_id=client_id,
                               received_at=received_at, cfg=cfg)

    storage_record = None
    storage_label = central_storage.NOT_CENTRAL
    if cfg.mode == central_storage.SERVE:
        # CENTRAL Drive is authoritative: the upload is not successful until the verified Drive copy exists. Nothing is ever substituted by a local copy.
        try:
            stored = central_storage.store_document(db, doc_kind, content, original_filename, user.id)
        except central_storage.CentralUnavailable as exc:
            outcome = DEFERRED if exc.retryable else FAILED
            attempt = _try_record_attempt(
                db, filename=original_filename, user_id=user.id, size=len(content), sha256=file_hash, document_type=document_type, client_id=client_id, result=outcome,
                error_message=exc.detail, uploaded_at=received_at,
            )
            audit.log_action(db, user_id=user.id, action=audit.DOCUMENT_UPLOAD_REFUSED, entity_type="ExpenseReport" if expense else "TripReport", details=f"result={outcome}; filename={original_filename!r}")
            db.commit()
            return _refusal(original_filename, result=outcome, message=exc.detail, attempt=attempt, received_at=received_at, retryable=exc.retryable)
        storage_record, storage_label = stored.record, stored.label
        if stored.label != central_storage.STORED:
            # A concurrent request may have created the document while this one waited for the same Drive upload: then this was simply a duplicate.
            winner = db.execute(select(model).where(model.file_hash == file_hash)).scalars().first()
            if winner is not None:
                return _duplicate_item(db, existing=winner, expense=expense, filename=original_filename, content=content, file_hash=file_hash, user=user, client_id=client_id,
                                       received_at=received_at, cfg=cfg)
            outcome = DEFERRED if stored.retryable else FAILED
            attempt = _try_record_attempt(
                db, filename=original_filename, user_id=user.id, size=len(content), sha256=file_hash, document_type=document_type, client_id=client_id, result=outcome,
                error_message=stored.message, storage_record_id=storage_record.id if storage_record is not None else None, uploaded_at=received_at,
            )
            audit.log_action(db, user_id=user.id, action=audit.DOCUMENT_UPLOAD_REFUSED, entity_type="ExpenseReport" if expense else "TripReport", details=f"result={outcome}; filename={original_filename!r}")
            db.commit()
            return _refusal(original_filename, result=outcome, message=stored.message or central_storage.GENERIC_UNAVAILABLE, attempt=attempt, received_at=received_at,
                            retryable=stored.retryable, storage_state=storage_label)
        # A second PC may have uploaded the same bytes at the same moment and created the document while this request waited for the (single) Drive file: duplicate.
        winner = db.execute(select(model).where(model.file_hash == file_hash)).scalars().first()
        if winner is not None:
            return _duplicate_item(db, existing=winner, expense=expense, filename=original_filename, content=content, file_hash=file_hash, user=user, client_id=client_id,
                                   received_at=received_at, cfg=cfg)

    try:
        stored_location = storage.save(kind, file_hash, original_filename, content)  # serve mode: the local WORKING COPY (a cache for OCR; Drive holds the original)
    except OSError:
        if cfg.mode != central_storage.SERVE:
            attempt = _try_record_attempt(
                db, filename=original_filename, user_id=user.id, size=len(content), sha256=file_hash, document_type=document_type, client_id=client_id, result=FAILED,
                error_message="The file could not be saved to storage.", uploaded_at=received_at,
            )
            db.commit()
            return _refusal(original_filename, result=FAILED, message="The file could not be saved to storage.", attempt=attempt, received_at=received_at, retryable=True)
        stored_location = ""  # the working copy is rebuilt from Drive on demand (central_storage.local_file_for)

    document_number = id_extractor(original_filename)

    row = model(
        filename=Path(stored_location).name if stored_location else original_filename,
        original_filename=original_filename,
        file_hash=file_hash,
        file_type="application/pdf",
        file_size=len(content),
        page_count=validation.page_count,
        source_path=stored_location,
        processing_status="PROCESSED",
        uploaded_at=received_at,
        uploaded_by_id=user.id,
        **({"er_id": document_number} if expense else {"trip_id": document_number}),
    )
    db.add(row)
    try:
        db.flush()  # assigns row.id, within the same transaction we commit below
    except IntegrityError:
        # Two uploads of the same bytes raced past the check above: the unique file_hash let exactly one create the document; this one is the duplicate.
        db.rollback()
        winner = db.execute(select(model).where(model.file_hash == file_hash)).scalars().first()
        if winner is None:
            raise
        return _duplicate_item(db, existing=winner, expense=expense, filename=original_filename, content=content, file_hash=file_hash, user=user, client_id=client_id,
                               received_at=received_at, cfg=cfg)
    audit.log_action(
        db, user_id=user.id, action=audit.DOCUMENT_UPLOADED,
        entity_type="ExpenseReport" if expense else "TripReport",
        entity_id=f"{doc_kind}:{row.id}", details=f"filename={original_filename!r}",
    )
    attempt = _try_record_attempt(
        db, filename=original_filename, user_id=user.id, size=len(content), sha256=file_hash, document_type=document_type, client_id=client_id, result=SUCCESS,
        canonical_kind=doc_kind, canonical_pk=row.id, storage_record_id=storage_record.id if storage_record is not None else None, uploaded_at=received_at,
    )
    if cfg.active:
        # Durable work: it survives a restart (the old BackgroundTasks lived in one process's memory). Shadow mode also queues the copy to Drive.
        processing_jobs.enqueue(db, doc_kind, row.id, EXTRACT)
        if cfg.mode == central_storage.SHADOW:
            processing_jobs.enqueue(db, doc_kind, row.id, SHADOW_COPY)
    db.commit()
    db.refresh(row)
    if cfg.active:
        processing_jobs.wake_worker()
        if cfg.mode == central_storage.SHADOW:
            storage_label = central_storage.PENDING
    else:
        # Auto-extraction: the document enters extraction processing immediately after
        # a successful upload, no manual step required (a manual reprocess endpoint
        # also exists). Runs in the background rather than blocking this response -
        # the upload itself (validate/store/create row) is already done and
        # successful at this point, and OCR/extraction for a real multi-page,
        # multi-file batch can take long enough that waiting for it here left the
        # HTTP request (and the frontend's upload UI) hanging indefinitely. A
        # failure during extraction is captured as extraction_status=FAILED on the
        # row itself - it must never fail the upload response, since the file was
        # genuinely received, validated, and stored successfully.
        background_tasks.add_task(_run_extraction_in_background, doc_kind, row.id)

    # Second, sorted + de-duplicated copy in the TA Backend archive (best effort, own thread: it can never fail or delay
    # this upload or the extraction - see services/ta_backend_forward.py). Only reached for a file the app itself accepted.
    ta_backend_forward.forward_upload(expense=expense, filename=original_filename, content=content)

    return UploadResultItem(
        filename=original_filename,
        success=True,
        document_id=f"{doc_kind}:{row.id}",
        processing_status=row.processing_status,
        page_count=row.page_count,
        document_number=document_number,
        extraction_status=row.extraction_status,
        result=SUCCESS,
        storage_state=storage_label,
        attempt_id=attempt.id if attempt is not None else None,
        uploaded_at=received_at,
        uploaded_at_ist=upload_history.to_ist_iso(received_at),
    )


@router.post("/api/uploads/expense-reports", response_model=UploadBatchResponse)
def upload_expense_reports(
    request: Request,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(UPLOAD_DOCUMENTS)),
):
    client_id = upload_history.client_identifier(request)
    results = [_process_one(db, f, expense=True, user=user, background_tasks=background_tasks, client_id=client_id) for f in files]
    return UploadBatchResponse(results=results)


@router.post("/api/uploads/trip-reports", response_model=UploadBatchResponse)
def upload_trip_reports(
    request: Request,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(UPLOAD_DOCUMENTS)),
):
    client_id = upload_history.client_identifier(request)
    results = [_process_one(db, f, expense=False, user=user, background_tasks=background_tasks, client_id=client_id) for f in files]
    return UploadBatchResponse(results=results)


def _user_names(db: Session, ids: set[int | None]) -> dict[int, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return {uid: name for uid, name in db.execute(select(User.id, User.name).where(User.id.in_(wanted))).all()}


@router.get("/api/uploads", response_model=list[UploadListItem])
def list_uploads(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    expense_rows = db.execute(select(ExpenseReport).order_by(ExpenseReport.uploaded_at.desc())).scalars().all()
    # Embedded trip summaries were never independently uploaded (no file of their own) -
    # they're shown inside their parent Expense Report's Extracted Details, not here.
    trip_rows = (
        db.execute(
            select(TripReport).where(TripReport.is_embedded.is_(False)).order_by(TripReport.uploaded_at.desc())
        )
        .scalars()
        .all()
    )

    names = _user_names(db, {r.uploaded_by_id for r in expense_rows} | {r.uploaded_by_id for r in trip_rows})
    labels = central_storage.storage_labels(db, [(EXPENSE, r.file_hash) for r in expense_rows] + [(TRIP, r.file_hash) for r in trip_rows])
    try:
        job_states = processing_jobs.state_for(db, [(EXPENSE, r.id) for r in expense_rows] + [(TRIP, r.id) for r in trip_rows]) if central_storage.config().active else {}
    except SQLAlchemyError:
        job_states = {}
    try:
        dup_counts = upload_history.duplicate_counts(db, [(EXPENSE, r.id) for r in expense_rows] + [(TRIP, r.id) for r in trip_rows])
    except SQLAlchemyError:
        dup_counts = {}

    def extras(doc_kind: str, r) -> dict:
        return {
            "uploaded_at_ist": upload_history.to_ist_iso(r.uploaded_at),
            "uploaded_by_id": r.uploaded_by_id,
            "uploaded_by_name": names.get(r.uploaded_by_id),
            "storage_state": labels.get((doc_kind, r.file_hash), central_storage.NOT_CENTRAL),
            "processing_state": job_states.get((doc_kind, r.id), "NONE"),
            "duplicate_count": dup_counts.get((doc_kind, r.id), 0),
        }

    items = [
        UploadListItem(
            document_id=f"expense:{r.id}",
            source_type="EXPENSE_REPORT",
            filename=r.filename,
            original_filename=r.original_filename,
            document_number=r.er_id,
            person_name=r.employee_name,
            employee_code=r.employee_code,
            file_size=r.file_size,
            page_count=r.page_count,
            processing_status=r.processing_status,
            error_message=r.error_message,
            extraction_status=r.extraction_status,
            uploaded_at=r.uploaded_at,
            **extras(EXPENSE, r),
        )
        for r in expense_rows
    ] + [
        UploadListItem(
            document_id=f"trip:{r.id}",
            source_type="TRIP_REPORT",
            filename=r.filename or "",
            original_filename=r.original_filename or "",
            document_number=r.trip_id,
            person_name=r.employee_name,
            employee_code=r.employee_code,
            file_size=r.file_size,
            page_count=r.page_count,
            processing_status=r.processing_status,
            error_message=r.error_message,
            extraction_status=r.extraction_status,
            uploaded_at=r.uploaded_at,
            **extras(TRIP, r),
        )
        for r in trip_rows
    ]
    items.sort(key=lambda i: i.uploaded_at, reverse=True)
    return items


@router.get("/api/uploads/attempts", response_model=list[UploadAttemptItem])
def list_upload_attempts(
    result: str | None = Query(None, description="SUCCESS | DUPLICATE | FAILED | DEFERRED"),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission(VIEW_DOCUMENTS)),
):
    """Every upload attempt, newest first: the upload history and the duplicate history (filter `result=DUPLICATE`). It survives restarts (it is a table)."""
    query = select(UploadAttempt).order_by(UploadAttempt.uploaded_at_utc.desc(), UploadAttempt.id.desc()).limit(limit)
    if result:
        query = query.where(UploadAttempt.result == result.upper())
    attempts = db.execute(query).scalars().all()
    names = _user_names(db, {a.uploaded_by_id for a in attempts})
    originals: dict[tuple[str, int], tuple[str | None, datetime | None]] = {}
    for doc_kind, model in ((EXPENSE, ExpenseReport), (TRIP, TripReport)):
        pks = {a.canonical_pk for a in attempts if a.canonical_kind == doc_kind and a.canonical_pk is not None}
        if pks:
            for pk, name, at in db.execute(select(model.id, model.original_filename, model.uploaded_at).where(model.id.in_(pks))).all():
                originals[(doc_kind, pk)] = (name, at)
    items = []
    for a in attempts:
        duplicate_of = None
        if a.result == DUPLICATE and a.canonical_pk is not None:
            name, at = originals.get((a.canonical_kind, a.canonical_pk), (None, None))  # name None = the canonical document has since been deleted (the history stays)
            duplicate_of = DuplicateOf(document_id=f"{a.canonical_kind}:{a.canonical_pk}", original_filename=name, uploaded_at=at)
        items.append(UploadAttemptItem(
            id=a.id, uploaded_at=a.uploaded_at_utc, uploaded_at_ist=upload_history.to_ist_iso(a.uploaded_at_utc), uploaded_by_id=a.uploaded_by_id,
            uploaded_by_name=names.get(a.uploaded_by_id), original_filename=a.original_filename, file_size=a.file_size, document_type=a.document_type, result=a.result,
            error_message=a.error_message, duplicate_reason=a.duplicate_reason, duplicate_of=duplicate_of, retained_copy=a.retained_copy, client=a.client_id,
            storage_state=(central_storage.label_for(central_storage.lookup(db, a.canonical_kind, a.sha256)) if central_storage.config().active and a.canonical_kind else None),
            sha256_short=a.sha256[:12] if a.sha256 else None,
        ))
    return items
