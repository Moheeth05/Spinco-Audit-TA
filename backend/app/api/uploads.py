import hashlib
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models.expense_report import ExpenseReport
from app.models.trip import TripReport
from app.schemas.uploads import UploadBatchResponse, UploadListItem, UploadResultItem
from app.services.document_ref import EXPENSE, TRIP
from app.services.extraction.pipeline import run_extraction
from app.services.filename_ids import extract_er_id, extract_trip_id
from app.services.pdf_validation import PdfValidationError, validate_pdf_upload
from app.services.storage_backend import storage, storage_kind_for
from app.services import audit, ta_backend_forward
from app.models.user import User
from app.services.permissions import UPLOAD_DOCUMENTS, get_current_user, require_permission

router = APIRouter(tags=["uploads"])
logger = logging.getLogger(__name__)


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
    """
    db = SessionLocal()
    try:
        run_extraction(db, kind, pk)
    except Exception:  # noqa: BLE001 - background task: must never crash the server, but must be visible
        logger.exception("Background extraction failed for %s:%s", kind, pk)
    finally:
        db.close()


def _process_one(
    db: Session, file: UploadFile, *, expense: bool, user: User, background_tasks: BackgroundTasks
) -> UploadResultItem:
    content = file.file.read()
    original_filename = file.filename or "document.pdf"

    try:
        validation = validate_pdf_upload(original_filename, content)
    except PdfValidationError as exc:
        return UploadResultItem(
            filename=original_filename,
            success=False,
            processing_status="FAILED",
            error_message=str(exc),
        )

    file_hash = hashlib.sha256(content).hexdigest()
    model = ExpenseReport if expense else TripReport
    kind = storage_kind_for(expense=expense)
    id_extractor = extract_er_id if expense else extract_trip_id

    existing = db.execute(select(model).where(model.file_hash == file_hash)).scalars().first()
    if existing:
        return UploadResultItem(
            filename=original_filename,
            success=False,
            document_id=f"{'expense' if expense else 'trip'}:{existing.id}",
            processing_status=existing.processing_status,
            error_message="File already exists.",
        )

    try:
        stored_location = storage.save(kind, file_hash, original_filename, content)
    except OSError:
        return UploadResultItem(
            filename=original_filename,
            success=False,
            processing_status="FAILED",
            error_message="The file could not be saved to storage.",
        )

    document_number = id_extractor(original_filename)

    row = model(
        filename=Path(stored_location).name,
        original_filename=original_filename,
        file_hash=file_hash,
        file_type="application/pdf",
        file_size=len(content),
        page_count=validation.page_count,
        source_path=stored_location,
        processing_status="PROCESSED",
        **({"er_id": document_number} if expense else {"trip_id": document_number}),
    )
    db.add(row)
    db.flush()  # assigns row.id, within the same transaction we commit below
    audit.log_action(
        db, user_id=user.id, action=audit.DOCUMENT_UPLOADED,
        entity_type="ExpenseReport" if expense else "TripReport",
        entity_id=f"{'expense' if expense else 'trip'}:{row.id}", details=f"filename={original_filename!r}",
    )
    db.commit()
    db.refresh(row)

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
    background_tasks.add_task(_run_extraction_in_background, EXPENSE if expense else TRIP, row.id)

    # Second, sorted + de-duplicated copy in the TA Backend archive (best effort, own thread: it can never fail or delay
    # this upload or the extraction - see services/ta_backend_forward.py). Only reached for a file the app itself accepted.
    ta_backend_forward.forward_upload(expense=expense, filename=original_filename, content=content)

    return UploadResultItem(
        filename=original_filename,
        success=True,
        document_id=f"{'expense' if expense else 'trip'}:{row.id}",
        processing_status=row.processing_status,
        page_count=row.page_count,
        document_number=document_number,
        extraction_status=row.extraction_status,
    )


@router.post("/api/uploads/expense-reports", response_model=UploadBatchResponse)
def upload_expense_reports(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(UPLOAD_DOCUMENTS)),
):
    results = [_process_one(db, f, expense=True, user=user, background_tasks=background_tasks) for f in files]
    return UploadBatchResponse(results=results)


@router.post("/api/uploads/trip-reports", response_model=UploadBatchResponse)
def upload_trip_reports(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(UPLOAD_DOCUMENTS)),
):
    results = [_process_one(db, f, expense=False, user=user, background_tasks=background_tasks) for f in files]
    return UploadBatchResponse(results=results)


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
        )
        for r in trip_rows
    ]
    items.sort(key=lambda i: i.uploaded_at, reverse=True)
    return items
