import calendar

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.validation import ValidationResult
from app.schemas.bills import BillOut
from app.schemas.expense_report import ExpenseLineItemOut, ExpenseReportListItem, ExpenseReportMonthSummary
from app.schemas.validation import ValidationResultOut
from app.services.validation.pipeline import validate_expense_report

router = APIRouter(prefix="/api/expense-reports", tags=["expense-reports"])


def _month_label(report_month: str) -> str:
    year, month = report_month.split("-")
    return f"{calendar.month_name[int(month)]} {year}"


@router.get("/months", response_model=list[ExpenseReportMonthSummary])
def list_months(db: Session = Depends(get_db)):
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
):
    """status: 'ALL' | 'ACCOUNT VERIFIED' | 'EXCEPTION' (default ALL)."""
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
def get_expense_lines(report_id: int, db: Session = Depends(get_db)):
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
def get_bills(report_id: int, db: Session = Depends(get_db)):
    report = db.get(ExpenseReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Expense report not found.")
    bills = db.execute(
        select(Bill).where(Bill.expense_report_id == report_id).order_by(Bill.page_number)
    ).scalars().all()
    return bills


@router.get("/{report_id}/validations", response_model=list[ValidationResultOut])
def get_validations(report_id: int, db: Session = Depends(get_db)):
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
def revalidate_report(report_id: int, db: Session = Depends(get_db)):
    """Re-runs policy validation for this report, replacing its previous
    validation_results/exceptions wholesale - never accumulates duplicates."""
    report = db.get(ExpenseReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Expense report not found.")
    validate_expense_report(db, report_id)
    results = db.execute(
        select(ValidationResult)
        .where(ValidationResult.expense_report_id == report_id)
        .order_by(ValidationResult.expense_id.is_(None), ValidationResult.expense_id, ValidationResult.id)
    ).scalars().all()
    return results
