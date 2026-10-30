"""Legacy-database coverage for the long-bill evidence columns.

A realistic LEGACY `bills` schema (exactly the current schema minus the three
evidence columns, with data already in it) is built in a DISPOSABLE SQLite file
under pytest's tmp_path, migrated through the real startup path
(`app.main.on_startup`: run_light_migrations -> create_all -> seed), loaded
through the ORM, and run through the validation pipeline with the new fields
NULL. Nothing here opens `backend/spinco_ta.db`, production data or
`uat-data-test`; every database is a throw-away file or in-memory engine.
"""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app import database
from app import main as app_main
from app import models  # noqa: F401  (registers every model on Base)
from app.database import Base
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.validation import ExceptionRecord, ValidationResult
from app.services import db_migrations
from app.services.exception_engine import evaluate_report
from app.services.validation.pipeline import validate_expense_report

NEW_COLUMNS = ("catering_mentioned", "room_service_food_amount", "room_service_food_mentioned")

ROOM_SERVICE_READABLE = "Aurangabad Gymkhana Club\nRoom Charges 2,550.00\nROOM SERVICE Restaurant Charges RS26/3238 357.00\nRoom Charges 2,550.00\nTotal 5,457.00"
ROOM_SERVICE_UNREADABLE = "Aurangabad Gymkhana Club\nRoom Charges 2,550.00\nROOM SERVICE Restaurant Charges RS26/3238\nRoom Charges 2,550.00\nTotal 5,457.00"
NO_FOOD = "Aurangabad Gymkhana Club\nRate Type: Breakfast\nRoom Charges 2,550.00\nRoom Charges 2,550.00\nTotal 5,100.00"
CATERING_EARLY = "IRCTC e-ticket\nTicket fare 2,000.00\nCatering Charges (Incl. of GST) 368.00\nTotal Fare 2,368.00"


def _assert_disposable(engine, tmp_path: Path) -> None:
    db_file = Path(engine.url.database).resolve()
    assert tmp_path.resolve() in db_file.parents, "migration tests must only ever touch a throw-away file"
    assert db_file != Path(database.DB_PATH).resolve(), "never the application's own database file"
    assert "uat-data-test" not in str(db_file)


def _build_legacy_db(tmp_path: Path, dropped=NEW_COLUMNS):
    """Full current schema with legacy data, then the evidence columns dropped -> a legacy `bills` table."""
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    _assert_disposable(engine, tmp_path)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        report = ExpenseReport(filename="t.pdf", original_filename="t.pdf", file_hash="legacy-hash", file_size=100, source_path="unused.pdf",
                               extraction_status="EXTRACTED", total_claimed=17_000.0, er_id="ER-LEGACY")
        session.add(report)
        session.commit()
        cases = [  # (s_no, category, claimed, bill amount, bill raw_text)
            (1, "LODGING", 5100.0, 5457.0, ROOM_SERVICE_READABLE),
            (2, "LODGING", 5100.0, 5100.0, NO_FOOD),
            (3, "LODGING", 5100.0, 5457.0, ROOM_SERVICE_UNREADABLE),
            (4, "TRAIN / BUS FARE", 2000.0, 2368.0, CATERING_EARLY),
        ]
        for s_no, category, claimed, amount, raw in cases:
            bill = Bill(expense_report_id=report.id, page_number=4 + s_no, amount=amount, bill_date=date(2026, 9, 24), category_guess="Lodging" if category == "LODGING" else "Train",
                        raw_text=raw, extraction_status="EXTRACTED", match_status="MATCHED")
            session.add(bill)
            session.commit()
            session.add(Expense(expense_report_id=report.id, s_no=s_no, expense_date=date(2026, 9, 24), category=category, claimed_amount=claimed,
                                bill_id=bill.id, candidate_bill_id=bill.id, bill_match_status="MATCHED", trip_match_status="NOT_PROCESSED"))
            session.commit()
        report_id = report.id
    finally:
        session.close()
    with engine.begin() as conn:
        for column in dropped:
            conn.execute(text(f"ALTER TABLE bills DROP COLUMN {column}"))
    present = {c["name"] for c in inspect(engine).get_columns("bills")}
    assert not (set(dropped) & present), "fixture must really be a legacy schema"
    return engine, report_id


@pytest.fixture
def startup_on(monkeypatch):
    """Run the application's real startup hook against a given (disposable) engine; seeding is a recorded no-op."""
    seeded: list[int] = []

    def _run(engine):
        monkeypatch.setattr(app_main, "engine", engine)
        monkeypatch.setattr(app_main, "seed_all", lambda: seeded.append(1))
        app_main.on_startup()
        return seeded

    return _run


# ---------- migration through the real startup path ----------


@pytest.mark.parametrize(
    "dropped",
    [NEW_COLUMNS, ("room_service_food_amount",), ("catering_mentioned", "room_service_food_mentioned")],
    ids=["all-three-missing", "only-amount-missing", "only-flags-missing"],
)
def test_startup_migrates_a_legacy_database_additively_and_idempotently(tmp_path, startup_on, dropped):
    engine, _ = _build_legacy_db(tmp_path, dropped)
    with engine.connect() as conn:
        before = conn.execute(text("SELECT id, raw_text, amount, catering_amount FROM bills ORDER BY id")).all()

    seeded = startup_on(engine)
    after_first = {c["name"]: str(c["type"]) for c in inspect(engine).get_columns("bills")}
    assert set(NEW_COLUMNS) <= set(after_first)
    startup_on(engine)  # a second launch must be a no-op, never a crash or a duplicate column
    assert {c["name"]: str(c["type"]) for c in inspect(engine).get_columns("bills")} == after_first
    assert seeded == [1, 1]  # the startup hook ran to the end both times (migrate -> create_all -> seed)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT id, raw_text, amount, catering_amount FROM bills ORDER BY id")).all() == before  # data untouched
        added = {c: conn.execute(text(f"SELECT COUNT(*) FROM bills WHERE {c} IS NOT NULL")).scalar() for c in dropped}
    assert all(count == 0 for count in added.values()), "old rows must stay NULL, never be back-filled"


def test_negative_control_the_legacy_schema_is_unreadable_until_migrated(tmp_path, startup_on):
    from sqlalchemy.exc import OperationalError

    engine, report_id = _build_legacy_db(tmp_path)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        with pytest.raises(OperationalError):  # the ORM selects the new columns, which do not exist yet
            session.query(Bill).filter(Bill.expense_report_id == report_id).all()
    finally:
        session.rollback()
        session.close()
    startup_on(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        assert len(session.query(Bill).filter(Bill.expense_report_id == report_id).all()) == 4  # the migration is what fixes it
    finally:
        session.close()


def test_the_application_database_file_is_never_touched_by_these_tests(tmp_path):
    engine, _ = _build_legacy_db(tmp_path)
    assert Path(engine.url.database).resolve() != Path(database.DB_PATH).resolve()


# ---------- legacy rows through the ORM and the validation pipeline ----------


def _validated(tmp_path, startup_on):
    engine, report_id = _build_legacy_db(tmp_path)
    startup_on(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    return session, report_id


def _results(session, report_id):
    by_s_no: dict[int, dict[str, ValidationResult]] = {}
    expenses = {e.id: e.s_no for e in session.query(Expense).filter(Expense.expense_report_id == report_id)}
    for vr in session.query(ValidationResult).filter(ValidationResult.expense_report_id == report_id):
        if vr.expense_id in expenses:
            by_s_no.setdefault(expenses[vr.expense_id], {})[vr.check_type] = vr
    return by_s_no


def test_legacy_rows_load_through_the_orm_with_null_evidence(tmp_path, startup_on):
    session, report_id = _validated(tmp_path, startup_on)
    try:
        bills = session.query(Bill).filter(Bill.expense_report_id == report_id).order_by(Bill.id).all()
        assert len(bills) == 4
        for bill in bills:
            assert bill.room_service_food_amount is None
            assert bill.room_service_food_mentioned is None
            assert bill.catering_mentioned is None
            assert bill.catering_amount is None
            assert bill.raw_text  # the legacy text is intact
    finally:
        session.close()


def test_legacy_rows_validate_through_the_raw_text_fallback_without_inventing_anything(tmp_path, startup_on):
    session, report_id = _validated(tmp_path, startup_on)
    try:
        validate_expense_report(session, report_id)
        evaluate_report(session, report_id)
        results = _results(session, report_id)

        # 1. readable room-service charge in raw_text: reviewed, and excluded from the amount comparison
        assert results[1]["Room Service/Food Check"].result == "REVIEW_REQUIRED"
        assert "Rs.357.00" in results[1]["Room Service/Food Check"].remarks
        assert results[1]["Bill Amount Check"].result == "PASS"

        # 2. a breakfast-plan folio: nothing flagged
        assert results[2]["Room Service/Food Check"].result == "PASS"
        assert results[2]["Bill Amount Check"].result == "PASS"

        # 3. a row whose amount is unreadable: still reviewed, but nothing is invented or excluded
        unreadable = results[3]["Room Service/Food Check"]
        assert unreadable.result == "REVIEW_REQUIRED" and "could not be determined" in unreadable.remarks
        assert unreadable.bill_amount is None
        assert results[3]["Bill Amount Check"].result == "EXCEPTION"
        assert results[3]["Bill Amount Check"].exception_type == "AMOUNT MISMATCH"

        # 4. catering still works on a legacy row (raw_text fallback)
        assert results[4]["Catering Check"].result == "REVIEW_REQUIRED"
        assert "Rs.368.00" in results[4]["Catering Check"].remarks
        assert results[4]["Bill Amount Check"].result == "PASS"

        types = {
            (session.get(Expense, r.expense_id).s_no, r.exception_type)
            for r in session.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report_id)
            if r.expense_id is not None  # the report-level TOTAL MISMATCH belongs to no expense
        }
        assert (1, "ROOM SERVICE / FOOD INCLUDED") in types
        assert (3, "ROOM SERVICE / FOOD INCLUDED") in types and (3, "AMOUNT MISMATCH") in types
        assert (4, "CATERING INCLUDED") in types
        assert not any(s_no == 2 and t in ("ROOM SERVICE / FOOD INCLUDED", "AMOUNT MISMATCH") for s_no, t in types)

        # validation reads evidence, it never writes it back onto a legacy row
        session.expire_all()
        for bill in session.query(Bill).filter(Bill.expense_report_id == report_id):
            assert bill.room_service_food_amount is None and bill.room_service_food_mentioned is None and bill.catering_mentioned is None
    finally:
        session.close()


def test_repeat_validation_of_legacy_rows_does_not_duplicate_results(tmp_path, startup_on):
    session, report_id = _validated(tmp_path, startup_on)
    try:
        validate_expense_report(session, report_id)
        first = session.query(ValidationResult).filter(ValidationResult.expense_report_id == report_id).count()
        validate_expense_report(session, report_id)
        assert session.query(ValidationResult).filter(ValidationResult.expense_report_id == report_id).count() == first
    finally:
        session.close()


def test_run_light_migrations_alone_is_idempotent_on_a_fresh_full_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    _assert_disposable(engine, tmp_path)
    Base.metadata.create_all(bind=engine)
    before = [(c["name"], str(c["type"])) for c in inspect(engine).get_columns("bills")]
    db_migrations.run_light_migrations(engine)
    db_migrations.run_light_migrations(engine)
    assert [(c["name"], str(c["type"])) for c in inspect(engine).get_columns("bills")] == before
