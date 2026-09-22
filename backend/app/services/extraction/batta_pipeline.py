"""Batta-to-Trip matching orchestration for one Expense Report, run
automatically right after Phase 3's header/expense-line extraction and its
embedded TRIP SUMMARY block. Reuses trip_matcher.py's deterministic scoring
the same way bill_pipeline.py wraps bill_matcher.py.

Also provides the re-evaluation entrypoint used when a standalone Trip
Report PDF is uploaded (or reprocessed) after a Batta expense was already
extracted with no matching trip available - see
`reevaluate_unresolved_batta_for_trip`.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.expense_report import Expense, ExpenseReport
from app.models.trip import Trip, TripReport
from app.models.trip_match import TripMatchCandidate
from app.services.category_map import normalize_category
from app.services.extraction import trip_matcher

USABLE_TRIP_STATUSES = ("EXTRACTED", "REVIEW_REQUIRED")


def _trip_to_dict(trip: TripReport, legs: list[Trip]) -> dict[str, Any]:
    return {
        "id": trip.id,
        "trip_id": trip.trip_id,
        "is_embedded": trip.is_embedded,
        "employee_code": trip.employee_code,
        "employee_name": trip.employee_name,
        "employee_email": None,  # Trip/TripReport never stores email directly - see Employee lookup
        "start_date": trip.start_date,
        "end_date": trip.end_date,
        "destination": trip.destination,
        "legs": [{"from_date": leg.from_date, "to_date": leg.to_date} for leg in legs],
    }


def gather_candidate_trips(db: Session, report: ExpenseReport) -> list[dict[str, Any]]:
    """All trips a Batta expense on this report could plausibly match: this
    report's own embedded TRIP SUMMARY (if any, regardless of extraction
    confidence - it's direct evidence) plus every usable standalone Trip
    Report currently in the system. Deliberately unfiltered by date/employee
    here - trip_matcher.py's scoring (and the employee-mismatch exclusion)
    does that filtering deterministically and explainably."""
    trips: list[TripReport] = []

    embedded = (
        db.query(TripReport)
        .filter(TripReport.is_embedded.is_(True), TripReport.source_expense_report_id == report.id)
        .first()
    )
    if embedded is not None:
        trips.append(embedded)

    standalone = (
        db.query(TripReport)
        .filter(TripReport.is_embedded.is_(False), TripReport.extraction_status.in_(USABLE_TRIP_STATUSES))
        .all()
    )
    trips.extend(standalone)

    if not trips:
        return []

    legs_by_trip = {}
    for leg in db.query(Trip).filter(Trip.trip_report_id.in_([t.id for t in trips])).all():
        legs_by_trip.setdefault(leg.trip_report_id, []).append(leg)

    return [_trip_to_dict(t, legs_by_trip.get(t.id, [])) for t in trips]


def _is_batta(category: str | None) -> bool:
    return normalize_category(category) == "BATTA"


def match_batta_expenses(db: Session, report: ExpenseReport) -> None:
    """Matches (or re-matches) every BATTA expense on this report against
    currently-available trip evidence, replacing trip_match_* fields and
    TripMatchCandidate rows wholesale - the same "replace, never accumulate"
    pattern used by bill matching and validation."""
    expenses = db.query(Expense).filter(Expense.expense_report_id == report.id).all()
    batta_expenses = [e for e in expenses if _is_batta(e.category)]
    if not batta_expenses:
        return

    candidate_trips = gather_candidate_trips(db, report)

    embedded = (
        db.query(TripReport)
        .filter(TripReport.is_embedded.is_(True), TripReport.source_expense_report_id == report.id)
        .first()
    )
    embedded_trip_id = embedded.trip_id if embedded else None

    employee_email = None
    if report.employee_id:
        employee = db.get(Employee, report.employee_id)
        employee_email = employee.email if employee else None

    db.query(TripMatchCandidate).filter(
        TripMatchCandidate.expense_id.in_([e.id for e in batta_expenses])
    ).delete(synchronize_session=False)

    for expense in batta_expenses:
        expense_dict = {
            "expense_date": expense.expense_date,
            "expense_details": expense.expense_details,
            "employee_code": report.employee_code,
            "employee_name": report.employee_name,
            "employee_email": employee_email,
            "embedded_trip_id": embedded_trip_id,
        }
        outcome = trip_matcher.match_batta(expense_dict, candidate_trips)

        expense.trip_match_status = outcome.status
        expense.trip_match_score = outcome.score
        expense.trip_match_confidence = outcome.confidence
        expense.trip_match_reasons = "; ".join(outcome.reasons) if outcome.reasons else None
        expense.trip_employee_mismatch = outcome.employee_mismatch
        expense.trip_report_id = outcome.matched_trip_report_id if outcome.status == "MATCHED" else None

        rank = 0
        for candidate in outcome.candidates:
            rank += 1
            db.add(
                TripMatchCandidate(
                    expense_id=expense.id,
                    trip_report_id=candidate.trip_report_id,
                    rank=rank,
                    score=candidate.score,
                    reasons="; ".join(candidate.reasons) if candidate.reasons else None,
                )
            )
            # A same-trip-number duplicate (e.g. this Expense Report's embedded
            # trip summary vs. a standalone Trip Report for the same trip) was
            # collapsed into `candidate` above so it never shows up as a false
            # MULTIPLE_CANDIDATES ambiguity - but it still needs its own row so
            # that trip's own "batta-matches" page can find this expense too.
            for merged_trip_id in candidate.merged_trip_report_ids:
                rank += 1
                db.add(
                    TripMatchCandidate(
                        expense_id=expense.id,
                        trip_report_id=merged_trip_id,
                        rank=rank,
                        score=candidate.score,
                        reasons=f"Same trip as the primary match (trip_report_id={candidate.trip_report_id}); not selected as the primary source.",
                    )
                )
    db.commit()


def reevaluate_unresolved_batta_for_trip(db: Session, trip: TripReport) -> set[int]:
    """Called after a standalone Trip Report is (re)extracted. Every Expense
    Report with at least one BATTA expense gets its Batta matching re-run
    against the full, now-updated candidate pool (which includes this trip).

    This deliberately re-runs even for expenses already MATCHED (e.g. to an
    embedded trip summary), not just unresolved ones: match_batta_expenses is
    deterministic and idempotent, so a re-run is cheap and never changes an
    already-correct match, but it IS what lets a same-trip-number standalone
    Trip Report uploaded after the fact get linked as corroborating evidence
    (see trip_matcher's same-trip dedup) - without this, that standalone
    trip's own "linked Batta claims" view would silently show nothing even
    though it evidences the same real trip. Returns the set of
    expense_report_ids that were touched, so the caller can re-run policy
    validation on exactly those reports."""
    if trip.is_embedded:
        return set()

    batta_expense_report_ids = {
        e.expense_report_id for e in db.query(Expense.expense_report_id, Expense.category).all() if _is_batta(e.category)
    }
    touched: set[int] = set()
    for report_id in batta_expense_report_ids:
        report = db.get(ExpenseReport, report_id)
        if report is None:
            continue
        match_batta_expenses(db, report)
        touched.add(report_id)

    return touched
