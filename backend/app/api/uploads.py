import hashlib

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.expense_report import ExpenseReport
from app.models.trip import TripReport
from app.schemas.uploads import UploadBatchResponse, UploadListItem, UploadResultItem
from app.services.document_ref import EXPENSE, TRIP
from app.services.extraction.pipeline import run_extraction
from app.services.filename_ids import extract_er_id, extract_trip_id
from app.services.pdf_validation import PdfValidationError, validate_pdf_upload
from app.services.storage import EXPENSE_REPORTS_DIR, TRIP_REPORTS_DIR, save_upload
from app.services import audit
from app.models.user import User
from app.services.permissions import UPLOAD_DOCUMENTS, get_current_user, require_permission

router = APIRouter(tags=["uploads"])


def _process_one(db: Session, file: UploadFile, *, expense: bool) -> UploadResultItem:
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
    directory = EXPENSE_REPORTS_DIR if expense else TRIP_REPORTS_DIR
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
        stored_path = save_upload(directory, file_hash, original_filename, content)
    except OSError:
        return UploadResultItem(
            filename=original_filename,
            success=False,
            processing_status="FAILED",
            error_message="The file could not be saved to storage.",
        )

    document_number = id_extractor(original_filename)

    row = model(
        filename=stored_path.name,
        original_filename=original_filename,
        file_hash=file_hash,
        file_type="application/pdf",
        file_size=len(content),
        page_count=validation.page_count,
        source_path=str(stored_path),
        processing_status="PROCESSED",
        **({"er_id": document_number} if expense else {"trip_id": document_number}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # Auto-extraction: the document enters extraction processing immediately after
    # a successful upload, no manual step required (a manual reprocess endpoint
    # also exists). A failure here is captured as extraction_status=FAILED on the
    # row itself - it must never fail the upload response, since the file was
    # genuinely received, validated, and stored successfully.
    try:
        run_extraction(db, EXPENSE if expense else TRIP, row.id)
        db.refresh(row)
    except Exception:  # noqa: BLE001 - upload succeeded regardless of extraction outcome
        pass

    return UploadResultItem(
        filename=original_filename,
        success=True,
        document_id=f"{'expense' if expense else 'trip'}:{row.id}",
        processing_status=row.processing_status,
        page_count=row.page_count,
        document_number=document_number,
        extraction_status=row.extraction_status,
    )


def _audit_uploads(db: Session, user: User, results: list[UploadResultItem], *, doc_type: str) -> None:
    for r in results:
        if r.success:
            audit.log_action(
                db, user_id=user.id, action=audit.DOCUMENT_UPLOADED, entity_type=doc_type,
                entity_id=r.document_id, details=f"filename={r.filename!r}",
            )


@router.post("/api/uploads/expense-reports", response_model=UploadBatchResponse)
def upload_expense_reports(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(UPLOAD_DOCUMENTS)),
):
    results = [_process_one(db, f, expense=True) for f in files]
    _audit_uploads(db, user, results, doc_type="ExpenseReport")
    return UploadBatchResponse(results=results)


@router.post("/api/uploads/trip-reports", response_model=UploadBatchResponse)
def upload_trip_reports(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(UPLOAD_DOCUMENTS)),
):
    results = [_process_one(db, f, expense=False) for f in files]
    _audit_uploads(db, user, results, doc_type="TripReport")
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
