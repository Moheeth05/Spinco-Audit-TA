"""Phase 6 Trip Report + Batta matching tests.

Sections:
  1-16: pure unit tests against services/extraction/trip_matcher.py (no DB) -
        each scoring signal and decision-threshold branch, using plain dicts
        exactly as batta_pipeline.py builds them.
  17-22: DB/API-level integration tests via services/extraction/batta_pipeline.py,
         services/validation/checks.py's check_batta_trip_match, the real
         ER-40223/TRIP-25167 (embedded+standalone dedup) and ER-40706/TRIP-25845
         (later-upload re-evaluation) reference cases, source-page provenance,
         persistence, and Phase 1-5 regression.
"""

from datetime import date
from pathlib import Path

from app.services.extraction import trip_matcher

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _trip(**kwargs):
    defaults = dict(
        id=1,
        trip_id="TRIP-90001",
        is_embedded=False,
        employee_code="T100",
        employee_name="TEST EMPLOYEE",
        employee_email=None,
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 5),
        destination="Indore",
        legs=[{"from_date": date(2026, 7, 1), "to_date": date(2026, 7, 1)}],
    )
    defaults.update(kwargs)
    return defaults


def _batta(**kwargs):
    defaults = dict(
        expense_date=date(2026, 7, 2),
        expense_details="BATTA",
        employee_code="T100",
        employee_name="TEST EMPLOYEE",
        employee_email=None,
        embedded_trip_id=None,
    )
    defaults.update(kwargs)
    return defaults


# ---------- 1-3: standalone/embedded extraction + legs (real documents) ----------


def test_1_standalone_trip_report_extraction_preserves_known_values(client, sample_trip_pdf_path):
    resp = client.post(
        "/api/uploads/trip-reports",
        files=[("files", (sample_trip_pdf_path.name, sample_trip_pdf_path.read_bytes(), "application/pdf"))],
    )
    result = resp.json()["results"][0]
    assert result["success"]
    report_id = result["document_id"].split(":")[1]
    detail = client.get(f"/api/trip-reports/{report_id}/details").json()
    assert detail["employee_name"] == "PRANAY RAJESH PAWAR"
    assert detail["employee_code"] == "T465"
    assert detail["start_date"] == "2026-07-13"
    assert detail["end_date"] == "2026-07-19"
    assert "Medilux" in (detail["purpose"] or "") or "Pithampur" in (detail["purpose"] or "")


def test_2_embedded_trip_summary_still_reconstructs_5_legs(client):
    er_path = REPO_ROOT / "ER-40223.pdf"
    resp = client.post(
        "/api/uploads/expense-reports", files=[("files", (er_path.name, er_path.read_bytes(), "application/pdf"))]
    )
    result = resp.json()["results"][0]
    doc_id = result["document_id"]
    detail = client.get(f"/api/documents/{doc_id}/extraction").json()
    embedded = detail["embedded_trip_summary"]
    assert embedded is not None
    assert embedded["trip_id"] == "TRIP-25167"
    assert len(embedded["legs"]) == 5


def test_3_trip_legs_persisted_for_standalone_report(client, sample_trip_pdf_path):
    resp = client.post(
        "/api/uploads/trip-reports",
        files=[("files", (sample_trip_pdf_path.name, sample_trip_pdf_path.read_bytes(), "application/pdf"))],
    )
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]
    detail = client.get(f"/api/trip-reports/{report_id}/details").json()
    assert len(detail["legs"]) >= 1
    assert all(leg["from_location"] for leg in detail["legs"])


# ---------- 4-7: identity/trip-number signals ----------


def test_4_employee_code_match_scores_positively():
    score, reasons = trip_matcher.score_pair(_batta(employee_code="T100"), _trip(employee_code="T100"))
    assert score > 0
    assert any("employee code" in r.lower() for r in reasons)


def test_5_employee_name_only_match_when_no_code_available():
    exp = _batta(employee_code=None, employee_name="MANEESH KUMAR")
    trip = _trip(employee_code=None, employee_name="MANEESH KUMAR")
    score, reasons = trip_matcher.score_pair(exp, trip)
    assert any("employee name" in r.lower() for r in reasons)


def test_6_employee_email_match_scores_like_employee_code():
    exp = _batta(employee_code=None, employee_email="pranay@spincotech.com")
    trip = _trip(employee_code=None, employee_email="pranay@spincotech.com")
    score, reasons = trip_matcher.score_pair(exp, trip)
    assert any("email" in r.lower() for r in reasons)


def test_7_trip_number_matches_own_embedded_summary():
    exp = _batta(embedded_trip_id="TRIP-25167")
    trip = _trip(trip_id="TRIP-25167", is_embedded=False)
    score, reasons = trip_matcher.score_pair(exp, trip)
    assert any("trip number matches" in r.lower() for r in reasons)


# ---------- 8-11: date-range containment ----------


def test_8_date_within_range_scores_positively():
    score, reasons = trip_matcher.score_pair(_batta(expense_date=date(2026, 7, 3)), _trip())
    assert any("travel period" in r.lower() for r in reasons)


def test_9_inclusive_start_date_boundary():
    score, reasons = trip_matcher.score_pair(_batta(expense_date=date(2026, 7, 1)), _trip(start_date=date(2026, 7, 1), end_date=date(2026, 7, 5)))
    assert any("travel period" in r.lower() for r in reasons)


def test_10_inclusive_end_date_boundary():
    score, reasons = trip_matcher.score_pair(_batta(expense_date=date(2026, 7, 5)), _trip(start_date=date(2026, 7, 1), end_date=date(2026, 7, 5)))
    assert any("travel period" in r.lower() for r in reasons)


def test_11_date_outside_range_does_not_score_date_signal():
    score, reasons = trip_matcher.score_pair(_batta(expense_date=date(2026, 8, 1)), _trip(start_date=date(2026, 7, 1), end_date=date(2026, 7, 5)))
    assert not any("travel period" in r.lower() for r in reasons)


# ---------- 12: destination ----------


def test_12_destination_text_similarity_signal():
    exp = _batta(expense_details="Indore trip allowance")
    trip = _trip(destination="Indore")
    score, reasons = trip_matcher.score_pair(exp, trip)
    assert any("destination" in r.lower() for r in reasons)


# ---------- 13-16: full match_batta() decision outcomes ----------


def test_13_strong_match_is_matched_high_confidence():
    # Employee code (+35) + date-in-range (+30) + leg-date bonus (+10) = 75 -> HIGH.
    exp = _batta(employee_code="T100", expense_date=date(2026, 7, 1))
    outcome = trip_matcher.match_batta(exp, [_trip(id=1, employee_code="T100")])
    assert outcome.status == "MATCHED"
    assert outcome.confidence == "HIGH"
    assert outcome.matched_trip_report_id == 1


def test_14_multiple_equally_good_candidates_is_ambiguous():
    exp = _batta(employee_code="T100", expense_date=date(2026, 7, 2))
    trips = [
        _trip(id=1, trip_id="TRIP-A", employee_code="T100", destination="Indore"),
        _trip(id=2, trip_id="TRIP-B", employee_code="T100", destination="Indore"),
    ]
    outcome = trip_matcher.match_batta(exp, trips)
    assert outcome.status == "MULTIPLE_CANDIDATES"
    assert outcome.matched_trip_report_id is None
    assert len(outcome.candidates) == 2


def test_15_no_trip_found_is_no_match():
    outcome = trip_matcher.match_batta(_batta(), [])
    assert outcome.status == "NO_MATCH"
    assert outcome.matched_trip_report_id is None
    assert outcome.employee_mismatch is False


def test_16_employee_mismatch_is_no_match_with_flag_set():
    exp = _batta(employee_code="T100", expense_date=date(2026, 7, 2))
    other_employee_trip = _trip(id=9, employee_code="T999")
    outcome = trip_matcher.match_batta(exp, [other_employee_trip])
    assert outcome.status == "NO_MATCH"
    assert outcome.employee_mismatch is True


# ---------- 17-18: later Trip Report upload triggers automatic re-evaluation ----------


def test_17_18_later_trip_report_upload_resolves_previously_unmatched_batta(client):
    er_path = REPO_ROOT / "ER-40706 (1).pdf"
    resp = client.post(
        "/api/uploads/expense-reports", files=[("files", (er_path.name, er_path.read_bytes(), "application/pdf"))]
    )
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]

    expenses_before = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    battas_before = [e for e in expenses_before if (e["category"] or "").upper().startswith("BATTA")]
    assert battas_before
    assert all(e["trip_match_status"] == "NO_MATCH" for e in battas_before)

    ev_before = client.get(f"/api/expenses/{battas_before[0]['id']}/validations").json()
    batta_check_before = next(v for v in ev_before if v["check_type"] == "Batta Trip Match Check")
    assert batta_check_before["result"] == "EXCEPTION"
    assert batta_check_before["exception_type"] == "TRIP NOT FOUND"

    trip_path = REPO_ROOT / "TRIP-25845_Chandigarh_tour30.06.2026_to_12.07.2026.pdf"
    trip_resp = client.post(
        "/api/uploads/trip-reports", files=[("files", (trip_path.name, trip_path.read_bytes(), "application/pdf"))]
    )
    assert trip_resp.json()["results"][0]["success"]

    expenses_after = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    battas_after = [e for e in expenses_after if (e["category"] or "").upper().startswith("BATTA")]
    assert all(e["trip_match_status"] == "MATCHED" for e in battas_after)

    ev_after = client.get(f"/api/expenses/{battas_after[0]['id']}/validations").json()
    batta_check_after = next(v for v in ev_after if v["check_type"] == "Batta Trip Match Check")
    assert batta_check_after["result"] == "PASS"


# ---------- 19: embedded + standalone duplicate trip handling ----------


def test_19_embedded_and_standalone_same_trip_do_not_create_false_ambiguity(client):
    er_path = REPO_ROOT / "ER-40223.pdf"
    er_resp = client.post(
        "/api/uploads/expense-reports", files=[("files", (er_path.name, er_path.read_bytes(), "application/pdf"))]
    )
    report_id = er_resp.json()["results"][0]["document_id"].split(":")[1]

    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    battas = [e for e in expenses if (e["category"] or "").upper().startswith("BATTA")]
    assert battas
    # Matched via the embedded trip summary alone (standalone TRIP-25167 not uploaded yet) - never MULTIPLE_CANDIDATES.
    assert all(e["trip_match_status"] == "MATCHED" for e in battas)

    trip_path = REPO_ROOT / "TRIP-25167_Raipur,_Billai,_Ahmedabad-_27thJuly_-30th_July_2026.pdf"
    trip_resp = client.post(
        "/api/uploads/trip-reports", files=[("files", (trip_path.name, trip_path.read_bytes(), "application/pdf"))]
    )
    trip_report_id = trip_resp.json()["results"][0]["document_id"].split(":")[1]

    # Still a clean single MATCHED outcome, not MULTIPLE_CANDIDATES, even though
    # a second (standalone) source for the same trip number now exists.
    expenses_after = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    battas_after = [e for e in expenses_after if (e["category"] or "").upper().startswith("BATTA")]
    assert all(e["trip_match_status"] == "MATCHED" for e in battas_after)

    # Source provenance is not silently discarded - the standalone trip's own
    # batta-matches view must still show these expenses, even though it isn't
    # the winning source.
    standalone_matches = client.get(f"/api/trip-reports/{trip_report_id}/batta-matches").json()
    assert len(standalone_matches) == len(battas_after)
    assert all(m["is_matched_here"] is False for m in standalone_matches)


# ---------- 20: source-page provenance ----------


def test_20_batta_check_source_page_traces_to_embedded_trip_summary(client):
    er_path = REPO_ROOT / "ER-40223.pdf"
    resp = client.post(
        "/api/uploads/expense-reports", files=[("files", (er_path.name, er_path.read_bytes(), "application/pdf"))]
    )
    doc_id = resp.json()["results"][0]["document_id"]
    report_id = doc_id.split(":")[1]
    detail = client.get(f"/api/documents/{doc_id}/extraction").json()
    embedded_page = detail["embedded_trip_summary"]["source_page"]

    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    first_batta = next(e for e in expenses if (e["category"] or "").upper().startswith("BATTA"))
    ev = client.get(f"/api/expenses/{first_batta['id']}/validations").json()
    batta_check = next(v for v in ev if v["check_type"] == "Batta Trip Match Check")
    assert batta_check["source_page"] == embedded_page


# ---------- 21: persistence (DB-level, mirrors Phase 5's restart-persistence pattern) ----------


def test_21_trip_match_fields_persist_across_a_fresh_session(client):
    er_path = REPO_ROOT / "ER-40223.pdf"
    resp = client.post(
        "/api/uploads/expense-reports", files=[("files", (er_path.name, er_path.read_bytes(), "application/pdf"))]
    )
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]

    from app.database import SessionLocal
    from app.models.expense_report import Expense

    db = SessionLocal()
    try:
        battas = db.query(Expense).filter(Expense.expense_report_id == int(report_id)).all()
        battas = [e for e in battas if (e.category or "").upper().startswith("BATTA")]
        assert battas
        assert all(e.trip_match_status == "MATCHED" and e.trip_report_id is not None for e in battas)
    finally:
        db.close()


# ---------- 22: Phase 1-5 regression ----------


def test_22_phase1_5_regression_still_works(client, sample_pdf_path):
    assert len(client.get("/api/policy-master").json()) == 29

    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (sample_pdf_path.name, sample_pdf_path.read_bytes(), "application/pdf"))],
    )
    result = resp.json()["results"][0]
    assert result["success"]
    report_id = result["document_id"].split(":")[1]

    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    first = next(e for e in expenses if e["s_no"] == 1)
    assert first["claimed_amount"] == 2000.0
    assert first["claimed_amount"] != 2500.0

    bills = client.get(f"/api/expense-reports/{report_id}/bills").json()
    amounts = [b["amount"] for b in bills if b["amount"] is not None]
    assert 2500.0 not in amounts
    assert 2000.0 in amounts

    ev = client.get(f"/api/expenses/{first['id']}/validations").json()
    amount_check = next(v for v in ev if v["check_type"] == "Bill Amount Check")
    assert first["bill_match_status"] == "MULTIPLE_CANDIDATES"
    assert amount_check["result"] == "REVIEW_REQUIRED"

    # Non-Batta expenses must never get a Batta Trip Match Check result other than NOT_APPLICABLE.
    for e in expenses:
        ev_e = client.get(f"/api/expenses/{e['id']}/validations").json()
        batta_check = next((v for v in ev_e if v["check_type"] == "Batta Trip Match Check"), None)
        if batta_check:
            assert batta_check["result"] == "NOT_APPLICABLE"
