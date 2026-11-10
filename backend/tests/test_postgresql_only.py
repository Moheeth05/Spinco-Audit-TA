"""Tests that only make sense on a SERVER database (PostgreSQL), which enforces foreign keys. They are skipped on SQLite - so they run only when the suite is started in PostgreSQL mode
(SPINCO_TEST_DATABASE_URL_FILE, see tests/conftest.py and the test report). They protect the two PostgreSQL fixes found by running this suite on a real PostgreSQL server:
  1. a freshly initialised PostgreSQL schema must pass the application's strict schema check (auto-increment keys are not "drift");
  2. the pipelines can delete / replace rows that other rows point at (fk_safety), and a manual verification is PARKED, never lost.
"""

import pytest
from sqlalchemy import select

from app import database
from app.database import SessionLocal
from app.models.expense_report import Expense, ExpenseReport
from app.models.user import User
from app.models.validation import ExceptionVerification
from app.services import central_storage, fk_safety

pytestmark = pytest.mark.skipif(database.IS_SQLITE, reason="PostgreSQL-only: SQLite does not enforce foreign keys")


def make_report(db):
    report = ExpenseReport(filename="ER-1.pdf", original_filename="ER-1.pdf", file_hash="h" * 64, file_size=1, source_path="x")
    db.add(report)
    db.flush()
    return report


def test_the_initialised_postgresql_schema_passes_the_strict_schema_check(client):
    assert central_storage.schema_problems() == []


def test_postgresql_really_refuses_a_dangling_pointer_so_the_helpers_are_needed(client):
    with SessionLocal() as db:
        report = make_report(db)
        expense = Expense(expense_report_id=report.id)
        db.add(expense)
        db.flush()
        admin = db.execute(select(User)).scalars().first()
        db.add(ExceptionVerification(expense_report_id=report.id, expense_id=expense.id, exception_type="OVER_LIMIT", original_severity="REVIEW REQUIRED", original_reason="r", verified_by_id=admin.id))
        db.commit()
        with pytest.raises(Exception, match="(?i)foreign key"):
            db.query(Expense).filter(Expense.expense_report_id == report.id).delete()
            db.commit()
        db.rollback()


def test_detach_expenses_parks_a_verification_keeps_its_history_and_lets_the_delete_succeed(client):
    with SessionLocal() as db:
        report = make_report(db)
        expense = Expense(expense_report_id=report.id)
        db.add(expense)
        db.flush()
        admin = db.execute(select(User)).scalars().first()
        verification = ExceptionVerification(expense_report_id=report.id, expense_id=expense.id, exception_type="OVER_LIMIT", original_severity="REVIEW REQUIRED",
                                             original_reason="the reason", verified_by_id=admin.id, verification_note="kept")
        db.add(verification)
        db.commit()
        vid, old_expense_id = verification.id, expense.id

        fk_safety.detach_expenses(db, [old_expense_id])
        db.query(Expense).filter(Expense.expense_report_id == report.id).delete()
        db.commit()

        row = db.get(ExceptionVerification, vid)
        assert row is not None, "a verification is audit history: it is never deleted"
        assert row.expense_id is None
        assert row.exception_type == fk_safety.PARKED + "OVER_LIMIT"  # matches no exception and no report-level verification
        assert (row.original_reason, row.verification_note, row.verified_by_id) == ("the reason", "kept", admin.id)
        assert fk_safety.peek_parked(db)[vid] == (old_expense_id, "OVER_LIMIT")


def test_the_helpers_do_nothing_on_an_empty_list(client):
    with SessionLocal() as db:
        fk_safety.detach_bills(db, [])
        fk_safety.detach_expenses(db, [])
        fk_safety.detach_validation_results(db, [])
        assert fk_safety.peek_parked(db) == {}
