"""Verification history: the superseded state of a manual verification is preserved when an auditor explicitly
re-verifies, so a replaced note survives even though new audit events never carry note text.

Synthetic rows and the disposable test databases only. No authentic PDF, no real database. The history table is
written only by the verify route and has no endpoint.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app import database
from app import models  # noqa: F401  (registers every model on Base)
from app.database import Base, SessionLocal
from app.main import app
from app.models.expense_report import Expense, ExpenseReport
from app.models.logs import AuditLog
from app.models.user import User
from app.models.validation import ExceptionVerification, ExceptionVerificationHistory
from app.services import audit

from test_verification_evidence_policy import (
    ROOM,
    _edit_validation_row,
    _reevaluate,
    _record,
    _row,
    _seed_review,
    _snapshot,
    _verification,
    _verify,
)

REVIEW_TEXT = "Room service / food included: Rs.{amount:,.2f} of Rs.5,100.00 total. Requires policy review."


def _history() -> list[ExceptionVerificationHistory]:
    db = SessionLocal()
    try:
        rows = db.query(ExceptionVerificationHistory).order_by(ExceptionVerificationHistory.id).all()
        for row in rows:
            db.expunge(row)
        return rows
    finally:
        db.close()


def _change_evidence(report_id: int, amount: float) -> None:
    """The room-service amount behind the review changes, so the standing approval no longer applies."""
    _edit_validation_row(report_id, bill_amount=amount, remarks=REVIEW_TEXT.format(amount=amount))
    _reevaluate(report_id)


def _history_snapshot(row: ExceptionVerificationHistory) -> dict:
    return {c.name: getattr(row, c.name) for c in ExceptionVerificationHistory.__table__.columns}


def test_the_first_verification_writes_no_history(client):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="first look")
    assert _history() == [], "there is nothing to supersede on a first verification"


def test_re_verifying_with_a_different_note_preserves_the_superseded_record_and_the_row_id(client):
    report_id, expense_id = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="first look")
    first = _snapshot(_verification(report_id))
    _change_evidence(report_id, 357.0)

    _verify(client, _record(client, report_id, ROOM), note="second look")

    live = _snapshot(_verification(report_id))
    assert live["id"] == first["id"], "the live row keeps its id"
    assert live["verification_note"] == "second look" and live["evidence_signature"] != first["evidence_signature"]
    (kept,) = _history()
    assert kept.verification_id == first["id"] and kept.expense_report_id == report_id and kept.expense_id == expense_id and kept.exception_type == ROOM
    assert kept.verification_note == "first look", "the replaced note is preserved"
    assert kept.verified_by_id == first["verified_by_id"] and kept.verified_at == first["verified_at"]
    assert kept.original_severity == first["original_severity"] and kept.original_reason == first["original_reason"]
    assert "Rs.957.00" in kept.original_reason, "what was approved first, as it was approved"
    assert kept.evidence_signature == first["evidence_signature"]
    assert kept.superseded_at is not None and kept.superseded_by_id == live["verified_by_id"]


def test_re_verifying_a_row_that_had_no_note_records_the_absent_note_as_none(client):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM))
    _change_evidence(report_id, 357.0)
    _verify(client, _record(client, report_id, ROOM), note="now with a note")
    (kept,) = _history()
    assert kept.verification_note is None
    assert _verification(report_id).verification_note == "now with a note"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_re_verifying_with_a_blank_note_keeps_the_live_note_and_still_preserves_the_superseded_record(client, blank):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="the original note")
    first = _snapshot(_verification(report_id))
    _change_evidence(report_id, 357.0)

    _verify(client, _record(client, report_id, ROOM), note=blank)

    live = _snapshot(_verification(report_id))
    assert live["verification_note"] == "the original note", "existing behaviour: a blank note does not replace the note"
    assert live["id"] == first["id"] and live["evidence_signature"] != first["evidence_signature"]
    (kept,) = _history()
    assert kept.verification_note == "the original note", "the superseded metadata is preserved even when the note was not replaced"
    assert kept.evidence_signature == first["evidence_signature"] and kept.verified_at == first["verified_at"]


def test_successive_re_verifications_keep_every_superseded_state_in_order(client):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="note 1")
    states = [_snapshot(_verification(report_id))]
    for amount, note in ((357.0, "note 2"), (210.0, "note 3"), (95.0, "note 4")):
        _change_evidence(report_id, amount)
        _verify(client, _record(client, report_id, ROOM), note=note)
        states.append(_snapshot(_verification(report_id)))

    kept = _history()
    assert [h.verification_note for h in kept] == ["note 1", "note 2", "note 3"], "one history row per replaced state, oldest first"
    assert [h.evidence_signature for h in kept] == [s["evidence_signature"] for s in states[:3]]
    assert [h.verified_at for h in kept] == [s["verified_at"] for s in states[:3]]
    assert [h.original_reason for h in kept] == [s["original_reason"] for s in states[:3]]
    assert len({h.evidence_signature for h in kept}) == 3
    assert {h.verification_id for h in kept} == {states[0]["id"]}, "every one points at the single live row"
    live = _snapshot(_verification(report_id))
    assert live["id"] == states[0]["id"] and live["verification_note"] == "note 4"
    db = SessionLocal()
    try:
        assert db.query(ExceptionVerification).count() == 1, "never a second live row"
    finally:
        db.close()


def test_history_rows_are_not_rewritten_by_later_evaluation_or_re_verification(client):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="note 1")
    _change_evidence(report_id, 357.0)
    _verify(client, _record(client, report_id, ROOM), note="note 2")
    before = [_history_snapshot(h) for h in _history()]
    _reevaluate(report_id)
    _reevaluate(report_id)
    assert [_history_snapshot(h) for h in _history()] == before, "evaluation never touches history"
    _change_evidence(report_id, 100.0)
    _verify(client, _record(client, report_id, ROOM), note="note 3")
    after = [_history_snapshot(h) for h in _history()]
    assert after[:1] == before, "an earlier history row is exactly as it was"
    assert [h["verification_note"] for h in after] == ["note 1", "note 2"]


def test_the_history_row_is_written_in_the_same_transaction_as_the_re_verification(client, monkeypatch):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="first look")
    before = _snapshot(_verification(report_id))
    _change_evidence(report_id, 357.0)
    stale = _record(client, report_id, ROOM)

    def failing_audit(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(audit, "log_action", failing_audit)
    try:
        resp = client.post(f"/api/exceptions/{stale['id']}/verify", json={"note": "must not be kept"})
        assert resp.status_code == 500
    except RuntimeError:
        pass  # the test client re-raises server errors

    assert _history() == [], "no history row survives a failed re-verification"
    assert _snapshot(_verification(report_id)) == before, "and the live verification is exactly as it was"


def test_re_verification_leaves_historical_audit_rows_untouched_and_adds_no_note_text(client):
    report_id, _ = _seed_review()
    db = SessionLocal()
    try:
        old = AuditLog(user_id=None, action=audit.EXCEPTION_VERIFIED, entity_type="ExceptionRecord", entity_id="1", details="X; note: a note written before this change")
        db.add(old)
        db.commit()
        old_id = old.id
    finally:
        db.close()

    _verify(client, _record(client, report_id, ROOM), note="note 1")
    _change_evidence(report_id, 357.0)
    _verify(client, _record(client, report_id, ROOM), note="note 2")

    db = SessionLocal()
    try:
        assert db.get(AuditLog, old_id).details == "X; note: a note written before this change"
        new = db.query(AuditLog).filter(AuditLog.action == audit.EXCEPTION_VERIFIED, AuditLog.id != old_id).all()
        assert len(new) == 2
        assert all("note 1" not in e.details and "note 2" not in e.details for e in new)
    finally:
        db.close()
    assert [h.verification_note for h in _history()] == ["note 1"], "the replaced note lives in history, not in the audit log"


# ================================================================ access: written only by the route, no endpoint reads it


def test_no_route_exposes_verification_history(client):
    paths = [getattr(route, "path", "") for route in app.routes]
    assert not [p for p in paths if "history" in p.lower()], "no history endpoint exists"
    for route in app.routes:
        model = getattr(route, "response_model", None)
        fields = getattr(model, "model_fields", None) or {}
        assert not [f for f in fields if "history" in f.lower()], f"{getattr(route, 'path', '')} must not carry history"

    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="note 1")
    _change_evidence(report_id, 357.0)
    _verify(client, _record(client, report_id, ROOM), note="note 2")
    api_view = client.get(f"/api/expense-reports/{report_id}/exceptions").json()
    assert "note 1" not in str(api_view), "the superseded note is not served by the exceptions API"
    assert all("history" not in key.lower() for item in api_view for key in item)


def test_history_rows_are_built_in_one_helper_and_written_only_by_the_verify_and_delete_routes():
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    sources = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in root.rglob("*.py")}
    constructs = re.compile(r"(?<!class )ExceptionVerificationHistory\(")
    assert sorted(path for path, text in sources.items() if constructs.search(text)) == ["services/verification_history.py"]
    callers = re.compile(r"verification_history\.(snapshot|retire_for_report)\(")
    assert sorted(path for path, text in sources.items() if callers.search(text)) == ["api/documents.py", "api/exceptions.py"]


# ================================================================ migration: the table is created additively by the startup path


@pytest.fixture
def startup_on(monkeypatch):
    from app import main as app_main

    def _run(engine):
        monkeypatch.setattr(app_main, "engine", engine)
        monkeypatch.setattr(app_main, "seed_all", lambda: None)
        app_main.on_startup()  # the application's real hook: run_light_migrations -> create_all -> seed (stubbed)

    return _run


def _disposable_engine(tmp_path, name):
    from pathlib import Path

    db_file = tmp_path / name
    assert tmp_path.resolve() in db_file.resolve().parents and db_file.resolve() != Path(database.DB_PATH).resolve()
    return create_engine(f"sqlite:///{db_file}")


HISTORY_COLUMNS = {
    "id", "verification_id", "expense_report_id", "expense_id", "exception_type", "original_severity", "original_reason",
    "evidence_signature", "verified_by_id", "verified_at", "verification_note", "superseded_at", "superseded_by_id",
}


def test_a_fresh_database_gets_the_history_table_from_startup(tmp_path, startup_on):
    engine = _disposable_engine(tmp_path, "fresh.db")
    startup_on(engine)
    assert "exception_verification_history" in inspect(engine).get_table_names()
    assert {c["name"] for c in inspect(engine).get_columns("exception_verification_history")} == HISTORY_COLUMNS


def _legacy_database(tmp_path, *, drop_signature: bool):
    """A database from before the history table (and optionally before the signature column), with real-looking rows."""
    engine = _disposable_engine(tmp_path, "legacy_history.db")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        report = ExpenseReport(filename="s.pdf", original_filename="s.pdf", file_hash="hist-mig", file_size=1, source_path="unused.pdf",
                               extraction_status="EXTRACTED", total_claimed=5100.0, er_id="ER-77301")
        session.add(report)
        session.commit()
        expense = Expense(expense_report_id=report.id, s_no=1, expense_date=date(2026, 7, 24), category="LODGING", claimed_amount=5100.0,
                          bill_match_status="MATCHED", trip_match_status="NOT_PROCESSED", validation_status="REVIEW_REQUIRED")
        user = User(name="Legacy", email="history@example.com", role="ADMIN")
        session.add_all([expense, user])
        session.commit()
        session.add(_row(expense_report_id=report.id, expense_id=expense.id))
        session.add(ExceptionVerification(expense_report_id=report.id, expense_id=expense.id, exception_type=ROOM, original_severity="REVIEW REQUIRED",
                                          original_reason="Room service / food included: Rs.957.00 of Rs.5,100.00 total. Requires policy review.",
                                          verified_by_id=user.id, verification_note="legacy approval"))
        session.add(AuditLog(user_id=user.id, action=audit.EXCEPTION_VERIFIED, entity_type="ExceptionRecord", entity_id="9", details="X; note: legacy approval"))
        session.commit()
    finally:
        session.close()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE exception_verification_history"))
        if drop_signature:
            conn.execute(text("ALTER TABLE exception_verifications DROP COLUMN evidence_signature"))
    assert "exception_verification_history" not in inspect(engine).get_table_names(), "fixture must be a real legacy schema"
    return engine


def _table_dump(engine, table: str) -> list:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(f"SELECT * FROM {table} ORDER BY id"))]


@pytest.mark.parametrize("drop_signature", [False, True])
def test_startup_adds_the_history_table_to_an_existing_database_without_touching_any_row_and_is_repeatable(tmp_path, startup_on, drop_signature):
    engine = _legacy_database(tmp_path, drop_signature=drop_signature)
    tables = ("exception_verifications", "audit_logs", "expenses", "expense_reports", "validation_results", "users")
    before = {t: _table_dump(engine, t) for t in tables}
    old_columns = [c["name"] for c in inspect(engine).get_columns("exception_verifications")]

    startup_on(engine)
    assert {c["name"] for c in inspect(engine).get_columns("exception_verification_history")} == HISTORY_COLUMNS
    assert _table_dump(engine, "exception_verification_history") == []

    startup_on(engine)  # a second launch is a no-op
    startup_on(engine)
    assert {c["name"] for c in inspect(engine).get_columns("exception_verification_history")} == HISTORY_COLUMNS

    for table, rows in before.items():
        if table == "exception_verifications" and drop_signature:
            # the signature column migration ran too: every pre-existing value is unchanged and the new column is NULL
            new_columns = [c["name"] for c in inspect(engine).get_columns(table)]
            keep = [new_columns.index(c) for c in old_columns]
            after = _table_dump(engine, table)
            assert [tuple(r[i] for i in keep) for r in after] == rows
            assert all(r[new_columns.index("evidence_signature")] is None for r in after)
        else:
            assert _table_dump(engine, table) == rows, f"{table} must be untouched"


def test_startup_does_not_recreate_or_empty_an_existing_history_table(tmp_path, startup_on):
    engine = _legacy_database(tmp_path, drop_signature=False)
    startup_on(engine)
    with engine.begin() as conn:
        verification_id = conn.execute(text("SELECT id FROM exception_verifications")).scalar_one()
        user_id = conn.execute(text("SELECT id FROM users")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO exception_verification_history (verification_id, expense_report_id, expense_id, exception_type, original_severity,"
                " original_reason, verified_by_id, verified_at, verification_note, superseded_at, superseded_by_id)"
                " VALUES (:v, 1, 1, :t, 'REVIEW REQUIRED', 'r', :u, '2026-07-01 00:00:00', 'kept note', '2026-07-02 00:00:00', :u)"
            ),
            {"v": verification_id, "t": ROOM, "u": user_id},
        )
    before = _table_dump(engine, "exception_verification_history")
    assert len(before) == 1
    startup_on(engine)
    startup_on(engine)
    assert _table_dump(engine, "exception_verification_history") == before


def test_run_light_migrations_alone_does_not_create_or_alter_the_history_table(tmp_path):
    """It is create_all (next in the startup path) that builds the table; the additive migration must simply tolerate its absence."""
    from app.services.db_migrations import run_light_migrations

    engine = _legacy_database(tmp_path, drop_signature=False)
    run_light_migrations(engine)
    run_light_migrations(engine)
    assert "exception_verification_history" not in inspect(engine).get_table_names()
