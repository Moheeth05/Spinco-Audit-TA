"""Safe, scoped cleanup for BillMatchCandidate/TripMatchCandidate rows whose
expense_id/bill_id/trip_report_id is about to be invalidated by a parent
Expense/Bill/TripReport delete-and-recreate (or delete) cycle.

See docs/ORPHAN_RECORD_INVESTIGATION.md for the bug this closes: neither
match-candidate model has a relationship() to its parents, so SQLAlchemy's
ORM cascade can never reach them, and every wholesale-replace/delete call
site previously deleted parent rows without ever deleting the candidate
rows that referenced their old ids.

Every function here takes an explicit, already-known list of ids - never a
broad/unscoped DELETE - and must be called BEFORE the corresponding parent
rows are deleted, with no db.commit() in between, so that if the caller's
own transaction rolls back, this cleanup rolls back with it.
"""

from sqlalchemy.orm import Session

from app.models.bill_match import BillMatchCandidate
from app.models.trip_match import TripMatchCandidate


def delete_candidates_for_expense_ids(db: Session, expense_ids: list[int]) -> None:
    """Call before deleting Expense rows with these ids - covers both
    candidate tables, since both reference expense_id."""
    if not expense_ids:
        return
    db.query(BillMatchCandidate).filter(BillMatchCandidate.expense_id.in_(expense_ids)).delete(
        synchronize_session=False
    )
    db.query(TripMatchCandidate).filter(TripMatchCandidate.expense_id.in_(expense_ids)).delete(
        synchronize_session=False
    )


def delete_bill_match_candidates_for_bill_ids(db: Session, bill_ids: list[int]) -> None:
    """Call before deleting Bill rows with these ids."""
    if not bill_ids:
        return
    db.query(BillMatchCandidate).filter(BillMatchCandidate.bill_id.in_(bill_ids)).delete(synchronize_session=False)


def delete_trip_match_candidates_for_trip_report_ids(db: Session, trip_report_ids: list[int]) -> None:
    """Call before deleting a TripReport row with one of these ids."""
    if not trip_report_ids:
        return
    db.query(TripMatchCandidate).filter(TripMatchCandidate.trip_report_id.in_(trip_report_ids)).delete(
        synchronize_session=False
    )
