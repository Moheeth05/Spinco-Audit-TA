"""Phase 9 Home Dashboard tests - services/analytics_service.py + api/dashboard.py.

Sections:
  1-10: synthetic DB-level scenarios (SessionLocal-direct, mirroring
        test_exception_engine.py's own pattern) - KPI definitions, no
        double-counting, empty-database behavior, chronological month
        sorting.
  11-13: real-document reference cases (ER-39832) via the `client` fixture.
"""

import uuid
from datetime import date

from app.database import SessionLocal
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.trip import TripReport
from app.services.exception_engine import evaluate_report
from app.services.validation.pipeline import validate_expense_report

REPO_ROOT_STR = "D:/Bills"


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


# ---------- 1-2: empty database ----------


def test_1_empty_database_kpis_are_all_zero(client):
    resp = client.get("/api/dashboard/kpis")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {
        "total_expense_reports": 0,
        "total_trip_reports": 0,
        "total_claimed_amount": 0.0,
        "account_verified_reports": 0,
        "exception_reports": 0,
        "review_required_reports": 0,
        "total_expenses": 0,
    }


def test_2_empty_database_monthly_and_category_endpoints_return_empty_lists(client):
    assert client.get("/api/dashboard/monthly-uploads").json() == []
    assert client.get("/api/dashboard/monthly-claimed").json() == []
    assert client.get("/api/dashboard/category-breakdown").json() == []
    assert client.get("/api/dashboard/recent-uploads").json() == []


# ---------- 3-6: KPI definitions ----------


def test_3_total_expense_reports_is_a_plain_count(client):
    db = SessionLocal()
    try:
        for i in range(3):
            _make_report(db, er_id=f"ER-DASH{i}")
    finally:
        db.close()

    assert client.get("/api/dashboard/kpis").json()["total_expense_reports"] == 3


def test_4_status_kpis_match_overall_status_exactly(client):
    db = SessionLocal()
    try:
        r1 = _make_report(db, er_id="ER-DASHVER", total_claimed=2000.0)
        _make_bill(db, r1.id, amount=2000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill = db.query(Bill).filter(Bill.expense_report_id == r1.id).first()
        _make_expense(
            db, r1.id, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_id=bill.id, bill_match_status="MATCHED",
        )
        _evaluate(db, r1.id)

        r2 = _make_report(db, er_id="ER-DASHEXC", total_claimed=9000.0)
        _make_bill(db, r2.id, amount=9000.0, bill_date=date(2026, 6, 1), category_guess="Fuel")
        bill2 = db.query(Bill).filter(Bill.expense_report_id == r2.id).first()
        _make_expense(
            db, r2.id, category="OWN CAR PETROL", claimed_amount=9000.0, expense_date=date(2026, 6, 1),
            bill_id=bill2.id, bill_match_status="MATCHED",
        )
        _evaluate(db, r2.id)

        r3 = _make_report(db, er_id="ER-DASHREV", total_claimed=2000.0)
        _make_expense(
            db, r3.id, category="OWN CAR PETROL", claimed_amount=2000.0, expense_date=date(2026, 6, 1),
            bill_match_status="MULTIPLE_CANDIDATES",
        )
        _evaluate(db, r3.id)
    finally:
        db.close()

    kpis = client.get("/api/dashboard/kpis").json()
    assert kpis["account_verified_reports"] == 1
    assert kpis["exception_reports"] == 1
    assert kpis["review_required_reports"] == 1
    assert kpis["total_expense_reports"] == 3


def test_5_total_claimed_amount_sums_report_total_claimed_not_expense_lines(client):
    """The report's own stated total is authoritative (matches the
    already-established Phase 1 convention) - not a re-sum of expense
    lines, which can legitimately differ on a TOTAL MISMATCH report."""
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-DASHTOTAL", total_claimed=5000.0)
        # Line items sum to 2000, deliberately NOT 5000 - a genuine TOTAL MISMATCH.
        _make_expense(db, report.id, claimed_amount=2000.0)
        _evaluate(db, report.id)
    finally:
        db.close()

    kpis = client.get("/api/dashboard/kpis").json()
    assert kpis["total_claimed_amount"] == 5000.0


def test_6_total_expenses_is_a_plain_expense_row_count(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-DASHCOUNT")
        for s_no in range(1, 5):
            _make_expense(db, report.id, s_no=s_no, claimed_amount=100.0)
    finally:
        db.close()

    assert client.get("/api/dashboard/kpis").json()["total_expenses"] == 4


# ---------- 7-8: no double counting (trip reports, embedded exclusion) ----------


def test_7_embedded_trips_never_counted_as_trip_reports(client):
    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-DASHEMB")
        embedded = TripReport(is_embedded=True, source_expense_report_id=report.id, trip_id="TRIP-EMB1", report_month="2026-06")
        db.add(embedded)
        standalone = TripReport(is_embedded=False, trip_id="TRIP-STANDALONE1", report_month="2026-06", start_date=date(2026, 6, 1))
        db.add(standalone)
        db.commit()
    finally:
        db.close()

    kpis = client.get("/api/dashboard/kpis").json()
    assert kpis["total_trip_reports"] == 1

    uploads = client.get("/api/dashboard/monthly-uploads").json()
    june = next(u for u in uploads if u["month"] == "2026-06")
    assert june["trip_reports"] == 1


def test_8_recent_uploads_have_a_navigable_document_id(client, sample_pdf_path):
    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (sample_pdf_path.name, sample_pdf_path.read_bytes(), "application/pdf"))],
    )
    result = resp.json()["results"][0]

    recent = client.get("/api/dashboard/recent-uploads").json()
    assert len(recent) == 1
    assert recent[0]["document_id"] == result["document_id"]


# ---------- 9-10: chronological month sorting ----------


def test_9_monthly_uploads_sorted_chronologically_not_alphabetically(client):
    db = SessionLocal()
    try:
        for month in ("2026-12", "2026-02", "2026-08"):
            r = ExpenseReport(
                filename="t.pdf", original_filename="t.pdf", file_hash=f"h-{uuid.uuid4()}", file_size=10,
                source_path="x.pdf", report_month=month, total_claimed=0.0,
            )
            db.add(r)
        db.commit()
    finally:
        db.close()

    months = [u["month"] for u in client.get("/api/dashboard/monthly-uploads").json()]
    assert months == ["2026-02", "2026-08", "2026-12"]


def test_10_month_label_is_human_readable_and_chronological():
    from app.services.analytics_service import _month_label

    assert _month_label("2026-02") == "Feb 2026"
    assert _month_label("2026-12") == "Dec 2026"


# ---------- 11-13: real ER-39832 reference case ----------


def test_11_er39832_reference_case_in_dashboard(client, sample_pdf_path):
    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (sample_pdf_path.name, sample_pdf_path.read_bytes(), "application/pdf"))],
    )
    assert resp.json()["results"][0]["success"]

    kpis = client.get("/api/dashboard/kpis").json()
    assert kpis["total_expense_reports"] == 1
    assert kpis["total_claimed_amount"] == 16845.0  # ER-39832's real stated total
    assert kpis["exception_reports"] == 1  # genuine MISSING BILL/DUPLICATE BILL/OVER LIMIT exceptions
    assert kpis["account_verified_reports"] == 0

    categories = client.get("/api/dashboard/category-breakdown").json()
    petrol = next(c for c in categories if c["category"] == "OWN CAR PETROL")
    assert petrol["claimed_amount"] > 0
    assert petrol["expense_count"] == 9


def test_12_er39832_petrol_reference_never_2500_via_category_totals(client, sample_pdf_path):
    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (sample_pdf_path.name, sample_pdf_path.read_bytes(), "application/pdf"))],
    )
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]
    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    first = next(e for e in expenses if e["s_no"] == 1)
    assert first["claimed_amount"] == 2000.0
    assert first["claimed_amount"] != 2500.0


def test_13_dashboard_and_analytics_report_status_counts_agree(client, sample_pdf_path):
    client.post(
        "/api/uploads/expense-reports",
        files=[("files", (sample_pdf_path.name, sample_pdf_path.read_bytes(), "application/pdf"))],
    )
    dashboard_kpis = client.get("/api/dashboard/kpis").json()
    analytics_summary = client.get("/api/analytics/summary").json()

    assert dashboard_kpis["total_expense_reports"] == analytics_summary["total_reports"]
    assert dashboard_kpis["account_verified_reports"] == analytics_summary["account_verified_reports"]
    assert dashboard_kpis["exception_reports"] == analytics_summary["exception_reports"]
    assert dashboard_kpis["review_required_reports"] == analytics_summary["review_required_reports"]
    assert dashboard_kpis["total_claimed_amount"] == analytics_summary["total_claimed_amount"]
