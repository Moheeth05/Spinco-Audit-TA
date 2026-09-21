from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.expense_report import ExpenseReport
from app.models.trip import TripReport
from app.schemas.documents import DocumentDetail
from app.services.document_ref import EXPENSE, InvalidDocumentId, decode_document_id

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _load(db: Session, document_id: str):
    try:
        ref = decode_document_id(document_id)
    except InvalidDocumentId:
        raise HTTPException(status_code=400, detail="Invalid document id.")

    model = ExpenseReport if ref.kind == EXPENSE else TripReport
    row = db.get(model, ref.pk)
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return ref, row


@router.get("/{document_id}", response_model=DocumentDetail)
def get_document(document_id: str, db: Session = Depends(get_db)):
    ref, row = _load(db, document_id)
    is_expense = ref.kind == EXPENSE
    return DocumentDetail(
        document_id=document_id,
        source_type="EXPENSE_REPORT" if is_expense else "TRIP_REPORT",
        filename=row.filename or "",
        original_filename=row.original_filename or "",
        file_type=row.file_type,
        file_size=row.file_size,
        page_count=row.page_count,
        file_hash=row.file_hash,
        processing_status=row.processing_status,
        error_message=row.error_message,
        uploaded_at=row.uploaded_at,
        document_number=row.er_id if is_expense else row.trip_id,
        person_name=row.employee_name,
        employee_code=row.employee_code,
        report_month_label=row.report_month_label if is_expense else None,
        submitted_on=row.submitted_on if is_expense else None,
    )


@router.get("/{document_id}/file")
def get_document_file(document_id: str, download: bool = False, db: Session = Depends(get_db)):
    _, row = _load(db, document_id)
    if not row.source_path or not Path(row.source_path).exists():
        raise HTTPException(status_code=404, detail="The stored file could not be found.")

    return FileResponse(
        path=row.source_path,
        media_type="application/pdf",
        filename=row.original_filename,
        content_disposition_type="attachment" if download else "inline",
    )


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: str, db: Session = Depends(get_db)):
    ref, row = _load(db, document_id)
    file_path = Path(row.source_path) if row.source_path else None

    db.delete(row)
    db.commit()

    if file_path and file_path.exists():
        try:
            file_path.unlink()
        except OSError:
            pass
