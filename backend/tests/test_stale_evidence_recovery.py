"""Rehearsal of the stale-evidence recovery procedure (docs/RELEASE_AND_UPDATES.md,
"Pre-release check: stale catering / room-service-food evidence") on DISPOSABLE,
SYNTHETIC data only.

Nothing here touches a real database or an authentic document: the `client` fixture's
database and storage are a fresh temp directory (see conftest.py, which points
SPINCO_DB_PATH / SPINCO_STORAGE_ROOT there BEFORE the app is imported, and
`test_the_test_database_is_the_disposable_one` below asserts it), every PDF is rendered
by tests/synthetic_reports.py, and the file-level backup/restore tests use their own
throw-away SQLite files under pytest's tmp_path.

  1. rematch-bills heals stale stored evidence on one report and leaves another untouched.
  2. CHARACTERIZATION of the full /reprocess route: what happens to a manual verification.
  3. Backup -> simulated failure after the deletion commit -> restore (file level).
  4. The candidate-report predicate on legacy / migrated / partially migrated schemas.
"""

import hashlib
import os
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

import synthetic_cases
import synthetic_reports as sr
from app import database
from app import models  # noqa: F401  (registers every model on Base)
from app.database import Base, SessionLocal
from app.models.bill import Bill
from app.models.bill_match import BillMatchCandidate
from app.models.expense_report import Expense, ExpenseReport
from app.models.logs import AuditLog
from app.models.validation import ExceptionRecord, ExceptionVerification
from app.services import storage
from app.services.exception_engine import evaluate_report
from app.services.extraction import bill_parser, bill_pipeline
from app.services.extraction.text_extraction import PyMuPDFTextExtractor
from app.services.validation.pipeline import validate_expense_report

NEW_BILL_COLUMNS = ("catering_mentioned", "room_service_food_amount", "room_service_food_mentioned")


# ---------------------------------------------------------------- safety: only ever the disposable database


def test_the_test_database_is_the_disposable_one():
    db_path = Path(database.DB_PATH).resolve()
    assert "spinco_test_" in str(db_path), "the suite must run on the temp database set up by conftest.py"
    assert db_path != (Path(database.BACKEND_DIR) / "spinco_ta.db").resolve(), "never the application's own database file"
    assert "uat-data-test" not in str(db_path)
    assert "spinco_test_" in str(Path(storage.STORAGE_ROOT)), "document storage must be the temp storage root too"


# ---------------------------------------------------------------- synthetic documents


def _lodging_case(er_id: str) -> sr.Case:
    """A synthetic ER with ONE lodging claim (5,100.00) and one hotel folio on page 2:
    room charges 2 x 2,550.00 + a ROOM SERVICE row of 357.00 = 5,457.00, plus a printed
    'CATERING SERVICE' form header followed by an unrelated 'ROOM RENT 1607.00' row (the
    cross-line catering defect's trigger). Correct evidence: food 357.00, no catering amount."""
    day = date(2026, 7, 24)
    folio = sr.Receipt(
        "r1", "folio_room_service",
        ["AURANGABAD GYMKHANA HOTEL", "TAX INVOICE", "Invoice No 2526/2923", f"Check-in {day.strftime('%d/%m/%Y')}", "Check-out 25/07/2026",
         "CATERING SERVICE", "ROOM RENT 1607.00", "Room Charges 2,550.00", "ROOM SERVICE Restaurant Charges RS26/3238 357.00",
         "Room Charges 2,550.00", "Total 5,457.00"],
        5457.0, day, None, "GYMKHANA", kind="invoice", category_evidence="Lodging",
    )
    claim = sr.Claim(1, day, "LODGING", "Hotel stay", "Aurangabad Gymkhana", 5100.0, expect="SUPPORTED", acceptable={"r1"})
    return sr.Case("lodging_room_service", "dev", "L1", [claim], [folio], er_id=er_id)


def _upload(client, case: sr.Case) -> int:
    pdf, _page_of = sr.render_er(case)
    resp = client.post("/api/uploads/expense-reports", files=[("files", (f"{case.er_id}.pdf", pdf, "application/pdf"))])
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["success"] is True, result
    return int(result["document_id"].split(":")[1])


def _exceptions(client, report_id: int) -> list[dict]:
    resp = client.get(f"/api/expense-reports/{report_id}/exceptions")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _of_type(items: list[dict], exception_type: str) -> list[dict]:
    return [e for e in items if e["exception_type"] == exception_type]


def _state(report_id: int) -> dict:
    """Everything recovery may or may not change for one report, read back from the disposable database."""
    db = SessionLocal()
    try:
        bills = db.query(Bill).filter(Bill.expense_report_id == report_id).order_by(Bill.id).all()
        expenses = db.query(Expense).filter(Expense.expense_report_id == report_id).order_by(Expense.s_no).all()
        records = db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report_id).order_by(ExceptionRecord.id).all()
        verifications = db.query(ExceptionVerification).filter(ExceptionVerification.expense_report_id == report_id).order_by(ExceptionVerification.id).all()

        def num(value):
            return None if value is None else float(value)

        return {
            "bills": [
                (b.id, b.page_number, num(b.amount), num(b.catering_amount), b.catering_mentioned, num(b.room_service_food_amount), b.room_service_food_mentioned)
                for b in bills
            ],
            "bill_ids": [b.id for b in bills],
            "expense_ids": {e.s_no: e.id for e in expenses},
            "expense_bill_ids": {e.s_no: e.bill_id for e in expenses},
            "records": [(r.expense_id, r.exception_type, r.severity, r.status) for r in records],
            "verifications": [(v.expense_id, v.exception_type) for v in verifications],
        }
    finally:
        db.close()


def _orphan_counts() -> dict:
    db = SessionLocal()
    try:
        return {
            "candidates_missing_bill": db.query(BillMatchCandidate).filter(~BillMatchCandidate.bill_id.in_(db.query(Bill.id))).count(),
            "candidates_missing_expense": db.query(BillMatchCandidate).filter(~BillMatchCandidate.expense_id.in_(db.query(Expense.id))).count(),
        }
    finally:
        db.close()


def _audit(action: str, entity_id: str) -> int:
    db = SessionLocal()
    try:
        return db.query(AuditLog).filter(AuditLog.action == action, AuditLog.entity_id == entity_id).count()
    finally:
        db.close()


# ================================================================ TEST 1: rematch-bills heals stale evidence


def test_rematch_bills_replaces_stale_evidence_and_leaves_the_other_report_untouched(client):
    report_a = _upload(client, _lodging_case("ER-72001"))
    report_b = _upload(client, synthetic_cases.build_cases("dev", 2)[0])  # a second, unrelated synthetic report
    doc_a = f"expense:{report_a}"

    # ---- the freshly extracted evidence is already correct (sanity: the detectors are current)
    fresh = _state(report_a)
    assert len(fresh["bills"]) == 1
    _bill_id, _page, bill_amount, catering_amount, catering_flag, food_amount, food_flag = fresh["bills"][0]
    assert bill_amount == 5457.0
    assert food_amount == 357.0 and food_flag is True
    assert catering_amount is None and catering_flag is True  # the "CATERING SERVICE / ROOM RENT 1607.00" header is a mention only
    assert fresh["expense_bill_ids"][1] == fresh["bill_ids"][0], "the 5,100.00 claim must be matched to the folio"
    assert _of_type(_exceptions(client, report_a), "AMOUNT MISMATCH") == []

    # ---- reproduce what an OLDER working-tree detector would have stored, then re-run validation exactly as the app does
    db = SessionLocal()
    try:
        bill = db.query(Bill).filter(Bill.expense_report_id == report_a).one()
        bill.catering_amount = 1607.00  # the unrelated "ROOM RENT" amount captured as catering (cross-line defect)
        bill.catering_mentioned = True
        bill.room_service_food_amount = 957.00  # a wrong stored food total
        bill.room_service_food_mentioned = True
        db.commit()
        validate_expense_report(db, report_a)
        evaluate_report(db, report_a)
    finally:
        db.close()

    stale = _exceptions(client, report_a)
    assert len(_of_type(stale, "AMOUNT MISMATCH")) == 1, "ERRONEOUS pre-recovery outcome: a correct claim now shows an amount mismatch"
    stale_review = _of_type(stale, "ROOM SERVICE / FOOD INCLUDED")
    assert len(stale_review) == 1 and "Rs.957.00" in stale_review[0]["reason"], "and the review states the wrong stored amount"

    # a manual verification the auditor already made on the review item (must survive recovery)
    verify = client.post(f"/api/exceptions/{stale_review[0]['id']}/verify", json={"note": "checked against the folio"})
    assert verify.status_code == 200, verify.text
    assert verify.json()["status"] == "RESOLVED"

    before_a = _state(report_a)
    before_b = _state(report_b)
    assert before_a["verifications"] == [(before_a["expense_ids"][1], "ROOM SERVICE / FOOD INCLUDED")]
    workbook = Path(storage.EXCEL_DIR) / "Spinco_TA_Audit_Consolidated.xlsx"
    workbook_mtime_before = workbook.stat().st_mtime_ns if workbook.exists() else None

    # ---- recovery: the real route, through the project's authenticated test client
    resp = client.post(f"/api/documents/{doc_a}/rematch-bills")
    assert resp.status_code == 200, resp.text

    # ---- the bills were recreated and carry CORRECT evidence again
    after_a = _state(report_a)
    assert len(after_a["bills"]) == 1
    assert after_a["bill_ids"] != before_a["bill_ids"], "the bill row must have been recreated, not edited in place"
    _id, _page, amount, catering_amount, catering_flag, food_amount, food_flag = after_a["bills"][0]
    assert amount == 5457.0
    assert food_amount == 357.0 and food_flag is True
    assert catering_amount is None and catering_flag is True, "stale 1607.00 gone; the mention flag is preserved"

    # ---- the claim's exceptions were recalculated
    recovered = _exceptions(client, report_a)
    assert _of_type(recovered, "AMOUNT MISMATCH") == [], "the false amount mismatch is gone"
    review = _of_type(recovered, "ROOM SERVICE / FOOD INCLUDED")
    assert len(review) == 1 and "Rs.357.00" in review[0]["reason"] and "Rs.957.00" not in review[0]["reason"]

    # ---- the manual verification was given on evidence quoting Rs.957.00; the recovered evidence quotes Rs.357.00.
    # Evidence signature policy: that approval must NOT be re-applied. The review is OPEN (with the stable
    # "no longer applies" note), the verification row is retained untouched as history, and the expense row
    # was not recreated so the row still points at an existing expense.
    assert after_a["expense_ids"] == before_a["expense_ids"]
    assert after_a["verifications"] == before_a["verifications"], "the historical verification row is retained, not deleted or rewritten"
    assert review[0]["status"] == "OPEN" and not review[0]["verification_note"], "the obsolete approval is not applied"
    assert "no longer applies" in review[0]["reason"] and review[0]["reason"].count("no longer applies") == 1
    db = SessionLocal()
    try:
        current_expense_ids = {e.id for e in db.query(Expense).filter(Expense.expense_report_id == report_a)}
        claim = db.query(Expense).filter(Expense.expense_report_id == report_a, Expense.s_no == 1).one()
        assert claim.validation_status != "VERIFIED", "the stale approval must not mark the expense VERIFIED"
        assert db.get(ExpenseReport, report_a).overall_status != "ACCOUNT VERIFIED", "nor can it make the report ACCOUNT VERIFIED"
    finally:
        db.close()
    assert {expense_id for expense_id, _t in after_a["verifications"]} <= current_expense_ids

    # ---- the second report is completely unchanged
    assert _state(report_b) == before_b

    # ---- no orphaned match candidates anywhere
    assert _orphan_counts() == {"candidates_missing_bill": 0, "candidates_missing_expense": 0}

    # ---- the audit trail: exactly one BILL_REMATCHED for this document (an audit row alone proves nothing - the checks above do)
    assert _audit("BILL_REMATCHED", doc_a) == 1
    assert _audit("BILL_REMATCHED", f"expense:{report_b}") == 0
    assert _audit("DOCUMENT_REPROCESSED", doc_a) == 0

    # ---- the consolidated workbook was (re)generated; its CONTENT is not verified here
    assert workbook.exists()
    if workbook_mtime_before is not None:
        assert workbook.stat().st_mtime_ns >= workbook_mtime_before


# ================================================================ TEST 2: full /reprocess - what happens to a manual verification


def test_full_reprocess_preserves_a_manual_verification_for_the_same_claim(client):
    """Full /reprocess recreates a report's Expense rows with NEW ids. This test originally CHARACTERIZED the
    defect (the verification stayed on the old expense id and the regenerated exception came back OPEN);
    since extraction/verification_carryover.py it pins the FIXED behaviour: the verification follows its claim.

    Two synthetic reports are loaded and the OLDER one is reprocessed, so SQLite cannot reuse the freed ids
    (a single-report database would - which is why test_exception_engine.py::test_29 could not tell the
    difference). The full policy matrix lives in test_verification_carryover.py."""
    report_a = _upload(client, synthetic_cases.build_cases("dev", 1)[6])  # scenario "missing_bill": claim 2 has a MISSING BILL exception
    report_b = _upload(client, synthetic_cases.build_cases("dev", 2)[6])
    assert report_a != report_b

    missing = _of_type([e for e in _exceptions(client, report_a)], "MISSING BILL")
    assert len(missing) == 1, "the scenario must give report A exactly one confirmed MISSING BILL exception"
    verify = client.post(f"/api/exceptions/{missing[0]['id']}/verify", json={"note": "accepted by the auditor"})  # an EXCEPTION needs a note
    assert verify.status_code == 200, verify.text
    assert verify.json()["status"] == "RESOLVED"

    def verification_rows(report_id):
        db = SessionLocal()
        try:
            return [
                (v.id, v.expense_id, v.exception_type, v.verified_by_id, v.verified_at, v.verification_note, v.original_severity, v.original_reason)
                for v in db.query(ExceptionVerification).filter(ExceptionVerification.expense_report_id == report_id).order_by(ExceptionVerification.id)
            ]
        finally:
            db.close()

    before = _state(report_a)
    before_b = _state(report_b)
    rows_before = verification_rows(report_a)
    verified_expense_id = missing[0]["expense_id"]
    assert before["verifications"] == [(verified_expense_id, "MISSING BILL")]
    assert verified_expense_id in before["expense_ids"].values()
    assert max(before["expense_ids"].values()) < min(before_b["expense_ids"].values()), "report B's rows must sit after A's so ids cannot be reused"

    resp = client.post(f"/api/documents/expense:{report_a}/reprocess")
    assert resp.status_code == 200, resp.text

    after = _state(report_a)
    # the Expense rows were recreated with NEW ids ...
    assert set(after["expense_ids"].values()).isdisjoint(set(before["expense_ids"].values())), "precondition: the ids really changed"
    # ... and the SAME verification row now follows its claim (s_no 2) to the new expense, everything else about it unchanged ...
    rows_after = verification_rows(report_a)
    assert len(rows_after) == 1
    assert rows_after[0][0] == rows_before[0][0], "the verification row itself (its id) is preserved"
    assert rows_after[0][1] == after["expense_ids"][2] != verified_expense_id
    assert rows_after[0][2:] == rows_before[0][2:], "type, verifier, timestamp, note, original severity and reason are untouched"
    # ... so the regenerated MISSING BILL exception for the same claim is RESOLVED, with the auditor's note.
    again = _of_type(_exceptions(client, report_a), "MISSING BILL")
    assert len(again) == 1
    assert again[0]["expense_id"] == after["expense_ids"][2]
    assert again[0]["status"] == "RESOLVED"
    assert again[0]["verification_note"] == "accepted by the auditor"

    # the other report is unaffected; the full route recorded its own audit event (not BILL_REMATCHED) and the carry-over was audited
    assert _state(report_b) == before_b
    assert _audit("DOCUMENT_REPROCESSED", f"expense:{report_a}") == 1
    assert _audit("BILL_REMATCHED", f"expense:{report_a}") == 0
    assert _audit("VERIFICATION_CARRIED_OVER", str(rows_before[0][0])) == 1


# ================================================================ TEST 3: backup -> simulated failure after the deletion commit -> restore


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _integrity_check(path: Path) -> str:
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()


def _assert_throwaway(path: Path, tmp_path: Path) -> None:
    resolved = Path(path).resolve()
    assert tmp_path.resolve() in resolved.parents, "recovery rehearsals must only ever touch files under pytest's tmp_path"
    assert resolved != Path(database.DB_PATH).resolve()
    assert resolved != (Path(database.BACKEND_DIR) / "spinco_ta.db").resolve()
    assert "uat-data-test" not in str(resolved)


def _build_disposable_report_db(tmp_path: Path, name: str = "recovery_source.db"):
    """A throw-away SQLite file holding ONE synthetic report whose bills were extracted by the real pipeline."""
    db_file = tmp_path / name
    _assert_throwaway(db_file, tmp_path)
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    pdf, _page_of = sr.render_er(_lodging_case("ER-73001"))
    pdf_path = tmp_path / "ER-73001.pdf"
    pdf_path.write_bytes(pdf)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        report = ExpenseReport(
            filename="ER-73001.pdf", original_filename="ER-73001.pdf", file_hash="disposable-hash-73001", file_size=len(pdf),
            source_path=str(pdf_path), extraction_status="EXTRACTED", total_claimed=5100.0, er_id="ER-73001",
        )
        session.add(report)
        session.commit()
        session.add(Expense(expense_report_id=report.id, s_no=1, expense_date=date(2026, 7, 24), category="LODGING", claimed_amount=5100.0,
                            bill_match_status="NO_MATCH", trip_match_status="NOT_PROCESSED"))
        session.commit()
        bill_pipeline.extract_and_match_bills(session, report, PyMuPDFTextExtractor(), content_last_page=1)
        report_id = report.id
    finally:
        session.close()
    return engine, db_file, report_id


def _file_db_snapshot(db_file: Path, report_id: int) -> dict:
    engine = create_engine(f"sqlite:///{db_file}")
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        def num(value):
            return None if value is None else float(value)

        bills = session.query(Bill).filter(Bill.expense_report_id == report_id).order_by(Bill.id).all()
        expense = session.query(Expense).filter(Expense.expense_report_id == report_id).one()
        candidates = session.query(BillMatchCandidate).filter(BillMatchCandidate.expense_id == expense.id).order_by(BillMatchCandidate.id).all()
        return {
            "bills": [(b.id, num(b.amount), num(b.catering_amount), b.catering_mentioned, num(b.room_service_food_amount), b.room_service_food_mentioned, b.raw_text) for b in bills],
            "bill_ids": [b.id for b in bills],
            "expense": (expense.id, expense.bill_id, expense.candidate_bill_id, expense.bill_match_status),
            "candidates": [(c.expense_id, c.bill_id, c.rank) for c in candidates],
        }
    finally:
        session.close()
        engine.dispose()


def test_backup_simulated_failure_after_the_deletion_commit_and_restore(tmp_path, monkeypatch):
    engine, live, report_id = _build_disposable_report_db(tmp_path)
    original = _file_db_snapshot(live, report_id)
    assert len(original["bills"]) == 1, "fixture: the real pipeline must have extracted the folio"
    old_bill_id = original["bill_ids"][0]
    assert original["expense"][1] == old_bill_id, "fixture: the claim is matched to the bill"
    assert original["candidates"], "fixture: match candidates exist for the bill"

    # ---- backup: connections disposed FIRST, then a file-level copy outside the working database file
    engine.dispose()
    assert not (tmp_path / "recovery_source.db-journal").exists(), "a hot journal would mean an interrupted transaction: never back up over one"
    backup = tmp_path / "backup" / "recovery_source.db"
    backup.parent.mkdir()
    _assert_throwaway(backup, tmp_path)
    shutil.copy2(live, backup)
    original_hash, original_size = _sha256(live), live.stat().st_size
    assert _sha256(backup) == original_hash and backup.stat().st_size == original_size
    assert _integrity_check(backup) == "ok"

    # ---- simulated failure AFTER the early deletion commit (safe seam: the parser raises when the pipeline re-parses the page)
    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated failure after the deletion commit")

    engine_f = create_engine(f"sqlite:///{live}")
    session = sessionmaker(bind=engine_f, autoflush=False)()
    try:
        report = session.get(ExpenseReport, report_id)
        # a scoped patch: undoing only this one (never the suite's autouse provider / httpx guards)
        with monkeypatch.context() as patch:
            patch.setattr(bill_parser, "parse_bill_text", _boom)
            with pytest.raises(RuntimeError, match="simulated failure"):
                bill_pipeline.extract_and_match_bills(session, report, PyMuPDFTextExtractor(), content_last_page=1)
    finally:
        session.close()
        engine_f.dispose()

    # ---- the failure state: the non-atomic deletion window is real
    failed = _file_db_snapshot(live, report_id)
    assert failed["bills"] == [], "the old bills were deleted and COMMITTED before the failure"
    assert failed["expense"][1] == old_bill_id and old_bill_id not in failed["bill_ids"], "the claim still points at a bill that no longer exists"
    assert failed["candidates"] == [], "its match candidates were removed with the bills"
    assert _sha256(live) != original_hash, "the live file no longer matches the backup"

    # ---- restore from the verified backup (the damaged file is kept aside, never deleted)
    kept = tmp_path / "damaged_after_failure.db"
    shutil.move(str(live), str(kept))
    shutil.copy2(backup, live)
    assert _sha256(live) == original_hash and live.stat().st_size == original_size
    assert _integrity_check(live) == "ok"
    assert kept.exists()

    # ---- restored records and relationships are exactly the original ones
    assert _file_db_snapshot(live, report_id) == original


# ================================================================ TEST 4: the candidate-report predicate on legacy / migrated / partial schemas


def _candidate_reports(engine) -> tuple[str, list[int]]:
    """The documented operator procedure: look at the columns FIRST, query only flag columns that exist."""
    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(bills)"))}
        flags = [c for c in ("catering_mentioned", "room_service_food_mentioned") if c in columns]
        if not flags:
            return "not_migrated", []
        status = "migrated" if len(flags) == 2 else "partial"
        where = " OR ".join(f"{c} IS NOT NULL" for c in flags)
        rows = connection.execute(text(f"SELECT DISTINCT expense_report_id FROM bills WHERE {where} ORDER BY 1"))
        return status, [row[0] for row in rows]


@pytest.fixture
def predicate_db(tmp_path):
    """Four reports: 1 = legacy (both flags NULL), 2 = catering flag only, 3 = room-service flag only (False still counts), 4 = both."""
    db_file = tmp_path / "predicate_source.db"
    _assert_throwaway(db_file, tmp_path)
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        plan = {1: (None, None), 2: (True, None), 3: (None, False), 4: (True, False)}
        for number, (catering, room) in plan.items():
            report = ExpenseReport(filename=f"R{number}.pdf", original_filename=f"R{number}.pdf", file_hash=f"predicate-hash-{number}", file_size=1,
                                   source_path="unused.pdf", extraction_status="EXTRACTED", total_claimed=1.0, er_id=f"ER-7400{number}")
            session.add(report)
            session.commit()
            for page in (1, 2):  # two bills per report: the report must be listed once
                session.add(Bill(expense_report_id=report.id, page_number=page, amount=100.0, catering_mentioned=catering, room_service_food_mentioned=room))
            session.commit()
        ids = {row.er_id: row.id for row in session.query(ExpenseReport)}
    finally:
        session.close()
    engine.dispose()
    return db_file, {number: ids[f"ER-7400{number}"] for number in plan}


def _variant(db_file: Path, tmp_path: Path, name: str, drop: tuple[str, ...]):
    copy = tmp_path / name
    shutil.copy2(db_file, copy)
    _assert_throwaway(copy, tmp_path)
    engine = create_engine(f"sqlite:///{copy}")
    with engine.begin() as connection:
        for column in drop:
            connection.execute(text(f"ALTER TABLE bills DROP COLUMN {column}"))
    return engine


def test_predicate_on_a_migrated_schema_lists_exactly_the_new_format_reports(predicate_db, tmp_path):
    db_file, ids = predicate_db
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        status, reports = _candidate_reports(engine)
    finally:
        engine.dispose()
    assert status == "migrated"
    assert reports == sorted([ids[2], ids[3], ids[4]]), "a legacy NULL-only report (1) is excluded; a stored False still counts as new-format"


def test_predicate_each_flag_column_on_its_own(predicate_db, tmp_path):
    db_file, ids = predicate_db
    only_catering = _variant(db_file, tmp_path, "only_catering.db", ("room_service_food_mentioned", "room_service_food_amount"))
    only_room = _variant(db_file, tmp_path, "only_room.db", ("catering_mentioned",))
    try:
        assert _candidate_reports(only_catering) == ("partial", sorted([ids[2], ids[4]]))
        assert _candidate_reports(only_room) == ("partial", sorted([ids[3], ids[4]]))
    finally:
        only_catering.dispose()
        only_room.dispose()


def test_predicate_on_a_database_without_the_evidence_columns_never_queries_them(predicate_db, tmp_path):
    db_file, _ids = predicate_db
    legacy = _variant(db_file, tmp_path, "legacy_schema.db", NEW_BILL_COLUMNS)
    statements: list[str] = []
    event.listen(legacy, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, executemany: statements.append(statement))
    try:
        assert not (set(NEW_BILL_COLUMNS) & {c["name"] for c in inspect(legacy).get_columns("bills")}), "fixture must be a real legacy schema"
        statements.clear()
        assert _candidate_reports(legacy) == ("not_migrated", [])
    finally:
        legacy.dispose()
    assert statements, "the column check itself must have run"
    assert not any(column in statement for statement in statements for column in NEW_BILL_COLUMNS), "no statement may name a column that does not exist"


def test_predicate_on_a_partial_schema_does_not_reference_the_missing_column(predicate_db, tmp_path):
    db_file, _ids = predicate_db
    partial = _variant(db_file, tmp_path, "partial_schema.db", ("catering_mentioned",))
    statements: list[str] = []
    event.listen(partial, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, executemany: statements.append(statement))
    try:
        _candidate_reports(partial)
    finally:
        partial.dispose()
    assert statements and not any("catering_mentioned" in statement for statement in statements)
