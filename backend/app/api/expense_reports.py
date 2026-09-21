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
    return db.execute(stmt).scalars().all()
