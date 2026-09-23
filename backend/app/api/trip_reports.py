import calendar

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.expense_report import Expense
from app.models.trip import Trip, TripReport
from app.models.trip_match import TripMatchCandidate
from app.schemas.trip_match import BattaMatchListItem
from app.schemas.trip_report import TripLegOut, TripReportDetailOut, TripReportListItem, TripReportMonthSummary
from app.models.user import User
from app.services.permissions import VIEW_TRIP_REPORTS, require_permission

router = APIRouter(prefix="/api/trip-reports", tags=["trip-reports"])
_view = Depends(require_permission(VIEW_TRIP_REPORTS))


def _month_label(report_month: str) -> str:
    year, month = report_month.split("-")
    return f"{calendar.month_name[int(month)]} {year}"


@router.get("/months", response_model=list[TripReportMonthSummary])
def list_months(db: Session = Depends(get_db), _user: User = _view):
    rows = db.execute(
        select(TripReport.report_month, func.count(TripReport.id))
        .where(TripReport.report_month.is_not(None), TripReport.is_embedded.is_(False))
        .group_by(TripReport.report_month)
        .order_by(TripReport.report_month.desc())
    ).all()
    return [
        TripReportMonthSummary(report_month=m, report_month_label=_month_label(m), report_count=c) for m, c in rows
    ]


@router.get("", response_model=list[TripReportListItem])
def list_trip_reports(report_month: str | None = None, db: Session = Depends(get_db), _user: User = _view):
    # Embedded trip summaries (extracted from inside an Expense Report PDF) are surfaced
    # on that Expense Report's detail page, not as their own standalone Trip Report row.
    stmt = select(TripReport).where(TripReport.is_embedded.is_(False)).order_by(TripReport.uploaded_at.desc())
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
            extraction_status=r.extraction_status,
            extraction_confidence=r.extraction_confidence,
            file_size=r.file_size,
            page_count=r.page_count,
            uploaded_at=r.uploaded_at,
        )
        for r in rows
    ]


@router.get("/{report_id}/details", response_model=TripReportDetailOut)
def get_trip_details(report_id: int, db: Session = Depends(get_db), _user: User = _view):
    report = db.get(TripReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Trip report not found.")
    legs = db.execute(select(Trip).where(Trip.trip_report_id == report_id).order_by(Trip.leg_no)).scalars().all()
    return TripReportDetailOut(
        trip_id=report.trip_id,
        employee_name=report.employee_name,
        employee_code=report.employee_code,
        start_date=report.start_date,
        end_date=report.end_date,
        origin=report.origin,
        destination=report.destination,
        purpose=report.purpose,
        approval_status=report.approval_status,
        approval_date=report.approval_date,
        approver=report.approver,
        extraction_status=report.extraction_status,
        extraction_confidence=report.extraction_confidence,
        legs=[
            TripLegOut(
                leg_no=leg.leg_no,
                from_location=leg.from_location,
                from_date=leg.from_date,
                to_location=leg.to_location,
                to_date=leg.to_date,
                description=leg.description,
            )
            for leg in legs
        ],
    )


@router.get("/{report_id}/batta-matches", response_model=list[BattaMatchListItem])
def get_batta_matches(report_id: int, db: Session = Depends(get_db), _user: User = _view):
    """Every Batta expense that considered this trip during matching - the
    confirmed match (if any) plus near-miss candidates, so an auditor can see
    why a trip was or wasn't selected."""
    report = db.get(TripReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Trip report not found.")

    candidate_rows = (
        db.execute(select(TripMatchCandidate).where(TripMatchCandidate.trip_report_id == report_id))
        .scalars()
        .all()
    )
    results = []
    for c in candidate_rows:
        expense = db.get(Expense, c.expense_id)
        if expense is None:
            continue
        results.append(
            BattaMatchListItem(
                expense_id=expense.id,
                expense_report_id=expense.expense_report_id,
                s_no=expense.s_no,
                expense_date=expense.expense_date,
                claimed_amount=expense.claimed_amount,
                is_matched_here=expense.trip_report_id == report_id,
                trip_match_status=expense.trip_match_status,
                score=c.score,
                reasons=c.reasons,
            )
        )
    results.sort(key=lambda r: (not r.is_matched_here, -(r.score or 0)))
    return results
