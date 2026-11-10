"""Foreign-key-safe deletes for server databases (PostgreSQL).

SQLite does not enforce foreign keys unless asked to (the application never asks), so the extraction / validation pipelines have always been free to delete a Bill, an
Expense or a ValidationResult while another row still points at it: the pointer simply dangles until the pipeline rewrites it a moment later. PostgreSQL enforces
foreign keys, and refuses such a delete (ForeignKeyViolation) - found by running the backend test-suite against a real PostgreSQL server.

These helpers clear the pointers (SET NULL on nullable columns) immediately BEFORE the delete, in the same transaction. They do NOTHING on SQLite, so the behaviour of the
desktop app, the staging harness and every existing SQLite database is byte-for-byte unchanged; they only matter where the database enforces the constraints.
"""

import weakref

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.bill import Bill
from app.models.expense_report import Expense
from app.models.logs import ProcessingLog
from app.models.trip import TripReport
from app.models.validation import ExceptionRecord, ExceptionVerification, ValidationResult


def _enforces_foreign_keys(db: Session) -> bool:
    return db.get_bind().dialect.name != "sqlite"


def _null(db: Session, model, column, ids) -> None:
    db.execute(update(model).where(column.in_(ids)).values({column.key: None}).execution_options(synchronize_session=False))


def detach_bills(db: Session, bill_ids: list[int]) -> None:
    """Call before deleting Bill rows with these ids."""
    if not bill_ids or not _enforces_foreign_keys(db):
        return
    _null(db, Expense, Expense.bill_id, bill_ids)
    _null(db, Expense, Expense.candidate_bill_id, bill_ids)
    _null(db, ExceptionRecord, ExceptionRecord.bill_id, bill_ids)


PARKED = "~parked~"  # prefix of the exception_type of a manual verification whose claim row is being replaced (it then matches no exception)
# What was parked in each Session ({verification id: (old expense id, original type)}). Kept per Session in a weak map (not on the session's own attributes) so the entry
# disappears with the session; verification_carryover.apply() takes it.
_PARKED: "weakref.WeakKeyDictionary[Session, dict[int, tuple[int, str]]]" = weakref.WeakKeyDictionary()


def take_parked(db: Session) -> dict[int, tuple[int, str]]:
    return _PARKED.pop(db, {})


def peek_parked(db: Session) -> dict[int, tuple[int, str]]:
    return dict(_PARKED.get(db, {}))


def detach_expenses(db: Session, expense_ids: list[int]) -> None:
    """Call before deleting Expense rows with these ids.

    A manual verification (ExceptionVerification) is audit history and must never be deleted, but on SQLite it was allowed to keep pointing at the deleted expense id (a
    "dangling" pointer that matches nothing - verification_carryover re-points it when the claim is confidently recognised). PostgreSQL forbids a dangling pointer, so the row is
    PARKED: its expense_id is cleared and its exception_type is prefixed, so it matches no exception and no report-level verification, all its other facts untouched; the
    original values are remembered on the session for verification_carryover.apply(), which restores the type and points it at the NEW claim when the match is confident, and
    otherwise leaves it parked (history, exactly like the stranded row on SQLite)."""
    if not expense_ids or not _enforces_foreign_keys(db):
        return
    parked: dict[int, tuple[int, str]] = _PARKED.setdefault(db, {})
    for verification in db.query(ExceptionVerification).filter(ExceptionVerification.expense_id.in_(expense_ids)).all():
        parked[verification.id] = (verification.expense_id, verification.exception_type)
        verification.expense_id = None
        verification.exception_type = (PARKED + verification.exception_type)[:100]
    db.flush()
    _null(db, Bill, Bill.matched_expense_id, expense_ids)
    _null(db, ExceptionRecord, ExceptionRecord.expense_id, expense_ids)
    _null(db, ValidationResult, ValidationResult.expense_id, expense_ids)


def detach_report_dependents(db: Session, report_id: int, expense_ids: list[int], bill_ids: list[int]) -> None:
    """Call before deleting an ExpenseReport (the ORM cascade then removes its Expense / Bill rows). Clears every other row's pointer to this report's bills and expenses and removes the
    report's own validation results and exception records (they require the report, so they cannot be kept once it is gone; SQLite leaves them behind as unreachable orphans)."""
    if not _enforces_foreign_keys(db):
        return
    detach_bills(db, bill_ids)
    detach_expenses(db, expense_ids)
    _null(db, ProcessingLog, ProcessingLog.expense_report_id, [report_id])  # the processing history is kept, just no longer attached to a report that is gone
    _null(db, TripReport, TripReport.source_expense_report_id, [report_id])  # an embedded trip report keeps living on its own
    db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report_id).delete(synchronize_session=False)
    db.query(ValidationResult).filter(ValidationResult.expense_report_id == report_id).delete(synchronize_session=False)
    db.flush()


def detach_trip_report(db: Session, trip_report_id: int) -> None:
    """Call before deleting a TripReport: expenses and exception records may point at it (nullable)."""
    if not _enforces_foreign_keys(db):
        return
    _null(db, Expense, Expense.trip_report_id, [trip_report_id])
    _null(db, ExceptionRecord, ExceptionRecord.trip_report_id, [trip_report_id])
    _null(db, ProcessingLog, ProcessingLog.trip_report_id, [trip_report_id])


def detach_validation_results(db: Session, result_ids: list[int]) -> None:
    """Call before deleting ValidationResult rows with these ids."""
    if not result_ids or not _enforces_foreign_keys(db):
        return
    _null(db, ExceptionRecord, ExceptionRecord.validation_result_id, result_ids)
