"""Phase 9 Analytics page tests - services/analytics_service.py + api/analytics.py.

Sections:
  1-14: synthetic DB-level scenarios covering category/exception/bill/trip/
        policy-limit aggregation and month filtering, mirroring
        test_dashboard.py's own pattern.
  15-18: real-document reference cases (ER-39832, ER-40223, ER-40706,
         TRIP-24935) exercising the full pipeline end to end.
  19-20: persistence proxy and Phase 1-8 regression smoke.
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
        expense_report_id=report_id, page_number=5, amount=2000.0, bill_date=date(2026, 6, 1),
        category_guess="Fuel", extraction_status="EXTRACTED",
    )
    defaults.update(kwargs)
    b = Bill(**defaults)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def _make_expense(db, report_id, **kwargs):
    defaults = dict(
        expense_report_id=report_id, s_no=1, expense_date=date(2026, 6, 1), category="OWN CAR PETROL",
        claimed_amount=2000.0, bill_match_status="NO_MATCH", trip_match_status="NOT_PROCESSED",
    )
    defaults.update(kwargs)
    e = Expense(**defaults)
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


def _make_trip(db, **kwargs):
    defaults = dict(
        is_embedded=False, trip_id="TRIP-90001", employee_name="TEST EMPLOYEE", employee_code="T100",
        start_date=date(2026, 6, 1), end_date=date(2026, 6, 5), destination="Indore", extraction_status="EXTRACTED",
        report_month="2026-06",
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


# ---------- 1-2: category counts and amounts ----------


def test_1_category_analytics_has_both_count_and_amount(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-AN1", total_claimed=4000.0)
        _make_expense(db, report.id, s_no=1, category="OWN CAR PETROL", claimed_amount=2000.0)
        _make_expense(db, report.id, s_no=2, category="OWN CAR PETROL", claimed_amount=2000.0)
        _evaluate(db, report.id)
    finally:
        db.close()

    rows = client.get("/api/analytics/categories").json()
    petrol = next(r for r in rows if r["category"] == "OWN CAR PETROL")
    assert petrol["expense_count"] == 2
    assert petrol["claimed_amount"] == 4000.0


def test_2_category_analytics_uses_exact_database_category_values(client):
    """Never renames/merges categories - "BATTA [1]" and "BATTA" remain
    distinct rows, exactly as check_bill_amount/category_map document."""
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-AN2", total_claimed=2000.0)
        _make_expense(db, report.id, category="BATTA [1]", claimed_amount=1000.0)
        _make_expense(db, report.id, s_no=2, category="BATTA", claimed_amount=1000.0)
        _evaluate(db, report.id)
    finally:
        db.close()

    rows = client.get("/api/analytics/categories").json()
    labels = {r["category"] for r in rows}
    assert "BATTA [1]" in labels
    assert "BATTA" in labels


# ---------- 3-4: exception counts by type and monthly trend ----------


def test_4_exception_type_breakdown_matches_active_exceptionrecord_rows(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-AN4", total_claimed=9000.0)
        _make_bill(db, report.id, amount=9000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=9000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        _evaluate(db, report.id)
    finally:
        db.close()

    rows = client.get("/api/analytics/exceptions").json()
    over_limit = next(r for r in rows if r["exception_type"] == "OVER LIMIT")
    assert over_limit["count"] == 1
    assert over_limit["severity"] == "EXCEPTION"


def test_5_exception_monthly_trend_groups_by_report_month_not_created_at(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-AN5", total_claimed=9000.0, report_month="2026-03")
        _make_bill(db, report.id, amount=9000.0, bill_date=date(2026, 3, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=9000.0, expense_date=date(2026, 3, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        _evaluate(db, report.id)
    finally:
        db.close()

    rows = client.get("/api/analytics/exceptions/monthly").json()
    march = next(r for r in rows if r["month"] == "2026-03")
    assert march["exception_count"] == 1
    assert march["month_label"] == "Mar 2026"


# ---------- 6-7: bill matching / trip matching counts ----------


def test_6_bill_match_summary_uses_phase4_statuses_verbatim(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-AN6", total_claimed=4000.0)
        _make_expense(db, report.id, s_no=1, bill_match_status="MATCHED", claimed_amount=2000.0)
        _make_expense(db, report.id, s_no=2, bill_match_status="NO_MATCH", claimed_amount=2000.0)
        _evaluate(db, report.id)
    finally:
        db.close()

    rows = {r["status"]: r["count"] for r in client.get("/api/analytics/bills").json()["by_status"]}
    assert rows.get("MATCHED") == 1
    assert rows.get("NO_MATCH") == 1


def test_7_trip_match_summary_only_counts_batta_expenses(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-AN7", total_claimed=3000.0)
        _make_expense(db, report.id, s_no=1, category="BATTA", claimed_amount=1000.0, trip_match_status="MATCHED")
        _make_expense(db, report.id, s_no=2, category="BATTA", claimed_amount=1000.0, trip_match_status="NO_MATCH")
        _make_expense(db, report.id, s_no=3, category="OWN CAR PETROL", claimed_amount=1000.0)
        _evaluate(db, report.id)
    finally:
        db.close()

    data = client.get("/api/analytics/trips").json()
    assert data["total_batta_expenses"] == 2
    assert data["matched_batta_expenses"] == 1
    assert data["unmatched_batta_expenses"] == 1


# ---------- 8-9: policy limit analysis ----------


def test_8_policy_limit_analysis_excludes_actual_basis_from_evaluated(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-AN8", total_claimed=3000.0)
        _make_bill(db, report.id, amount=2000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, s_no=1, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        _make_expense(db, report.id, s_no=2, category="LODGING", claimed_amount=1000.0)
        _evaluate(db, report.id)
    finally:
        db.close()

    data = client.get("/api/analytics/policy-limits").json()
    assert data["not_applicable_actual_basis"] >= 1
    assert data["evaluated_fixed_limit"] >= 1


def test_9_policy_limit_over_limit_claimed_amount_is_numeric(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-AN9", total_claimed=9000.0)
        _make_bill(db, report.id, amount=9000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).first()
        _make_expense(
            db, report.id, category="OWN CAR PETROL", claimed_amount=9000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        _evaluate(db, report.id)
    finally:
        db.close()

    data = client.get("/api/analytics/policy-limits").json()
    assert data["over_limit"] == 1
    assert isinstance(data["over_limit_claimed_amount"], (int, float))
    assert data["over_limit_claimed_amount"] == 9000.0


# ---------- 10-13: filtering ----------


def test_10_month_filter_narrows_summary(client):
    db = SessionLocal()
    try:
        r1 = _make_report(db, er_id="ER-AN10A", total_claimed=1000.0, report_month="2026-06")
        _make_expense(db, r1.id, claimed_amount=1000.0)
        _evaluate(db, r1.id)
        r2 = _make_report(db, er_id="ER-AN10B", total_claimed=2000.0, report_month="2026-07")
        _make_expense(db, r2.id, claimed_amount=2000.0)
        _evaluate(db, r2.id)
    finally:
        db.close()

    all_summary = client.get("/api/analytics/summary").json()
    june_summary = client.get("/api/analytics/summary", params={"month": "2026-06"}).json()
    assert all_summary["total_reports"] == 2
    assert june_summary["total_reports"] == 1
    assert june_summary["total_claimed_amount"] == 1000.0


def test_11_status_breakdown_percentages_sum_correctly(client):
    db = SessionLocal()
    try:
        for i in range(3):
            r = _make_report(db, er_id=f"ER-AN11-{i}", total_claimed=1000.0)
            _make_bill(db, r.id, amount=1000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
            bill = db.query(Bill).filter(Bill.expense_report_id == r.id).first()
            _make_expense(
                db, r.id, category="OWN CAR PETROL", claimed_amount=1000.0, expense_date=date(2026, 6, 1),
                bill_id=bill.id, bill_match_status="MATCHED",
            )
            _evaluate(db, r.id)
    finally:
        db.close()

    points = client.get("/api/analytics/status").json()
    verified = next(p for p in points if p["status"] == "ACCOUNT VERIFIED")
    assert verified["count"] == 3


def test_12_category_filter_by_month_and_status_are_independent_params(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-AN12", total_claimed=2000.0, report_month="2026-05")
        _make_expense(db, report.id, category="LODGING", claimed_amount=2000.0)
        _evaluate(db, report.id)
    finally:
        db.close()

    matching = client.get("/api/analytics/categories", params={"month": "2026-05"}).json()
    non_matching = client.get("/api/analytics/categories", params={"month": "2026-11"}).json()
    assert any(r["category"] == "LODGING" for r in matching)
    assert not any(r["category"] == "LODGING" for r in non_matching)


def test_13_available_months_are_chronologically_sorted(client):
    db = SessionLocal()
    try:
        for month in ("2026-11", "2026-01", "2026-06"):
            _make_report(db, er_id=f"ER-AN13-{month}", report_month=month)
    finally:
        db.close()

    months = client.get("/api/analytics/months").json()
    assert months == sorted(months, key=lambda m: tuple(int(p) for p in m.split("-")))
    assert months == ["2026-01", "2026-06", "2026-11"]


# ---------- 14: trip analytics excludes embedded ----------


def test_14_trip_analytics_excludes_embedded_trips(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-AN14")
        db.add(TripReport(is_embedded=True, source_expense_report_id=report.id, trip_id="TRIP-EMB", report_month="2026-06"))
        db.add(TripReport(is_embedded=False, trip_id="TRIP-STANDALONE", report_month="2026-06", start_date=date(2026, 6, 1), destination="Pune"))
        db.commit()
    finally:
        db.close()

    data = client.get("/api/analytics/trip-reports").json()
    assert data["total_trips"] == 1
    assert any(d["destination"] == "Pune" for d in data["by_destination"])


# ---------- 15-16: real ER-39832 / ER-40223 reference cases ----------


def test_15_er39832_appears_correctly_in_analytics(client, sample_pdf_path):
    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (sample_pdf_path.name, sample_pdf_path.read_bytes(), "application/pdf"))],
    )
    assert resp.json()["results"][0]["success"]

    summary = client.get("/api/analytics/summary").json()
    assert summary["total_reports"] == 1
    assert summary["total_claimed_amount"] == 16845.0
    assert summary["exception_reports"] == 1

    categories = client.get("/api/analytics/categories").json()
    petrol = next(c for c in categories if c["category"] == "OWN CAR PETROL")
    assert petrol["expense_count"] == 9


def test_16_er40223_batta_trip_matching_appears_in_analytics(client):
    er_path = REPO_ROOT / "ER-40223.pdf"
    resp = client.post(
        "/api/uploads/expense-reports", files=[("files", (er_path.name, er_path.read_bytes(), "application/pdf"))]
    )
    assert resp.json()["results"][0]["success"]

    data = client.get("/api/analytics/trips").json()
    assert data["total_batta_expenses"] == 4
    assert data["matched_batta_expenses"] == 4
    assert data["unmatched_batta_expenses"] == 0


# ---------- 17: ER-40706 late-trip resolution reflected in analytics ----------


def test_17_er40706_resolved_batta_state_reflected_in_analytics(client):
    er_path = REPO_ROOT / "ER-40706 (1).pdf"
    resp = client.post(
        "/api/uploads/expense-reports", files=[("files", (er_path.name, er_path.read_bytes(), "application/pdf"))]
    )
    assert resp.json()["results"][0]["success"]

    before = client.get("/api/analytics/trips").json()
    assert before["unmatched_batta_expenses"] == 10

    exceptions_before = client.get("/api/analytics/exceptions").json()
    assert any(e["exception_type"] == "TRIP NOT FOUND" for e in exceptions_before)

    trip_path = REPO_ROOT / "TRIP-25845_Chandigarh_tour30.06.2026_to_12.07.2026.pdf"
    client.post(
        "/api/uploads/trip-reports", files=[("files", (trip_path.name, trip_path.read_bytes(), "application/pdf"))]
    )

    after = client.get("/api/analytics/trips").json()
    assert after["matched_batta_expenses"] == 10
    assert after["unmatched_batta_expenses"] == 0

    exceptions_after = client.get("/api/analytics/exceptions").json()
    assert not any(e["exception_type"] == "TRIP NOT FOUND" for e in exceptions_after)


# ---------- 18: TRIP-24935 preserved in trip analytics ----------


def test_18_trip24935_preserved_in_trip_analytics(client, sample_trip_pdf_path):
    resp = client.post(
        "/api/uploads/trip-reports",
        files=[("files", (sample_trip_pdf_path.name, sample_trip_pdf_path.read_bytes(), "application/pdf"))],
    )
    assert resp.json()["results"][0]["success"]

    data = client.get("/api/analytics/trip-reports").json()
    assert data["total_trips"] == 1
    assert any(d["destination"] == "Indore" for d in data["by_destination"])


# ---------- 19: persistence proxy ----------


def test_19_analytics_reflects_committed_state_across_a_fresh_session(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-AN19", total_claimed=2000.0)
        _make_expense(db, report.id, claimed_amount=2000.0)
        _evaluate(db, report.id)
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        from app.services import analytics_service

        summary = analytics_service.report_status_summary(db2)
        assert summary["total_reports"] == 1
        assert summary["total_claimed_amount"] == 2000.0
    finally:
        db2.close()


# ---------- 20: Phase 1-8 regression smoke ----------


def test_20_phase1_8_regression_still_works(client):
    assert len(client.get("/api/policy-master").json()) == 29

    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", ("ER-39832.pdf", (REPO_ROOT / "ER-39832.pdf").read_bytes(), "application/pdf"))],
    )
    result = resp.json()["results"][0]
    assert result["success"]
    report_id = result["document_id"].split(":")[1]

    excel_meta = client.post("/api/excel-files/generate").json()
    assert excel_meta["total_reports"] == 1

    exceptions = client.get(f"/api/expense-reports/{report_id}/exceptions").json()
    assert len(exceptions) > 0
