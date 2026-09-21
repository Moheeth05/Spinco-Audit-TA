import calendar

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.trip import TripReport
from app.schemas.trip_report import TripReportListItem, TripReportMonthSummary

router = APIRouter(prefix="/api/trip-reports", tags=["trip-reports"])


def _month_label(report_month: str) -> str:
    year, month = report_month.split("-")
    return f"{calendar.month_name[int(month)]} {year}"


@router.get("/months", response_model=list[TripReportMonthSummary])
def list_months(db: Session = Depends(get_db)):
    rows = db.execute(
        select(TripReport.report_month, func.count(TripReport.id))
        .where(TripReport.report_month.is_not(None))
        .group_by(TripReport.report_month)
        .order_by(TripReport.report_month.desc())
    ).all()
    return [
        TripReportMonthSummary(report_month=m, report_month_label=_month_label(m), report_count=c) for m, c in rows
    ]


@router.get("", response_model=list[TripReportListItem])
def list_trip_reports(report_month: str | None = None, db: Session = Depends(get_db)):
    stmt = select(TripReport).order_by(TripReport.uploaded_at.desc())
    if report_month:
        stmt = stmt.where(TripReport.report_month == report_month)
    rows = db.execute(stmt).scalars().all()
    return [
        TripReportListItem(
            document_id=f"trip:{r.id}",
            trip_id=r.trip_id,
            filename=r.filename,
            original_filename=r.original_filename,
            employee_name=r.employee_name,
            employee_code=r.employee_code,
            start_date=r.start_date,
            end_date=r.end_date,
            origin=r.origin,
            destination=r.destination,
            approval_status=r.approval_status,
            processing_status=r.processing_status,
            error_message=r.error_message,
            file_size=r.file_size,
            page_count=r.page_count,
            uploaded_at=r.uploaded_at,
        )
        for r in rows
    ]
