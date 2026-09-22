from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.trip import TripReport
from app.schemas.dashboard import (
    CategoryBreakdownPoint,
    DashboardKPIs,
    MonthlyClaimedPoint,
    MonthlyUploadPoint,
    RecentUploadOut,
)
from app.services import analytics_service

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/kpis", response_model=DashboardKPIs)
def kpis(db: Session = Depends(get_db)):
    summary = analytics_service.report_status_summary(db)
    total_trips = (
        db.execute(select(func.count(TripReport.id)).where(TripReport.is_embedded.is_(False))).scalar() or 0
    )
    return DashboardKPIs(
        total_expense_reports=summary["total_reports"],
        total_trip_reports=total_trips,
        total_claimed_amount=summary["total_claimed_amount"],
        account_verified_reports=summary["account_verified_reports"],
        exception_reports=summary["exception_reports"],
        review_required_reports=summary["review_required_reports"],
        total_expenses=summary["total_expenses"],
    )


@router.get("/monthly-uploads", response_model=list[MonthlyUploadPoint])
def monthly_uploads(db: Session = Depends(get_db)):
    return analytics_service.monthly_report_volume(db)


@router.get("/monthly-claimed", response_model=list[MonthlyClaimedPoint])
def monthly_claimed(db: Session = Depends(get_db)):
    return analytics_service.monthly_claimed_trend(db)


@router.get("/category-breakdown", response_model=list[CategoryBreakdownPoint])
def category_breakdown(db: Session = Depends(get_db)):
    rows = analytics_service.category_breakdown(db)
    return sorted(rows, key=lambda r: r["claimed_amount"], reverse=True)


@router.get("/recent-uploads", response_model=list[RecentUploadOut])
def recent_uploads(db: Session = Depends(get_db)):
    return analytics_service.recent_uploads(db, limit=10)
