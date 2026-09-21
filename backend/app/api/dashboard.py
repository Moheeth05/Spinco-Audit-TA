from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.bill import Bill
from app.models.expense_report import ExpenseReport
from app.models.trip import TripReport
from app.schemas.dashboard import (
    CategoryBreakdownPoint,
    DashboardKPIs,
    MonthlyClaimedPoint,
    MonthlyUploadPoint,
    RecentUploadOut,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/kpis", response_model=DashboardKPIs)
def kpis(db: Session = Depends(get_db)):
    total_ers = db.execute(select(func.count(ExpenseReport.id))).scalar() or 0
    total_trips = db.execute(select(func.count(TripReport.id))).scalar() or 0
    total_claimed = db.execute(select(func.coalesce(func.sum(ExpenseReport.total_claimed), 0))).scalar() or 0
    verified = db.execute(
        select(func.count(ExpenseReport.id)).where(ExpenseReport.overall_status == "ACCOUNT VERIFIED")
    ).scalar() or 0
    exceptions = db.execute(
        select(func.count(ExpenseReport.id)).where(ExpenseReport.overall_status == "EXCEPTION")
    ).scalar() or 0

    return DashboardKPIs(
        total_expense_reports=total_ers,
        total_trip_reports=total_trips,
        total_claimed_amount=float(total_claimed),
        account_verified_reports=verified,
        exception_reports=exceptions,
    )


@router.get("/monthly-uploads", response_model=list[MonthlyUploadPoint])
def monthly_uploads(db: Session = Depends(get_db)):
    er_rows = dict(
        db.execute(
            select(ExpenseReport.report_month, func.count(ExpenseReport.id))
            .where(ExpenseReport.report_month.is_not(None))
            .group_by(ExpenseReport.report_month)
        ).all()
    )
    trip_rows = dict(
        db.execute(
            select(TripReport.report_month, func.count(TripReport.id))
            .where(TripReport.report_month.is_not(None))
            .group_by(TripReport.report_month)
        ).all()
    )
    months = sorted(set(er_rows) | set(trip_rows))
    return [
        MonthlyUploadPoint(month=m, expense_reports=er_rows.get(m, 0), trip_reports=trip_rows.get(m, 0))
        for m in months
    ]


@router.get("/monthly-claimed", response_model=list[MonthlyClaimedPoint])
def monthly_claimed(db: Session = Depends(get_db)):
    rows = db.execute(
        select(ExpenseReport.report_month, func.coalesce(func.sum(ExpenseReport.total_claimed), 0))
        .where(ExpenseReport.report_month.is_not(None))
        .group_by(ExpenseReport.report_month)
        .order_by(ExpenseReport.report_month)
    ).all()
    return [MonthlyClaimedPoint(month=m, claimed_amount=float(total)) for m, total in rows]


@router.get("/category-breakdown", response_model=list[CategoryBreakdownPoint])
def category_breakdown(db: Session = Depends(get_db)):
    from app.models.expense_report import Expense

    rows = db.execute(
        select(Expense.category, func.coalesce(func.sum(Expense.claimed_amount), 0))
        .where(Expense.category.is_not(None))
        .group_by(Expense.category)
        .order_by(Expense.category)
    ).all()
    return [CategoryBreakdownPoint(category=c, claimed_amount=float(total)) for c, total in rows]


@router.get("/recent-uploads", response_model=list[RecentUploadOut])
def recent_uploads(db: Session = Depends(get_db)):
    ers = db.execute(select(ExpenseReport).order_by(ExpenseReport.uploaded_at.desc()).limit(10)).scalars().all()
    trips = db.execute(select(TripReport).order_by(TripReport.uploaded_at.desc()).limit(10)).scalars().all()

    items = [
        RecentUploadOut(
            filename=er.filename,
            person_name=er.employee_name,
            file_type="Expense",
            report_month_label=er.report_month_label,
            status=er.overall_status,
            uploaded_at=er.uploaded_at,
        )
        for er in ers
    ] + [
        RecentUploadOut(
            filename=t.filename or t.trip_id,
            person_name=t.employee_name,
            file_type="Trip",
            report_month_label=t.report_month,
            status=t.validation_status or "PROCESSED",
            uploaded_at=t.uploaded_at,
        )
        for t in trips
    ]
    items.sort(key=lambda i: i.uploaded_at, reverse=True)
    return items[:10]
