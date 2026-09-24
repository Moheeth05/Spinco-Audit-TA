"""Phase 7 Exception Engine + report-level ACCOUNT VERIFIED / EXCEPTION /
REVIEW REQUIRED status tests.

Sections:
  1-17: synthetic DB-level scenarios (SessionLocal-direct, mirroring
        test_validation.py's own pattern) - one deviation at a time so each
        exception type / report-status rule is exercised in isolation.
  18-21: revalidation/persistence/provenance behavior.
  22-26: real-document reference cases (ER-39832, ER-40223, ER-40706) and a
         Phase 1-6 regression smoke check.
"""

import uuid
from datetime import date
from pathlib import Path

from app.database import SessionLocal
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.trip import TripReport
from app.services.exception_engine import evaluate_report
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
        match_status="MATCHED",
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


def _make_trip(db, **kwargs):
    defaults = dict(
        is_embedded=False,
        trip_id="TRIP-90001",
        employee_name="TEST EMPLOYEE",
        employee_code="T100",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        destination="Indore",
        extraction_status="EXTRACTED",
    )
    defaults.update(kwargs)
    t = TripReport(**defaults)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _evaluate(db, report_id):
    validate_expense_report(db, report_id)
    evaluate_report(db, report_id)
    return db.get(ExpenseReport, report_id)


def _exception_types(db, report_id, expense_id=None):
    from app.models.validation import ExceptionRecord

    q = db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report_id)
    if expense_id is not None:
        q = q.filter(ExceptionRecord.expense_id == expense_id)
    return [r.exception_type for r in q.all()]


# ---------- 1: fully passing report ----------


def test_1_fully_passing_report_is_account_verified(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-CLEAN", total_claimed=2000.0)
        _make_bill(db, report.id, amount=2000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db,
            report.id,
            category="OWN CAR PETROL",
            claimed_amount=2000.0,
            expense_date=date(2026, 6, 1),
            bill_id=bill.id,
            bill_match_status="MATCHED",
        )
        report = _evaluate(db, report.id)
        assert report.overall_status == "ACCOUNT VERIFIED"
        assert report.exception_count == 0
        assert report.review_required_count == 0
        assert report.verified_expense_count == 1
    finally:
        db.close()


# ---------- 2-9: each confirmed exception type in isolation ----------


def test_2_over_limit_is_exception():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-OVERLIMIT", total_claimed=9000.0)
        _make_bill(db, report.id, amount=9000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=9000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        report = _evaluate(db, report.id)
        assert report.overall_status == "EXCEPTION"
        assert "OVER LIMIT" in _exception_types(db, report.id)
    finally:
        db.close()


def test_3_amount_mismatch_is_exception():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-AMTMISMATCH", total_claimed=2000.0)
        _make_bill(db, report.id, amount=1800.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        report = _evaluate(db, report.id)
        assert report.overall_status == "EXCEPTION"
        assert "AMOUNT MISMATCH" in _exception_types(db, report.id)
    finally:
        db.close()


def test_4_date_mismatch_is_exception():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-DATEMISMATCH", total_claimed=2000.0)
        _make_bill(db, report.id, amount=2000.0, bill_date=date(2026, 6, 5), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        report = _evaluate(db, report.id)
        assert report.overall_status == "EXCEPTION"
        assert "DATE MISMATCH" in _exception_types(db, report.id)
    finally:
        db.close()


def test_5_missing_bill_is_exception():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-MISSINGBILL", total_claimed=2000.0)
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_match_status="NO_MATCH",
        )
        report = _evaluate(db, report.id)
        assert report.overall_status == "EXCEPTION"
        assert "MISSING BILL" in _exception_types(db, report.id)
    finally:
        db.close()


def test_6_duplicate_bill_is_exception():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-DUPBILL", total_claimed=4000.0)
        _make_bill(db, report.id, amount=2000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        for s_no in (1, 2):
            _make_expense(
                db, report.id, s_no=s_no, category="OWN CAR PETROL", claimed_amount=2000.0,
                expense_date=date(2026, 6, 1), bill_id=bill.id, bill_match_status="MATCHED",
            )
        report = _evaluate(db, report.id)
        assert report.overall_status == "EXCEPTION"
        assert "DUPLICATE BILL" in _exception_types(db, report.id)
    finally:
        db.close()


def test_7_category_mismatch_is_exception():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-CATMISMATCH", total_claimed=2000.0)
        _make_bill(db, report.id, amount=2000.0, bill_date=date(2026, 6, 1), category_guess="Lodging")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        report = _evaluate(db, report.id)
        assert report.overall_status == "EXCEPTION"
        assert "CATEGORY MISMATCH" in _exception_types(db, report.id)
    finally:
        db.close()


def test_8_total_mismatch_is_exception():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-TOTALMISMATCH", total_claimed=99999.0)
        _make_bill(db, report.id, amount=2000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        report = _evaluate(db, report.id)
        assert report.overall_status == "EXCEPTION"
        assert "TOTAL MISMATCH" in _exception_types(db, report.id)
    finally:
        db.close()


def test_9_catering_included_is_exception():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-CATERING", total_claimed=2250.0)
        _make_bill(
            db, report.id, amount=2250.0, bill_date=date(2026, 6, 1), category_guess="Train",
            raw_text="Train Fare Rs.2000.00\nCatering charges Rs.250.00 included",
        )
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="TRAIN / BUS FARE", claimed_amount=2250.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        report = _evaluate(db, report.id)
        assert report.overall_status == "EXCEPTION"
        assert "CATERING INCLUDED" in _exception_types(db, report.id)
    finally:
        db.close()


# ---------- 10-12: Batta/trip exceptions ----------


def test_10_trip_not_found_is_exception():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-TRIPNOTFOUND", total_claimed=1000.0)
        _make_expense(
            db, report.id, category="BATTA", claimed_amount=1000.0, expense_date=date(2026, 6, 1),
            trip_match_status="NO_MATCH", trip_employee_mismatch=False,
        )
        report = _evaluate(db, report.id)
        assert report.overall_status == "EXCEPTION"
        assert "TRIP NOT FOUND" in _exception_types(db, report.id)
    finally:
        db.close()


def test_11_trip_date_mismatch_is_exception():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-TRIPDATEMISMATCH", total_claimed=1000.0)
        trip = _make_trip(db, start_date=date(2026, 7, 13), end_date=date(2026, 7, 19))
        _make_expense(
            db, report.id, category="BATTA", claimed_amount=1000.0, expense_date=date(2026, 7, 25),
            trip_match_status="MATCHED", trip_report_id=trip.id,
        )
        report = _evaluate(db, report.id)
        assert report.overall_status == "EXCEPTION"
        assert "TRIP DATE MISMATCH" in _exception_types(db, report.id)
    finally:
        db.close()


def test_12_trip_employee_mismatch_is_exception():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-TRIPEMPMISMATCH", total_claimed=1000.0)
        _make_expense(
            db, report.id, category="BATTA", claimed_amount=1000.0, expense_date=date(2026, 6, 1),
            trip_match_status="NO_MATCH", trip_employee_mismatch=True,
        )
        report = _evaluate(db, report.id)
        assert report.overall_status == "EXCEPTION"
        assert "TRIP EMPLOYEE MISMATCH" in _exception_types(db, report.id)
    finally:
        db.close()


# ---------- 13-14: REVIEW_REQUIRED must not silently pass ----------


def test_13_ambiguous_bill_match_is_review_required_not_verified():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-BILLREVIEW", total_claimed=2000.0)
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_match_status="MULTIPLE_CANDIDATES",
        )
        report = _evaluate(db, report.id)
        assert report.overall_status == "REVIEW REQUIRED"
        assert report.exception_count == 0
        assert report.review_required_count == 1
    finally:
        db.close()


def test_14_ambiguous_trip_match_is_review_required_not_verified():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-TRIPREVIEW", total_claimed=1000.0)
        _make_expense(
            db, report.id, category="BATTA", claimed_amount=1000.0, expense_date=date(2026, 6, 1),
            trip_match_status="MULTIPLE_CANDIDATES",
        )
        report = _evaluate(db, report.id)
        assert report.overall_status == "REVIEW REQUIRED"
        assert report.exception_count == 0
        assert "TRIP MATCH REVIEW REQUIRED" in _exception_types(db, report.id)
    finally:
        db.close()


# ---------- 15: confirmed exception takes precedence over review-only ----------


def test_15_confirmed_exception_plus_review_condition_is_exception():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-MIXED", total_claimed=11000.0)
        _make_bill(db, report.id, amount=9000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, s_no=1, category="OWN CAR PETROL", claimed_amount=9000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )  # OVER LIMIT exception
        _make_expense(
            db, report.id, s_no=2, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_match_status="MULTIPLE_CANDIDATES",
        )  # review-only
        report = _evaluate(db, report.id)
        assert report.overall_status == "EXCEPTION"
        assert report.exception_count == 1
        assert report.review_required_count == 1
    finally:
        db.close()


# ---------- 16: multiple independent exceptions all preserved ----------


def test_16_multiple_independent_exceptions_all_preserved():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-MULTIEXC", total_claimed=11000.0)
        _make_bill(db, report.id, amount=9000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        over_limit_bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, s_no=1, category="OWN CAR PETROL", claimed_amount=9000.0, expense_date=date(2026, 6, 1),
            bill_id=over_limit_bill.id, bill_match_status="MATCHED",
        )

        _make_bill(db, report.id, amount=1000.0, bill_date=date(2026, 6, 2), category_guess="Lodging")
        mismatch_bill = db.query(Bill).filter(Bill.expense_report_id == report.id, Bill.amount == 1000.0).first()
        _make_expense(
            db, report.id, s_no=2, category="OWN CAR PETROL", claimed_amount=1000.0, expense_date=date(2026, 6, 2),
            bill_id=mismatch_bill.id, bill_match_status="MATCHED",
        )  # CATEGORY MISMATCH

        _make_expense(
            db, report.id, s_no=3, category="BATTA", claimed_amount=1000.0, expense_date=date(2026, 6, 3),
            trip_match_status="NO_MATCH",
        )  # TRIP NOT FOUND

        report = _evaluate(db, report.id)
        assert report.overall_status == "EXCEPTION"
        types = _exception_types(db, report.id)
        assert "OVER LIMIT" in types
        assert "CATEGORY MISMATCH" in types
        assert "TRIP NOT FOUND" in types
        assert report.exception_count == 3
    finally:
        db.close()


# ---------- 17: duplicate underlying exception is deduplicated ----------


def test_17_missing_bill_does_not_also_produce_bill_review_required():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-DEDUP", total_claimed=2000.0)
        expense = _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_match_status="NO_MATCH",
        )
        report = _evaluate(db, report.id)
        types = _exception_types(db, report.id, expense_id=expense.id)
        assert types.count("MISSING BILL") == 1
        assert "BILL MATCH REVIEW REQUIRED" not in types
    finally:
        db.close()


def test_17b_ambiguous_bill_match_does_not_also_produce_required_document_missing():
    """Post-UAT correctness fix: an ambiguous match (MULTIPLE_CANDIDATES, not
    NO_MATCH - a document is plausibly present, matching is just uncertain)
    used to produce both "BILL MATCH REVIEW REQUIRED" (from the Bill Amount/
    Date/Category/Duplicate checks) and "REQUIRED DOCUMENT MISSING" (from
    Required Document Check's own generic REVIEW_REQUIRED branch) as two
    separate ExceptionRecord rows describing the identical underlying
    uncertainty - and "REQUIRED DOCUMENT MISSING" wrongly implied the
    document was physically absent when it was actually just an uncertain
    match. Both checks now collapse into the one accurately-named family."""
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-AMBIGDEDUP", total_claimed=2000.0)
        expense = _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_match_status="MULTIPLE_CANDIDATES",
        )
        report = _evaluate(db, report.id)
        types = _exception_types(db, report.id, expense_id=expense.id)
        assert types.count("BILL MATCH REVIEW REQUIRED") == 1
        assert "REQUIRED DOCUMENT MISSING" not in types
        assert report.review_required_count == 1
    finally:
        db.close()


# ---------- 18-19: revalidation / late Trip Report resolution ----------


def test_18_19_late_trip_report_resolves_trip_not_found_and_revalidation_is_idempotent(client):
    er_path = REPO_ROOT / "ER-40706 (1).pdf"
    resp = client.post(
        "/api/uploads/expense-reports", files=[("files", (er_path.name, er_path.read_bytes(), "application/pdf"))]
    )
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]

    exceptions_before = client.get(f"/api/expense-reports/{report_id}/exceptions").json()
    types_before = [e["exception_type"] for e in exceptions_before]
    assert "TRIP NOT FOUND" in types_before
    count_before = len(exceptions_before)

    trip_path = REPO_ROOT / "TRIP-25845_Chandigarh_tour30.06.2026_to_12.07.2026.pdf"
    client.post(
        "/api/uploads/trip-reports", files=[("files", (trip_path.name, trip_path.read_bytes(), "application/pdf"))]
    )

    exceptions_after = client.get(f"/api/expense-reports/{report_id}/exceptions").json()
    types_after = [e["exception_type"] for e in exceptions_after]
    assert "TRIP NOT FOUND" not in types_after

    # Revalidating again must not duplicate whatever exceptions remain.
    client.post(f"/api/expense-reports/{report_id}/revalidate")
    exceptions_final = client.get(f"/api/expense-reports/{report_id}/exceptions").json()
    assert len(exceptions_final) == len(exceptions_after)


# ---------- 20: persistence (DB-level) ----------


def test_20_report_status_and_exceptions_persist_across_a_fresh_session(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-PERSIST", total_claimed=9000.0)
        _make_bill(db, report.id, amount=9000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=9000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        report_id = report.id
        _evaluate(db, report_id)
    finally:
        db.close()

    # Fresh session/connection - proxy for "the data was really committed".
    db2 = SessionLocal()
    try:
        reloaded = db2.get(ExpenseReport, report_id)
        assert reloaded.overall_status == "EXCEPTION"
        assert reloaded.exception_count == 1
        from app.models.validation import ExceptionRecord

        rows = db2.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report_id).all()
        assert any(r.exception_type == "OVER LIMIT" for r in rows)
    finally:
        db2.close()


# ---------- 21: source-page provenance ----------


def test_21_exception_source_page_traces_to_the_matched_bill():
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-SOURCEPAGE", total_claimed=2000.0)
        _make_bill(db, report.id, page_number=7, amount=1800.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        report = _evaluate(db, report.id)

        from app.models.validation import ExceptionRecord

        rec = (
            db.query(ExceptionRecord)
            .filter(ExceptionRecord.expense_report_id == report.id, ExceptionRecord.exception_type == "AMOUNT MISMATCH")
            .first()
        )
        assert rec is not None
        assert rec.source_page == 7
        assert rec.bill_id == bill.id
    finally:
        db.close()


# ---------- 22-23: mandatory ER-39832 reference case ----------


def test_22_23_er39832_reference_case(client, sample_pdf_path):
    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (sample_pdf_path.name, sample_pdf_path.read_bytes(), "application/pdf"))],
    )
    result = resp.json()["results"][0]
    report_id = result["document_id"].split(":")[1]

    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    first = next(e for e in expenses if e["s_no"] == 1)
    assert first["claimed_amount"] == 2000.0
    assert first["claimed_amount"] != 2500.0

    status = client.get(f"/api/expense-reports/{report_id}/status").json()
    # Genuine confirmed exceptions exist elsewhere on this real document
    # (MISSING BILL / DUPLICATE BILL / OVER LIMIT) - the report must not be
    # ACCOUNT VERIFIED, and the reference expense's own bill-tie ambiguity
    # must still be visibly present as a review-required condition, not
    # silently dropped or forced into a false PASS.
    assert status["overall_status"] != "ACCOUNT VERIFIED"

    exceptions = client.get(f"/api/expense-reports/{report_id}/exceptions").json()
    ref_expense_items = [e for e in exceptions if e["s_no"] == 1]
    assert any(e["exception_type"] == "BILL MATCH REVIEW REQUIRED" and e["severity"] == "REVIEW REQUIRED" for e in ref_expense_items)
    assert not any(e["severity"] == "EXCEPTION" for e in ref_expense_items)


# ---------- 24: ER-40223 embedded trip/Batta behavior ----------


def test_24_er40223_embedded_batta_matches_do_not_produce_trip_exceptions(client):
    er_path = REPO_ROOT / "ER-40223.pdf"
    resp = client.post(
        "/api/uploads/expense-reports", files=[("files", (er_path.name, er_path.read_bytes(), "application/pdf"))]
    )
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]

    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    battas = [e for e in expenses if (e["category"] or "").upper().startswith("BATTA")]
    assert battas
    assert all(e["trip_match_status"] == "MATCHED" for e in battas)

    exceptions = client.get(f"/api/expense-reports/{report_id}/exceptions").json()
    batta_s_nos = {e["s_no"] for e in battas}
    trip_exceptions_on_batta_lines = [
        e for e in exceptions if e["s_no"] in batta_s_nos and e["exception_type"] in ("TRIP NOT FOUND", "TRIP DATE MISMATCH", "TRIP EMPLOYEE MISMATCH", "TRIP MATCH REVIEW REQUIRED")
    ]
    assert trip_exceptions_on_batta_lines == []


# ---------- 25: ER-40706 late-trip resolution (report-level view) ----------


def test_25_er40706_report_status_reflects_late_trip_resolution(client):
    er_path = REPO_ROOT / "ER-40706 (1).pdf"
    resp = client.post(
        "/api/uploads/expense-reports", files=[("files", (er_path.name, er_path.read_bytes(), "application/pdf"))]
    )
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]

    status_before = client.get(f"/api/expense-reports/{report_id}/status").json()
    exceptions_before = client.get(f"/api/expense-reports/{report_id}/exceptions").json()
    assert any(e["exception_type"] == "TRIP NOT FOUND" for e in exceptions_before)

    trip_path = REPO_ROOT / "TRIP-25845_Chandigarh_tour30.06.2026_to_12.07.2026.pdf"
    client.post(
        "/api/uploads/trip-reports", files=[("files", (trip_path.name, trip_path.read_bytes(), "application/pdf"))]
    )

    status_after = client.get(f"/api/expense-reports/{report_id}/status").json()
    exceptions_after = client.get(f"/api/expense-reports/{report_id}/exceptions").json()
    assert not any(e["exception_type"] == "TRIP NOT FOUND" for e in exceptions_after)
    # Resolving 10 TRIP NOT FOUND exceptions must reduce the exception count.
    assert status_after["exception_count"] < status_before["exception_count"]


# ---------- 26: Phase 1-6 regression smoke ----------


def test_26_phase1_6_regression_still_works(client):
    assert len(client.get("/api/policy-master").json()) == 29

    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", ("ER-39832.pdf", (REPO_ROOT / "ER-39832.pdf").read_bytes(), "application/pdf"))],
    )
    result = resp.json()["results"][0]
    assert result["success"]
    assert result["extraction_status"] == "EXTRACTED"
    report_id = result["document_id"].split(":")[1]

    bills = client.get(f"/api/expense-reports/{report_id}/bills").json()
    assert len(bills) > 0

    validations = client.get(f"/api/expense-reports/{report_id}/validations").json()
    assert len(validations) > 0

    trip_resp = client.post(
        "/api/uploads/trip-reports",
        files=[
            (
                "files",
                (
                    "TRIP-24935_Trip_To_Indore_Pithampur.pdf",
                    (REPO_ROOT / "TRIP-24935_Trip_To_Indore_Pithampur.pdf").read_bytes(),
                    "application/pdf",
                ),
            )
        ],
    )
    trip_result = trip_resp.json()["results"][0]
    assert trip_result["success"]
    trip_report_id = trip_result["document_id"].split(":")[1]
    detail = client.get(f"/api/trip-reports/{trip_report_id}/details").json()
    assert detail["employee_name"] == "PRANAY RAJESH PAWAR"
    assert detail["employee_code"] == "T465"
    assert detail["start_date"] == "2026-07-13"
    assert detail["end_date"] == "2026-07-19"
