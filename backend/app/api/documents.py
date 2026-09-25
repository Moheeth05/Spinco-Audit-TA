from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.bill import Bill
from app.models.evidence import SourceEvidence
from app.models.expense_report import Expense, ExpenseReport
from app.models.trip import Trip, TripReport
from app.models.validation import ValidationResult
from app.schemas.documents import DocumentDetail
from app.services.document_ref import EXPENSE, TRIP, InvalidDocumentId, decode_document_id
from app.services.exception_engine import evaluate_report
from app.services.excel_generator import generate_workbook
from app.services.extraction.batta_pipeline import match_batta_expenses
from app.services.extraction.bill_pipeline import extract_and_match_bills
from app.services.extraction.expense_report_parser import find_report_summary_page
from app.services.extraction.pipeline import run_extraction
from app.services.extraction.shared import text_extractor
from app.services.storage_backend import storage
from app.services.validation.pipeline import validate_expense_report
from app.models.user import User
from app.services import audit
from app.services.permissions import DELETE_DOCUMENTS, REPROCESS_DOCUMENTS, VIEW_DOCUMENTS, require_permission

router = APIRouter(prefix="/api/documents", tags=["documents"])
_view = Depends(require_permission(VIEW_DOCUMENTS))
_reprocess = Depends(require_permission(REPROCESS_DOCUMENTS))


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
def get_document(document_id: str, db: Session = Depends(get_db), _user: User = _view):
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
        extraction_status=row.extraction_status,
        extraction_confidence=row.extraction_confidence,
        document_number=row.er_id if is_expense else row.trip_id,
        person_name=row.employee_name,
        employee_code=row.employee_code,
        report_month_label=row.report_month_label if is_expense else None,
        submitted_on=row.submitted_on if is_expense else None,
        overall_status=row.overall_status if is_expense else None,
        total_expenses=row.total_expenses if is_expense else None,
        verified_expense_count=row.verified_expense_count if is_expense else None,
        exception_count=row.exception_count if is_expense else None,
        review_required_count=row.review_required_count if is_expense else None,
    )


@router.get("/{document_id}/file")
def get_document_file(document_id: str, download: bool = False, db: Session = Depends(get_db), _user: User = _view):
    _, row = _load(db, document_id)
    if not row.source_path or not storage.exists(row.source_path):
        raise HTTPException(status_code=404, detail="The stored file could not be found.")

    # FileResponse streams directly from disk with HTTP Range support - the
    # one place this module still needs a real local path rather than
    # storage.retrieve()'s in-memory bytes (see storage_backend.py's
    # module docstring for why, and what changes here when a non-local
    # backend is introduced).
    return FileResponse(
        path=storage.local_path(row.source_path),
        media_type="application/pdf",
        filename=row.original_filename,
        content_disposition_type="attachment" if download else "inline",
    )


def _evidence_map(db: Session, document_type: str, document_ref_id: int) -> dict[str, int]:
    rows = (
        db.query(SourceEvidence)
        .filter(SourceEvidence.document_type == document_type, SourceEvidence.document_ref_id == document_ref_id)
        .all()
    )
    return {r.field_name: r.page_number for r in rows}


def _leg_out(leg: Trip) -> dict:
    return {
        "leg_no": leg.leg_no,
        "from_location": leg.from_location,
        "from_date": leg.from_date,
        "to_location": leg.to_location,
        "to_date": leg.to_date,
        "description": leg.description,
    }


def _build_extraction_detail(db: Session, document_id: str, ref, row) -> dict:
    is_expense = ref.kind == EXPENSE
    evidence = _evidence_map(db, EXPENSE if is_expense else TRIP, row.id)

    base = {
        "document_id": document_id,
        "source_type": "EXPENSE_REPORT" if is_expense else "TRIP_REPORT",
        "extraction_status": row.extraction_status,
        "extraction_confidence": row.extraction_confidence,
        "extraction_notes": row.extraction_notes,
    }
    if is_expense:
        base.update(
            {
                "overall_status": row.overall_status,
                "total_expenses": row.total_expenses,
                "verified_expense_count": row.verified_expense_count,
                "exception_count": row.exception_count,
                "review_required_count": row.review_required_count,
            }
        )

    if is_expense:
        expenses = (
            db.query(Expense).filter(Expense.expense_report_id == row.id).order_by(Expense.s_no).all()
        )
        embedded = (
            db.query(TripReport)
            .filter(TripReport.is_embedded.is_(True), TripReport.source_expense_report_id == row.id)
            .first()
        )
        embedded_out = None
        if embedded:
            embedded_legs = db.query(Trip).filter(Trip.trip_report_id == embedded.id).order_by(Trip.leg_no).all()
            embedded_out = {
                "document_id": f"trip:{embedded.id}",
                "trip_id": embedded.trip_id,
                "destination": embedded.destination,
                "start_date": embedded.start_date,
                "end_date": embedded.end_date,
                "source_page": embedded.embedded_source_page,
                "extraction_status": embedded.extraction_status,
                "extraction_confidence": embedded.extraction_confidence,
                "legs": [_leg_out(leg) for leg in embedded_legs],
            }

        base.update(
            {
                "header": {
                    "er_id": row.er_id,
                    "er_id_source_page": evidence.get("er_id"),
                    "employee_name": row.employee_name,
                    "employee_name_source_page": evidence.get("employee_name"),
                    "employee_code": row.employee_code,
                    "employee_code_source_page": evidence.get("employee_code"),
                    "department": row.department,
                    "location": row.location,
                    "report_month_label": row.report_month_label,
                    "submitted_on": row.submitted_on,
                    "submitted_on_source_page": evidence.get("submitted_on"),
                    "total_claimed": row.total_claimed,
                    "total_claimed_source_page": evidence.get("total_claimed"),
                    "total_expenses": row.total_expenses,
                },
                "expenses": [
                    {
                        "id": e.id,
                        "s_no": e.s_no,
                        "expense_date": e.expense_date,
                        "category": e.category,
                        "merchant": e.merchant,
                        "expense_details": e.expense_details,
                        "claimed_amount": e.claimed_amount,
                        "source_page": e.source_page,
                        "extraction_status": e.extraction_status,
                        "bill_found": e.bill_found,
                        "bill_id": e.bill_id,
                        "bill_amount": e.bill_amount,
                        "bill_date": e.bill_date,
                        "bill_match_status": e.bill_match_status,
                        "bill_match_score": e.bill_match_score,
                        "bill_match_confidence": e.bill_match_confidence,
                        "bill_match_reasons": e.bill_match_reasons,
                        "policy_basis": e.policy_basis,
                        "policy_limit": e.policy_limit,
                        "validation_status": e.validation_status,
                        "trip_report_id": e.trip_report_id,
                        "trip_match_status": e.trip_match_status,
                        "trip_match_score": e.trip_match_score,
                        "trip_match_confidence": e.trip_match_confidence,
                        "trip_match_reasons": e.trip_match_reasons,
                        "trip_employee_mismatch": e.trip_employee_mismatch,
                    }
                    for e in expenses
                ],
                "validations": [
                    {
                        "id": v.id,
                        "expense_id": v.expense_id,
                        "check_type": v.check_type,
                        "result": v.result,
                        "exception_type": v.exception_type,
                        "details": v.details,
                        "remarks": v.remarks,
                        "claimed_amount": v.claimed_amount,
                        "policy_limit": v.policy_limit,
                        "bill_amount": v.bill_amount,
                        "expense_date": v.expense_date,
                        "bill_date": v.bill_date,
                        "policy_category": v.policy_category,
                        "policy_version": v.policy_version,
                        "source_page": v.source_page,
                    }
                    for v in db.query(ValidationResult)
                    .filter(ValidationResult.expense_report_id == row.id)
                    .order_by(ValidationResult.expense_id.is_(None), ValidationResult.expense_id, ValidationResult.id)
                    .all()
                ],
                "bills": [
                    {
                        "id": b.id,
                        "page_number": b.page_number,
                        "vendor": b.vendor,
                        "bill_number": b.bill_number,
                        "bill_date": b.bill_date,
                        "amount": b.amount,
                        "gstin": b.gstin,
                        "description": b.description,
                        "category_guess": b.category_guess,
                        "extraction_status": b.extraction_status,
                        "extraction_confidence": b.extraction_confidence,
                        "extraction_notes": b.extraction_notes,
                        "match_status": b.match_status,
                        "matched_expense_id": b.matched_expense_id,
                    }
                    for b in db.query(Bill).filter(Bill.expense_report_id == row.id).order_by(Bill.page_number).all()
                ],
                "embedded_trip_summary": embedded_out,
            }
        )
    else:
        legs = db.query(Trip).filter(Trip.trip_report_id == row.id).order_by(Trip.leg_no).all()
        base.update(
            {
                "trip": {
                    "trip_id": row.trip_id,
                    "trip_id_source_page": evidence.get("trip_id"),
                    "employee_name": row.employee_name,
                    "employee_name_source_page": evidence.get("employee_name"),
                    "employee_code": row.employee_code,
                    "start_date": row.start_date,
                    "end_date": row.end_date,
                    "origin": row.origin,
                    "destination": row.destination,
                    "destination_source_page": evidence.get("destination"),
                    "purpose": row.purpose,
                    "purpose_source_page": evidence.get("purpose"),
                    "approval_status": row.approval_status,
                    "approval_date": row.approval_date,
                    "approver": row.approver,
                    "approver_source_page": evidence.get("approver"),
                },
                "legs": [_leg_out(leg) for leg in legs],
            }
        )

    return base


@router.get("/{document_id}/extraction")
def get_document_extraction(document_id: str, db: Session = Depends(get_db), _user: User = _view):
    ref, row = _load(db, document_id)
    return _build_extraction_detail(db, document_id, ref, row)


@router.post("/{document_id}/extract")
@router.post("/{document_id}/reprocess")
def reprocess_document(document_id: str, db: Session = Depends(get_db), user: User = _reprocess):
    """Re-runs extraction for this document, replacing its previously extracted
    fields/lines/evidence. Always updates the existing record - never creates a
    duplicate document."""
    ref, row = _load(db, document_id)
    run_extraction(db, ref.kind, ref.pk)  # commits internally (wholesale-replace pipeline - out of scope to change here)
    db.refresh(row)
    audit.log_action(
        db, user_id=user.id, action=audit.DOCUMENT_REPROCESSED,
        entity_type="ExpenseReport" if ref.kind == EXPENSE else "TripReport", entity_id=document_id,
    )
    db.commit()  # log_action only flushes now - see docs/CENTRALIZED_ARCHITECTURE.md §16
    return _build_extraction_detail(db, document_id, ref, row)


@router.post("/{document_id}/extract-bills")
@router.post("/{document_id}/rematch-bills")
def rematch_bills(document_id: str, db: Session = Depends(get_db), user: User = _reprocess):
    """Re-runs just the bill extraction/matching step (not the full header/
    expense-line extraction) for an Expense Report - e.g. after the matcher
    itself is improved, without needing to reprocess the whole document."""
    ref, row = _load(db, document_id)
    if ref.kind != EXPENSE:
        raise HTTPException(status_code=400, detail="Bill matching only applies to Expense Report documents.")
    if not row.source_path or not Path(row.source_path).exists():
        raise HTTPException(status_code=404, detail="The stored file could not be found.")

    content_pages = text_extractor.content_pages(Path(row.source_path))
    content_last_page = find_report_summary_page(content_pages)
    if content_last_page is None:
        content_last_page = content_pages[-1]["page_number"] if content_pages else 0
    extract_and_match_bills(db, row, text_extractor, content_last_page)
    try:
        validate_expense_report(db, row.id)
        evaluate_report(db, row.id)
        generate_workbook(db)
    except Exception:  # noqa: BLE001
        pass
    db.refresh(row)
    audit.log_action(db, user_id=user.id, action=audit.BILL_REMATCHED, entity_type="ExpenseReport", entity_id=document_id)
    db.commit()  # log_action only flushes now - see docs/CENTRALIZED_ARCHITECTURE.md §16
    return _build_extraction_detail(db, document_id, ref, row)


@router.post("/{document_id}/rematch-trips")
def rematch_trips(document_id: str, db: Session = Depends(get_db), user: User = _reprocess):
    """Re-runs just the Batta-to-Trip matching step (not the full extraction)
    for an Expense Report, then re-validates so the Batta Trip Match Check
    reflects the refreshed result - e.g. after new Trip Reports were
    uploaded or the matcher itself was improved."""
    ref, row = _load(db, document_id)
    if ref.kind != EXPENSE:
        raise HTTPException(status_code=400, detail="Batta trip matching only applies to Expense Report documents.")
    match_batta_expenses(db, row)
    try:
        validate_expense_report(db, row.id)
        evaluate_report(db, row.id)
        generate_workbook(db)
    except Exception:  # noqa: BLE001
        pass
    db.refresh(row)
    audit.log_action(db, user_id=user.id, action=audit.TRIP_REMATCHED, entity_type="ExpenseReport", entity_id=document_id)
    db.commit()  # log_action only flushes now - see docs/CENTRALIZED_ARCHITECTURE.md §16
    return _build_extraction_detail(db, document_id, ref, row)


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: str, db: Session = Depends(get_db), user: User = Depends(require_permission(DELETE_DOCUMENTS))):
    ref, row = _load(db, document_id)
    source_location = row.source_path
    filename = row.original_filename

    db.delete(row)
    audit.log_action(
        db, user_id=user.id, action=audit.DOCUMENT_DELETED,
        entity_type="ExpenseReport" if ref.kind == EXPENSE else "TripReport", entity_id=document_id,
        details=f"filename={filename!r}",
    )
    db.commit()

    if source_location:
        try:
            storage.delete(source_location)
        except OSError:
            pass
