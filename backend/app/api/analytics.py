from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.expense_report import ExpenseReport
from app.models.user import User
from app.schemas.analytics import (
    BillMatchSummary,
    CategoryAnalyticsPoint,
    EmployeeClaimedPoint,
    ExceptionMonthlyPoint,
    ExceptionTypePoint,
    MonthlyClaimedPoint,
    MonthlyVolumePoint,
    PolicyLimitAnalysisOut,
    ReportStatusSummary,
    StatusCountPoint,
    TripAnalyticsOut,
    TripMatchSummary,
)
from app.services import analytics_service
from app.services.permissions import VIEW_ANALYTICS, require_permission

router = APIRouter(prefix="/api/analytics", tags=["analytics"])
_view = Depends(require_permission(VIEW_ANALYTICS))


@router.get("/summary", response_model=ReportStatusSummary)
def summary(month: str | None = None, db: Session = Depends(get_db), _user: User = _view):
    return analytics_service.report_status_summary(db, month=month)


@router.get("/status", response_model=list[StatusCountPoint])
def status_breakdown(month: str | None = None, db: Session = Depends(get_db), _user: User = _view):
    s = analytics_service.report_status_summary(db, month=month)
    return [
        StatusCountPoint(status="ACCOUNT VERIFIED", count=s["account_verified_reports"]),
        StatusCountPoint(status="EXCEPTION", count=s["exception_reports"]),
        StatusCountPoint(status="REVIEW REQUIRED", count=s["review_required_reports"]),
    ]


@router.get("/monthly", response_model=list[MonthlyVolumePoint])
def monthly(db: Session = Depends(get_db), _user: User = _view):
    return analytics_service.monthly_report_volume(db)


@router.get("/monthly-claimed", response_model=list[MonthlyClaimedPoint])
def monthly_claimed(db: Session = Depends(get_db), _user: User = _view):
    return analytics_service.monthly_claimed_trend(db)


@router.get("/categories", response_model=list[CategoryAnalyticsPoint])
def categories(month: str | None = None, status: str | None = None, db: Session = Depends(get_db), _user: User = _view):
    rows = analytics_service.category_breakdown(db, month=month, status=status)
    return sorted(rows, key=lambda r: r["claimed_amount"], reverse=True)


@router.get("/exceptions", response_model=list[ExceptionTypePoint])
def exceptions(month: str | None = None, db: Session = Depends(get_db), _user: User = _view):
    rows = analytics_service.exception_type_breakdown(db, month=month)
    return sorted(rows, key=lambda r: r["count"], reverse=True)


@router.get("/exceptions/monthly", response_model=list[ExceptionMonthlyPoint])
def exceptions_monthly(db: Session = Depends(get_db), _user: User = _view):
    return analytics_service.exception_monthly_trend(db)


@router.get("/bills", response_model=BillMatchSummary)
def bills(month: str | None = None, db: Session = Depends(get_db), _user: User = _view):
    rows = analytics_service.bill_match_summary(db, month=month)
    return BillMatchSummary(by_status=rows)


@router.get("/trips", response_model=TripMatchSummary)
def trips(month: str | None = None, db: Session = Depends(get_db), _user: User = _view):
    return analytics_service.trip_match_summary(db, month=month)


@router.get("/trip-reports", response_model=TripAnalyticsOut)
def trip_reports(db: Session = Depends(get_db), _user: User = _view):
    return analytics_service.trip_analytics(db)


@router.get("/policy-limits", response_model=PolicyLimitAnalysisOut)
def policy_limits(month: str | None = None, db: Session = Depends(get_db), _user: User = _view):
    return analytics_service.policy_limit_analysis(db, month=month)


@router.get("/employee-claimed", response_model=list[EmployeeClaimedPoint])
def employee_claimed(db: Session = Depends(get_db), _user: User = _view):
    rows = db.execute(
        select(ExpenseReport.employee_name, func.coalesce(func.sum(ExpenseReport.total_claimed), 0))
        .where(ExpenseReport.employee_name.is_not(None))
        .group_by(ExpenseReport.employee_name)
        .order_by(ExpenseReport.employee_name)
    ).all()
    return [EmployeeClaimedPoint(employee_name=n, claimed_amount=float(total)) for n, total in rows]


@router.get("/months", response_model=list[str])
def available_months(db: Session = Depends(get_db), _user: User = _view):
    """Every distinct report_month with at least one Expense Report - used
    to populate the Analytics page's month filter dropdown with only real,
    selectable values (never a fabricated date range)."""
    rows = db.execute(
        select(ExpenseReport.report_month).where(ExpenseReport.report_month.is_not(None)).distinct()
    ).scalars().all()
    return sorted(rows, key=lambda m: tuple(int(p) for p in m.split("-")))
