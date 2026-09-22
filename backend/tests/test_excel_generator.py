"""Phase 8 consolidated Excel workbook tests.

Sections:
  1-8: synthetic DB-level scenarios (SessionLocal-direct, mirroring
       test_exception_engine.py's own pattern) - structural correctness,
       duplicate prevention, regeneration stability, summary consistency.
  9-13: real-document reference cases (ER-39832, ER-40223, ER-40706,
        TRIP-24935) via the `client` fixture, exercising the full pipeline
        end to end including the Phase 8 auto-regeneration hook.
"""

import uuid
from datetime import date, datetime
from pathlib import Path

import openpyxl

from app.database import SessionLocal
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.trip import TripReport
from app.services.excel_generator import (
    SHEET_BATTA_TRIP,
    SHEET_EXCEPTIONS,
    SHEET_EXPENSE_REPORTS,
    SHEET_EXPENSES,
    SHEET_NAMES,
    SHEET_SUMMARY,
    SHEET_TRIP_REPORTS,
    WORKBOOK_FILENAME,
    generate_workbook,
)
from app.services.exception_engine import evaluate_report
from app.services.storage import EXCEL_DIR
from app.services.validation.pipeline import validate_expense_report

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _make_report(db, **kwargs):
    defaults = dict(
        filename="test.pdf",
        original_filename="test.pdf",
        file_hash=f"hash-{uuid.uuid4()}",
        file_size=100,
        source_path="unused.pdf",
        extraction_status="EXTRACTED",
        total_claimed=0.0,
    )
    defaults.update(kwargs)
    row = ExpenseReport(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _make_bill(db, report_id, **kwargs):
    defaults = dict(
        expense_report_id=report_id,
        page_number=5,
        amount=2000.0,
        bill_date=date(2026, 6, 1),
        category_guess="Fuel",
        extraction_status="EXTRACTED",
    )
    defaults.update(kwargs)
    b = Bill(**defaults)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def _make_expense(db, report_id, **kwargs):
    defaults = dict(
        expense_report_id=report_id,
        s_no=1,
        expense_date=date(2026, 6, 1),
        category="OWN CAR PETROL",
        claimed_amount=2000.0,
        bill_match_status="NO_MATCH",
        trip_match_status="NOT_PROCESSED",
    )
    defaults.update(kwargs)
    e = Expense(**defaults)
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


def _evaluate(db, report_id):
    validate_expense_report(db, report_id)
    evaluate_report(db, report_id)


def _sheet_rows(ws):
    """All non-header rows as tuples, skipping fully-blank rows."""
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    return [r for r in rows if any(v is not None for v in r)]


def _headers(ws):
    return list(next(ws.iter_rows(min_row=1, max_row=1, values_only=True)))


# ---------- 1: workbook creation, opens, sheet names, headers ----------


def test_1_workbook_creation_and_structure(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-XLSX1", total_claimed=2000.0)
        _make_bill(db, report.id, amount=2000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        _evaluate(db, report.id)
        record = generate_workbook(db)
    finally:
        db.close()

    assert record.filename == WORKBOOK_FILENAME
    path = Path(record.file_path)
    assert path.exists()

    wb = openpyxl.load_workbook(path)
    try:
        assert set(wb.sheetnames) == set(SHEET_NAMES)
        er_headers = _headers(wb[SHEET_EXPENSE_REPORTS])
        assert er_headers[0] == "Report ID"
        assert "Overall Status" in er_headers
        exp_headers = _headers(wb[SHEET_EXPENSES])
        assert exp_headers[0] == "Expense ID"
        assert "Claimed Amount" in exp_headers
        exc_headers = _headers(wb[SHEET_EXCEPTIONS])
        assert exc_headers[0] == "Exception ID"
        assert "Reason" in exc_headers
    finally:
        wb.close()


# ---------- 2: stable row identity ----------


def test_2_stable_row_identity_uses_database_ids():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-XLSX2", total_claimed=4000.0)
        e1 = _make_expense(db, report.id, s_no=1, claimed_amount=2000.0)
        e2 = _make_expense(db, report.id, s_no=2, claimed_amount=2000.0)
        report_id, e1_id, e2_id = report.id, e1.id, e2.id
        _evaluate(db, report.id)
        generate_workbook(db)
    finally:
        db.close()

    wb = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
    try:
        rows = _sheet_rows(wb[SHEET_EXPENSES])
        ids = {r[0] for r in rows if r[1] == report_id}
        assert ids == {e1_id, e2_id}
    finally:
        wb.close()


# ---------- 3-4: regeneration is deterministic, never duplicates ----------


def test_3_regenerating_twice_produces_identical_row_counts():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-XLSX3", total_claimed=2000.0)
        _make_expense(db, report.id, claimed_amount=2000.0)
        _evaluate(db, report.id)
        generate_workbook(db)
        wb1 = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
        counts1 = {name: len(_sheet_rows(wb1[name])) for name in SHEET_NAMES}
        wb1.close()

        generate_workbook(db)
        wb2 = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
        counts2 = {name: len(_sheet_rows(wb2[name])) for name in SHEET_NAMES}
        wb2.close()
    finally:
        db.close()

    assert counts1 == counts2


def test_4_revalidation_then_regeneration_does_not_duplicate_rows():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-XLSX4", total_claimed=9000.0)
        _make_bill(db, report.id, amount=9000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=9000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        report_id = report.id
        _evaluate(db, report.id)
        generate_workbook(db)
        _evaluate(db, report.id)  # revalidate
        _evaluate(db, report.id)  # revalidate again
        generate_workbook(db)
    finally:
        db.close()

    wb = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
    try:
        exc_rows = [r for r in _sheet_rows(wb[SHEET_EXCEPTIONS]) if r[1] == report_id]
        exception_types = [r[7] for r in exc_rows]
        assert exception_types.count("OVER LIMIT") == 1
    finally:
        wb.close()


# ---------- 5: new upload adds rows, existing rows remain ----------


def test_5_new_document_adds_rows_without_touching_existing_ones():
    db = SessionLocal()
    try:
        report1 = _make_report(db, er_id="ER-XLSX5A", total_claimed=1000.0)
        _make_expense(db, report1.id, claimed_amount=1000.0)
        _evaluate(db, report1.id)
        generate_workbook(db)

        wb1 = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
        er_count_before = len(_sheet_rows(wb1[SHEET_EXPENSE_REPORTS]))
        report1_ids_before = {r[0] for r in _sheet_rows(wb1[SHEET_EXPENSE_REPORTS])}
        wb1.close()

        report2 = _make_report(db, er_id="ER-XLSX5B", total_claimed=2000.0)
        _make_expense(db, report2.id, claimed_amount=2000.0)
        _evaluate(db, report2.id)
        report2_id = report2.id
        generate_workbook(db)

        wb2 = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
        er_rows_after = _sheet_rows(wb2[SHEET_EXPENSE_REPORTS])
        wb2.close()
    finally:
        db.close()

    assert len(er_rows_after) == er_count_before + 1
    ids_after = {r[0] for r in er_rows_after}
    assert report1_ids_before <= ids_after
    assert report2_id in ids_after


# ---------- 6: summary sheet consistency ----------


def test_6_summary_counts_match_underlying_sheets():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-XLSX6", total_claimed=11000.0)
        _make_bill(db, report.id, amount=9000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        over_limit_bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, s_no=1, category="OWN CAR PETROL", claimed_amount=9000.0, expense_date=date(2026, 6, 1),
            bill_id=over_limit_bill.id, bill_match_status="MATCHED",
        )
        _make_expense(
            db, report.id, s_no=2, category="BATTA", claimed_amount=1000.0, expense_date=date(2026, 6, 1),
            trip_match_status="NO_MATCH",
        )
        _make_expense(
            db, report.id, s_no=3, category="COURIER CHARGES", claimed_amount=1000.0, expense_date=date(2026, 6, 1),
            bill_match_status="MULTIPLE_CANDIDATES",
        )
        _evaluate(db, report.id)
        generate_workbook(db)
    finally:
        db.close()

    wb = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
    try:
        summary_rows = {r[0]: r[1] for r in _sheet_rows(wb[SHEET_SUMMARY])}
        er_count = len(_sheet_rows(wb[SHEET_EXPENSE_REPORTS]))
        exc_count = len(_sheet_rows(wb[SHEET_EXCEPTIONS]))
        assert summary_rows["Total Expense Reports"] == er_count
        assert summary_rows["Total Exceptions"] == exc_count
        assert summary_rows["Total Expenses"] == len(_sheet_rows(wb[SHEET_EXPENSES]))
    finally:
        wb.close()


# ---------- 7: currency numeric, dates valid, blanks honest ----------


def test_7_currency_is_numeric_dates_are_dates_missing_values_are_blank():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-XLSX7", total_claimed=2000.0)
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_match_status="NO_MATCH",
        )
        report_id = report.id
        _evaluate(db, report.id)
        generate_workbook(db)
    finally:
        db.close()

    wb = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
    try:
        rows = [r for r in _sheet_rows(wb[SHEET_EXPENSES]) if r[1] == report_id]
        assert len(rows) == 1
        row = rows[0]
        claimed_amount = row[7]  # "Claimed Amount" column
        assert claimed_amount == 2000
        assert isinstance(claimed_amount, (int, float))
        assert not isinstance(claimed_amount, str)

        expense_date_cell = row[4]  # "Expense Date"
        assert isinstance(expense_date_cell, (date, datetime))

        # No bill was matched - Bill Vendor/Number/GSTIN must be blank, not "N/A" or invented.
        bill_vendor = row[14]
        assert bill_vendor is None
    finally:
        wb.close()


# ---------- 8: report statuses / exception counts match the database exactly ----------


def test_8_report_status_and_exception_counts_match_database():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-XLSX8", total_claimed=9000.0)
        _make_bill(db, report.id, amount=9000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=9000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        _evaluate(db, report.id)
        generate_workbook(db)
        db.refresh(report)
        db_status = report.overall_status
        db_exception_count = report.exception_count
    finally:
        db.close()

    wb = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
    try:
        er_rows = [r for r in _sheet_rows(wb[SHEET_EXPENSE_REPORTS]) if r[0] == report.id]
        assert len(er_rows) == 1
        assert er_rows[0][16] == db_status  # "Overall Status" column
        assert er_rows[0][14] == db_exception_count  # "Exception Count" column
        assert db_status == "EXCEPTION"
    finally:
        wb.close()


# ---------- 9: mandatory ER-39832 reference case (real PDF) ----------


def test_9_er39832_reference_case_in_workbook(client, sample_pdf_path):
    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (sample_pdf_path.name, sample_pdf_path.read_bytes(), "application/pdf"))],
    )
    result = resp.json()["results"][0]
    report_id = int(result["document_id"].split(":")[1])

    # Extraction pipeline auto-regenerates the workbook - confirm via a fresh disk read.
    wb = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
    try:
        exp_rows = [r for r in _sheet_rows(wb[SHEET_EXPENSES]) if r[1] == report_id]
        first = next(r for r in exp_rows if r[5] == "OWN CAR PETROL" and r[7] == 2000)
        assert first[7] == 2000
        assert first[7] != 2500
        assert isinstance(first[7], (int, float))

        exc_rows = [r for r in _sheet_rows(wb[SHEET_EXCEPTIONS]) if r[1] == report_id]
        assert len(exc_rows) > 0
        # The genuine bill-tie review condition must be present, with its real reason text.
        bill_review = [r for r in exc_rows if r[7] == "BILL MATCH REVIEW REQUIRED"]
        assert bill_review
        assert "no confirmed matching bill" in bill_review[0][10].lower()

        er_rows = [r for r in _sheet_rows(wb[SHEET_EXPENSE_REPORTS]) if r[0] == report_id]
        assert er_rows[0][16] != "ACCOUNT VERIFIED"
        assert er_rows[0][18] == "ER-39832.pdf"  # Source PDF column
    finally:
        wb.close()


# ---------- 10: ER-40223 embedded trip + Batta evidence preserved ----------


def test_10_er40223_embedded_trip_and_batta_evidence_in_workbook(client):
    er_path = REPO_ROOT / "ER-40223.pdf"
    resp = client.post(
        "/api/uploads/expense-reports", files=[("files", (er_path.name, er_path.read_bytes(), "application/pdf"))]
    )
    report_id = int(resp.json()["results"][0]["document_id"].split(":")[1])

    wb = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
    try:
        trip_rows = _sheet_rows(wb[SHEET_TRIP_REPORTS])
        embedded = [r for r in trip_rows if r[1] == "TRIP-25167" and r[2] == "Embedded in Expense Report"]
        assert embedded, "Embedded TRIP-25167 must appear in the Trip Reports sheet with its real source type."
        assert embedded[0][16] == "ER-40223.pdf"  # Source PDF resolves to the parent ER, not a fake standalone doc

        batta_rows = [r for r in _sheet_rows(wb[SHEET_BATTA_TRIP]) if r[1] == report_id]
        assert len(batta_rows) == 4
        assert all(r[6] == "MATCHED" for r in batta_rows)  # Trip Match Status column
        assert all(r[14] for r in batta_rows)  # Matching Evidence column is non-empty
    finally:
        wb.close()


# ---------- 11: ER-40706 late-trip resolution ----------


def test_11_er40706_late_trip_resolution_updates_workbook(client):
    er_path = REPO_ROOT / "ER-40706 (1).pdf"
    resp = client.post(
        "/api/uploads/expense-reports", files=[("files", (er_path.name, er_path.read_bytes(), "application/pdf"))]
    )
    report_id = int(resp.json()["results"][0]["document_id"].split(":")[1])

    wb_before = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
    try:
        exc_before = [r for r in _sheet_rows(wb_before[SHEET_EXCEPTIONS]) if r[1] == report_id]
        trip_not_found_before = [r for r in exc_before if r[7] == "TRIP NOT FOUND"]
        assert trip_not_found_before
        batta_before = [r for r in _sheet_rows(wb_before[SHEET_BATTA_TRIP]) if r[1] == report_id]
        assert all(r[6] == "NO_MATCH" for r in batta_before)
    finally:
        wb_before.close()

    trip_path = REPO_ROOT / "TRIP-25845_Chandigarh_tour30.06.2026_to_12.07.2026.pdf"
    client.post(
        "/api/uploads/trip-reports", files=[("files", (trip_path.name, trip_path.read_bytes(), "application/pdf"))]
    )

    wb_after = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
    try:
        exc_after = [r for r in _sheet_rows(wb_after[SHEET_EXCEPTIONS]) if r[1] == report_id]
        trip_not_found_after = [r for r in exc_after if r[7] == "TRIP NOT FOUND"]
        assert trip_not_found_after == [], "No stale TRIP NOT FOUND rows may remain after the trip resolves."

        batta_after = [r for r in _sheet_rows(wb_after[SHEET_BATTA_TRIP]) if r[1] == report_id]
        assert all(r[6] == "MATCHED" for r in batta_after)

        # No duplicate expense rows were created by the re-extraction/regeneration.
        exp_after = [r for r in _sheet_rows(wb_after[SHEET_EXPENSES]) if r[1] == report_id]
        expense_ids = [r[0] for r in exp_after]
        assert len(expense_ids) == len(set(expense_ids))
    finally:
        wb_after.close()


# ---------- 12: TRIP-24935 standalone trip preserved ----------


def test_12_trip24935_preserved_in_workbook(client, sample_trip_pdf_path):
    resp = client.post(
        "/api/uploads/trip-reports",
        files=[("files", (sample_trip_pdf_path.name, sample_trip_pdf_path.read_bytes(), "application/pdf"))],
    )
    assert resp.json()["results"][0]["success"]

    wb = openpyxl.load_workbook(EXCEL_DIR / WORKBOOK_FILENAME)
    try:
        rows = [r for r in _sheet_rows(wb[SHEET_TRIP_REPORTS]) if r[1] == "TRIP-24935"]
        assert len(rows) == 1
        row = rows[0]
        assert row[3] == "PRANAY RAJESH PAWAR"  # Employee Name
        assert row[4] == "T465"  # Employee ID
        # openpyxl round-trips any date-formatted cell as datetime.datetime
        # (Excel itself has no separate "date-only" storage type) - compare
        # via .date() rather than expecting a bare datetime.date back.
        assert row[11].date() == date(2026, 7, 13)  # Trip Start Date
        assert row[12].date() == date(2026, 7, 19)  # Trip End Date
    finally:
        wb.close()


# ---------- 13: workbook persists to disk independently of any in-memory state ----------


def test_13_workbook_file_persists_on_disk(client, sample_pdf_path):
    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (sample_pdf_path.name, sample_pdf_path.read_bytes(), "application/pdf"))],
    )
    assert resp.json()["results"][0]["success"]

    path = EXCEL_DIR / WORKBOOK_FILENAME
    assert path.exists()
    assert path.stat().st_size > 0

    # A completely independent load (new Workbook object, no shared state with
    # whatever generated it) proves the file itself is the persisted artifact,
    # not something only valid within the generating process's memory.
    wb = openpyxl.load_workbook(path)
    try:
        assert set(wb.sheetnames) == set(SHEET_NAMES)
    finally:
        wb.close()
