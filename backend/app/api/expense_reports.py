import calendar

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.expense_report import ExpenseReport
from app.schemas.expense_report import ExpenseReportListItem, ExpenseReportMonthSummary

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
            file_size=r.file_size,
            page_count=r.page_count,
            uploaded_at=r.uploaded_at,
            submitted_on=r.submitted_on,
        )
        for r in rows
    ]
