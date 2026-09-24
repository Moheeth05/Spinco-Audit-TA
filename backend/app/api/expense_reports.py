import calendar

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.validation import ExceptionRecord, ValidationResult
from app.schemas.bills import BillOut
from app.schemas.exceptions import ExceptionRecordOut, ReportStatusOut
from app.schemas.expense_report import ExpenseLineItemOut, ExpenseReportListItem, ExpenseReportMonthSummary
from app.schemas.validation import ValidationResultOut
from app.services.exception_engine import evaluate_report
from app.services.excel_generator import generate_workbook
from app.services.validation.pipeline import validate_expense_report
from app.models.user import User
from app.services import audit
from app.services.permissions import VALIDATE_REPORTS, VIEW_EXPENSE_REPORTS, require_permission

router = APIRouter(prefix="/api/expense-reports", tags=["expense-reports"])
_view = Depends(require_permission(VIEW_EXPENSE_REPORTS))


def _month_label(report_month: str) -> str:
    year, month = report_month.split("-")
    return f"{calendar.month_name[int(month)]} {year}"


@router.get("/months", response_model=list[ExpenseReportMonthSummary])
def list_months(db: Session = Depends(get_db), _user: User = _view):
    rows = db.execute(
        select(ExpenseReport.report_month, func.count(ExpenseReport.id))
        .where(ExpenseReport.report_month.is_not(None))
        .group_by(ExpenseReport.report_month)
        .order_by(ExpenseReport.report_month.desc())
    ).all()
    return [
        ExpenseReportMonthSummary(report_month=m, report_month_label=_month_label(m), report_count=c)
        for m, c in rows
    ]


@router.get("", response_model=list[ExpenseReportListItem])
def list_expense_reports(
    status: str | None = None,
    report_month: str | None = None,
    db: Session = Depends(get_db),
    _user: User = _view,
):
    """status: 'ALL' | 'ACCOUNT VERIFIED' | 'EXCEPTION' | 'REVIEW REQUIRED' (default ALL)."""
    stmt = select(ExpenseReport).order_by(ExpenseReport.uploaded_at.desc())
    if status and status != "ALL":
        stmt = stmt.where(ExpenseReport.overall_status == status)
    if report_month:
        stmt = stmt.where(ExpenseReport.report_month == report_month)
    rows = db.execute(stmt).scalars().all()
    return [
        ExpenseReportListItem(
            document_id=f"expense:{r.id}",
            er_id=r.er_id,
            filename=r.filename,
            original_filename=r.original_filename,
            employee_name=r.employee_name,
            employee_code=r.employee_code,
            report_month_label=r.report_month_label,
            total_expenses=r.total_expenses,
            total_claimed=r.total_claimed,
            verified_expense_count=r.verified_expense_count,
            exception_count=r.exception_count,
            review_required_count=r.review_required_count,
            overall_status=r.overall_status,
            processing_status=r.processing_status,
            error_message=r.error_message,
            extraction_status=r.extraction_status,
            extraction_confidence=r.extraction_confidence,
            file_size=r.file_size,
            page_count=r.page_count,
            uploaded_at=r.uploaded_at,
            submitted_on=r.submitted_on,
        )
        for r in rows
    ]


@router.get("/{report_id}/expenses", response_model=list[ExpenseLineItemOut])
def get_expense_lines(report_id: int, db: Session = Depends(get_db), _user: User = _view):
    report = db.get(ExpenseReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Expense report not found.")
    lines = db.execute(
        select(Expense).where(Expense.expense_report_id == report_id).order_by(Expense.s_no)
    ).scalars().all()
    return [
        ExpenseLineItemOut(
            id=e.id,
            s_no=e.s_no,
            expense_date=e.expense_date,
            category=e.category,
            merchant=e.merchant,
            expense_details=e.expense_details,
            claimed_amount=e.claimed_amount,
            source_page=e.source_page,
            extraction_status=e.extraction_status,
            bill_found=e.bill_found,
            bill_amount=e.bill_amount,
            bill_date=e.bill_date,
            bill_match_status=e.bill_match_status,
            bill_match_score=e.bill_match_score,
            bill_match_confidence=e.bill_match_confidence,
            policy_basis=e.policy_basis,
            policy_limit=e.policy_limit,
            validation_status=e.validation_status,
            trip_report_id=e.trip_report_id,
            trip_match_status=e.trip_match_status,
            trip_match_score=e.trip_match_score,
            trip_match_confidence=e.trip_match_confidence,
            trip_employee_mismatch=e.trip_employee_mismatch,
        )
        for e in lines
    ]


@router.get("/{report_id}/bills", response_model=list[BillOut])
def get_bills(report_id: int, db: Session = Depends(get_db), _user: User = _view):
    report = db.get(ExpenseReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Expense report not found.")
    bills = db.execute(
        select(Bill).where(Bill.expense_report_id == report_id).order_by(Bill.page_number)
    ).scalars().all()
    return bills


@router.get("/{report_id}/validations", response_model=list[ValidationResultOut])
def get_validations(report_id: int, db: Session = Depends(get_db), _user: User = _view):
    report = db.get(ExpenseReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Expense report not found.")
    results = db.execute(
        select(ValidationResult)
        .where(ValidationResult.expense_report_id == report_id)
        .order_by(ValidationResult.expense_id.is_(None), ValidationResult.expense_id, ValidationResult.id)
    ).scalars().all()
    return results


@router.post("/{report_id}/validate", response_model=list[ValidationResultOut])
@router.post("/{report_id}/revalidate", response_model=list[ValidationResultOut])
def revalidate_report(report_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission(VALIDATE_REPORTS))):
    """Re-runs policy validation for this report, replacing its previous
    validation_results/exceptions wholesale - never accumulates duplicates.
    Also re-evaluates the report-level Exception Engine (Phase 7) so
    overall_status/exception_count/review_required_count stay in sync."""
    report = db.get(ExpenseReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Expense report not found.")
    validate_expense_report(db, report_id)
    evaluate_report(db, report_id)
    try:
        generate_workbook(db)
    except Exception:  # noqa: BLE001 - revalidation must succeed even if the workbook write fails
        pass
    audit.log_action(db, user_id=user.id, action=audit.REPORT_REVALIDATED, entity_type="ExpenseReport", entity_id=str(report_id))
    results = db.execute(
        select(ValidationResult)
        .where(ValidationResult.expense_report_id == report_id)
        .order_by(ValidationResult.expense_id.is_(None), ValidationResult.expense_id, ValidationResult.id)
    ).scalars().all()
    return results


@router.get("/{report_id}/status", response_model=ReportStatusOut)
def get_report_status(report_id: int, db: Session = Depends(get_db), _user: User = _view):
    report = db.get(ExpenseReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Expense report not found.")
    return ReportStatusOut(
        document_id=f"expense:{report.id}",
        overall_status=report.overall_status,
        total_expenses=report.total_expenses,
        verified_expense_count=report.verified_expense_count,
        exception_count=report.exception_count,
        review_required_count=report.review_required_count,
    )


@router.get("/{report_id}/exceptions", response_model=list[ExceptionRecordOut])
def get_exceptions(report_id: int, db: Session = Depends(get_db), _user: User = _view):
    report = db.get(ExpenseReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Expense report not found.")

    rows = db.execute(
        select(ExceptionRecord)
        .where(ExceptionRecord.expense_report_id == report_id)
        .order_by(ExceptionRecord.expense_id.is_(None), ExceptionRecord.expense_id, ExceptionRecord.id)
    ).scalars().all()

    expenses_by_id = {
        e.id: e for e in db.execute(select(Expense).where(Expense.expense_report_id == report_id)).scalars().all()
    }
    vr_ids = {r.validation_result_id for r in rows if r.validation_result_id is not None}
    vr_by_id = (
        {v.id: v for v in db.execute(select(ValidationResult).where(ValidationResult.id.in_(vr_ids))).scalars().all()}
        if vr_ids
        else {}
    )

    out = []
    for r in rows:
        expense = expenses_by_id.get(r.expense_id) if r.expense_id is not None else None
        vr = vr_by_id.get(r.validation_result_id) if r.validation_result_id is not None else None
        out.append(
            ExceptionRecordOut(
                id=r.id,
                expense_id=r.expense_id,
                s_no=expense.s_no if expense else None,
                category=expense.category if expense else None,
                merchant=expense.merchant if expense else None,
                exception_type=r.exception_type,
                severity=r.severity,
                reason=r.reason,
                claimed_amount=(vr.claimed_amount if vr and vr.claimed_amount is not None else (expense.claimed_amount if expense else None)),
                policy_limit=vr.policy_limit if vr else None,
                bill_amount=vr.bill_amount if vr else None,
                expense_date=(vr.expense_date if vr and vr.expense_date is not None else (expense.expense_date if expense else None)),
                bill_date=vr.bill_date if vr else None,
                bill_id=r.bill_id,
                trip_report_id=r.trip_report_id,
                source_document_id=r.source_document_id,
                source_page=r.source_page,
                status=r.status,
                created_at=r.created_at,
            )
        )
    return out
