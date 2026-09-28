from pathlib import Path

import fitz
import pytest

from app.services.extraction import bill_matcher, bill_parser

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ALL_ER_PDFS = ["ER-39832.pdf", "ER-40223.pdf", "ER-40706 (1).pdf", "ER-40782.pdf"]


def _upload(client, filename, content):
    return client.post("/api/uploads/expense-reports", files=[("files", (filename, content, "application/pdf"))])


def _make_text_pdf(lines: list[str]) -> bytes:
    from io import BytesIO

    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 16
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _make_er_with_bill_pages(bill_page_lines: list[list[str]]) -> bytes:
    """A minimal synthetic Expense Report: page 1 has a real header/expense
    table/report-summary (so the extraction pipeline runs end to end), then
    one page per entry in bill_page_lines as supporting documents."""
    from io import BytesIO

    doc = fitz.open()
    page = doc.new_page()
    header_lines = [
        "Expense Report",
        "ER-90001",
        "Amount to be Reimbursed",
        "Rs.500.00",
        "Report",
        "January",
        "Submitted By",
        "TEST EMPLOYEE",
        "test@spincotech.com",
        "Submitted On",
        "01/01/2026",
        "EXPENSE SUMMARY",
        "1.",
        "01/01/2026",
        "COURIER CHARGES",
        "Test courier",
        "Rs.500.00",
        "REPORT SUMMARY BY CURRENCY",
        "Total Reimbursable Amount",
        "Rs.500.00",
        "TEST EMPLOYEE ( T001 )",
    ]
    y = 40
    for line in header_lines:
        page.insert_text((30, y), line, fontsize=9)
        y += 12

    for lines in bill_page_lines:
        bp = doc.new_page()
        y = 40
        for line in lines:
            bp.insert_text((30, y), line, fontsize=9)
            y += 12

    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


# ---------- 1-8: bill field extraction (unit-level, via the parser directly) ----------


def test_bill_page_detection_rejects_blank_page():
    assert bill_parser.has_bill_evidence("") is False
    assert bill_parser.has_bill_evidence("9") is False


def test_petrol_receipt_amount_extraction():
    text = "Bharat Petroleum\nProduct: DIESEL\nDate: 01/06/2026\nAmount (Rs.): 2000.00\nTHANK YOU VISIT AGAIN"
    parsed = bill_parser.parse_bill_text(text)
    assert parsed.amount == 2000.0
    assert parsed.bill_date is not None
    assert parsed.category_guess == "Fuel"


def test_bill_amount_extraction_currency_variants():
    for text, expected in [
        ("Total\n₹1,455.00\n", 1455.0),
        ("Grand Total\nRs. 220\n", 220.0),
        ("Amount: INR 2,000.50\n", 2000.5),
    ]:
        amt, _, _ = bill_parser._find_amount(text)
        assert amt == expected, text


def test_bill_date_extraction_supports_dash_format():
    parsed = bill_parser.parse_bill_text("Date: 23-06-2026\nAmount (Rs.): 2000.00")
    assert parsed.bill_date is not None
    assert parsed.bill_date.day == 23 and parsed.bill_date.month == 6


def test_vendor_extraction_prefers_company_suffix_line():
    text = "RATHNA ELECTRICALS & HARDWARES\nGSTIN: 33ARWMPK8203C1ZW\nGrand Total\nRs.220.00"
    parsed = bill_parser.parse_bill_text(text)
    assert parsed.vendor == "RATHNA ELECTRICALS & HARDWARES"


def test_vendor_extraction_ignores_disclaimer_prose_containing_suffix_words():
    text = "Jul 27, 2026\nThanks for riding\nTotal\n₹2,329.65\nUber does not provide transport services; Issued on behalf of X"
    parsed = bill_parser.parse_bill_text(text)
    assert parsed.vendor != "Uber does not provide transport services; Issued on behalf of X"


def test_bill_number_extraction():
    text = "Invoice No: INV-2026-445\nGrand Total\nRs.500.00"
    assert bill_parser._find_bill_number(text) == "INV-2026-445"


def test_gstin_extraction():
    text = "GSTIN : 32ARWPK8203C1ZW\nSome shop"
    parsed = bill_parser.parse_bill_text(text)
    assert parsed.gstin == "32ARWPK8203C1ZW"


def test_description_category_guess():
    parsed = bill_parser.parse_bill_text("HOTEL PICCADILY\nRoom Charge\nGrand Total\nRs.1455.00")
    assert parsed.category_guess == "Lodging"


# ---------- money/date edge cases specific to bill parsing ----------


def test_amount_label_word_boundary_ignores_merged_table_header():
    # "QtyTotal" must not be treated as a "total" label match.
    text = "S.No Dish Name Rate QtyTotal\n1 Item A 100 1 100\nGrand Total\nRs.100.00"
    amt, _, _ = bill_parser._find_amount(text)
    assert amt == 100.0


def test_small_unlabeled_decimal_not_treated_as_amount():
    # A star-rating-like number with no label anywhere must not become "the amount".
    text = "You rode with Dev Nath 4.98\nWant to review your trip history?"
    amt, _, warnings = bill_parser._find_amount(text)
    assert amt is None


def test_multi_bill_page_split_on_distinct_gstins():
    text = (
        "Shop A\nGSTIN: 32ARWPK8203C1ZW\nTotal Rs.100.00\n"
        "Shop B\nGSTIN: 24AALCS2698L1ZO\nTotal Rs.200.00"
    )
    segments = bill_parser.split_bill_segments(text)
    assert len(segments) == 2


def test_single_gstin_page_not_split():
    text = "Shop A\nGSTIN: 32ARWPK8203C1ZW\nTotal Rs.100.00"
    assert bill_parser.split_bill_segments(text) == [text]


# ---------- 9-13: matching ----------


def test_matcher_exact_amount_and_date_is_high_confidence_match():
    expense = {"claimed_amount": 2000.0, "expense_date": __import__("datetime").date(2026, 6, 1), "category": "OWN CAR PETROL", "expense_details": "petrol"}
    bills = [{"id": 1, "amount": 2000.0, "bill_date": __import__("datetime").date(2026, 6, 1), "vendor": None, "description": None, "category_guess": "Fuel", "bill_number": None}]
    outcome = bill_matcher.match_expense(expense, bills)
    assert outcome.status == "MATCHED"
    assert outcome.confidence == "HIGH"
    assert outcome.matched_bill_id == 1
    assert any("exact match" in r.lower() for r in outcome.reasons)


def test_matcher_no_bills_is_no_match():
    outcome = bill_matcher.match_expense({"claimed_amount": 100.0, "expense_date": None, "category": None, "expense_details": None}, [])
    assert outcome.status == "NO_MATCH"
    assert outcome.matched_bill_id is None


def test_matcher_multiple_equally_good_candidates():
    d = __import__("datetime").date(2026, 6, 1)
    expense = {"claimed_amount": 2000.0, "expense_date": d, "category": "OWN CAR PETROL", "expense_details": None}
    bills = [
        {"id": 1, "amount": 2000.0, "bill_date": None, "vendor": None, "description": None, "category_guess": None, "bill_number": None},
        {"id": 2, "amount": 2000.0, "bill_date": None, "vendor": None, "description": None, "category_guess": None, "bill_number": None},
    ]
    outcome = bill_matcher.match_expense(expense, bills)
    assert outcome.status == "MULTIPLE_CANDIDATES"
    assert outcome.matched_bill_id is None
    assert len(outcome.candidates) == 2


def test_matcher_weak_evidence_is_review_required_not_matched():
    expense = {"claimed_amount": 500.0, "expense_date": None, "category": "COURIER CHARGES", "expense_details": None}
    bills = [{"id": 1, "amount": 501.0, "bill_date": None, "vendor": None, "description": None, "category_guess": None, "bill_number": None}]
    outcome = bill_matcher.match_expense(expense, bills)
    assert outcome.status in ("NO_MATCH", "REVIEW_REQUIRED")
    assert outcome.matched_bill_id is None


def test_matcher_amount_uniqueness_bonus():
    d = __import__("datetime").date(2026, 6, 1)
    expense = {"claimed_amount": 2000.0, "expense_date": d, "category": None, "expense_details": None}
    unique_bill = {"id": 1, "amount": 2000.0, "bill_date": None, "vendor": None, "description": None, "category_guess": None, "bill_number": None}
    score_unique, _ = bill_matcher.score_pair(expense, unique_bill, amount_is_unique=True)
    score_not_unique, _ = bill_matcher.score_pair(expense, unique_bill, amount_is_unique=False)
    assert score_unique > score_not_unique


# ---------- 15/21: mandatory reference regression ----------


def test_er39832_petrol_bill_reference_regression(client, sample_pdf_path):
    """The spec's mandatory case: the 01/06/2026 OWN CAR PETROL claim of
    Rs.2,000 must never be extracted/matched as Rs.2,500, and its supporting
    bill's amount must genuinely be extracted as Rs.2,000 from the source PDF."""
    result = _upload(client, sample_pdf_path.name, sample_pdf_path.read_bytes()).json()["results"][0]
    doc_id = result["document_id"]
    report_id = doc_id.split(":")[1]

    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    first = next(e for e in expenses if e["s_no"] == 1)
    assert first["claimed_amount"] == 2000.0
    assert first["claimed_amount"] != 2500.0

    bills = client.get(f"/api/expense-reports/{report_id}/bills").json()
    amounts = [b["amount"] for b in bills if b["amount"] is not None]
    assert 2500.0 not in amounts, "A bill amount was fabricated as 2500 - this must never happen."
    assert 2000.0 in amounts, "The real Rs.2,000 petrol bill must be extracted from the source PDF."

    # Whatever the match outcome (a MATCHED single bill, or an honest
    # MULTIPLE_CANDIDATES given this document genuinely has three Rs.2,000
    # petrol bills with ambiguous/missing dates), no fabricated mismatch may
    # be recorded, and if matched, the amount must be exactly right.
    assert first["bill_match_status"] in ("MATCHED", "MULTIPLE_CANDIDATES", "REVIEW_REQUIRED")
    if first["bill_match_status"] == "MATCHED":
        assert first["bill_amount"] == 2000.0


# ---------- 16-17: source page tracking, missing bill infrastructure ----------


def test_bills_have_source_page_numbers(client, sample_pdf_path):
    result = _upload(client, sample_pdf_path.name, sample_pdf_path.read_bytes()).json()["results"][0]
    report_id = result["document_id"].split(":")[1]
    bills = client.get(f"/api/expense-reports/{report_id}/bills").json()
    assert len(bills) > 0
    assert all(b["page_number"] is not None for b in bills)


def test_expense_with_no_supporting_bill_is_no_match_not_invented():
    """A synthetic ER with zero bill pages - every expense must come back
    NO_MATCH, never a fabricated match."""
    pdf_bytes = _make_er_with_bill_pages([])

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        login = c.post("/api/auth/login", json={"email": "intern@spincotech.com", "password": "ChangeMe123!"}).json()
        result = c.post(
            "/api/uploads/expense-reports",
            files=[("files", ("no_bills.pdf", pdf_bytes, "application/pdf"))],
            headers={"X-CSRF-Token": login["csrf_token"]},
        ).json()["results"][0]
        assert result["success"]
        report_id = result["document_id"].split(":")[1]
        expenses = c.get(f"/api/expense-reports/{report_id}/expenses").json()
        assert expenses[0]["bill_match_status"] == "NO_MATCH"
        assert expenses[0]["bill_id"] is None if "bill_id" in expenses[0] else True


# ---------- 18-20: multi-amount unlabeled bills (post-UAT correctness fix) ----------
#
# A real document (ER-41115) had genuine bills whose "total" was never
# explicitly labeled ("Total"/"Amount"/etc.) - e.g. a ride-receipt page
# printing just "13.00 / 520.00 / 546.00" with no label at all (a taxable
# value + CGST/SGST + total). The old behavior discarded these entirely
# (amount=None), which meant a real, correctly-priced bill could never enter
# the matching candidate set at all (see bill_matcher.py's module docstring
# on candidate breadth) - producing a false MISSING BILL. The fix keeps the
# largest unlabeled figure as a LOW-confidence candidate rather than
# discarding it, since a bill total structurally can never be smaller than
# any line item/tax component printed alongside it.


def test_multiple_unlabeled_amounts_uses_largest_as_low_confidence_candidate():
    text = "Tax invoice issued by: Cabco\nGSTIN: 19AAGCI0467A1ZU\n13.00\n520.00\n546.00"
    amt, confidence, warnings = bill_parser._find_amount(text)
    assert amt == 546.0
    assert confidence == "LOW"
    assert any("largest" in w.lower() for w in warnings)


def test_multiple_unlabeled_amounts_parsed_bill_is_review_required_not_dropped():
    text = "Tax invoice issued by: Cabco\nGSTIN: 19AAGCI0467A1ZU\n13.00\n520.00\n546.00"
    parsed = bill_parser.parse_bill_text(text)
    assert parsed.amount == 546.0
    assert parsed.status == "REVIEW_REQUIRED"
    assert parsed.confidence == "LOW"


def test_multi_amount_low_confidence_bill_still_participates_in_matching():
    """The whole point of keeping the amount instead of discarding it: an
    otherwise-exact-matching expense must not come back MISSING BILL just
    because the bill's total was never explicitly labeled."""
    import datetime

    expense = {
        "claimed_amount": 546.0,
        "expense_date": None,
        "category": "CAB CHARGES",
        "expense_details": "cab to office",
    }
    bills = [
        {
            "id": 1,
            "amount": 546.0,
            "bill_date": None,
            "vendor": None,
            "description": None,
            "category_guess": None,
            "bill_number": None,
            "page_number": 5,
        }
    ]
    outcome = bill_matcher.match_expense(expense, bills)
    assert outcome.status != "NO_MATCH"
    assert outcome.matched_bill_id == 1 or 1 in [c.bill_id for c in outcome.candidates]


# ---------- 14: OCR bill extraction (reuses Phase 3's OCR engine) ----------


def test_ocr_bill_extraction_uses_shared_ocr_engine():
    from app.services.extraction.ocr_engine import TesseractOCREngine
    from app.services.extraction.shared import ocr_engine, text_extractor

    assert isinstance(ocr_engine, TesseractOCREngine)
    assert text_extractor.ocr_engine is ocr_engine


# ---------- 19/23: reprocessing does not duplicate bills; database persistence ----------


def test_rematch_bills_does_not_duplicate(client, sample_pdf_path):
    result = _upload(client, sample_pdf_path.name, sample_pdf_path.read_bytes()).json()["results"][0]
    doc_id = result["document_id"]
    report_id = doc_id.split(":")[1]

    first_count = len(client.get(f"/api/expense-reports/{report_id}/bills").json())
    client.post(f"/api/documents/{doc_id}/rematch-bills")
    client.post(f"/api/documents/{doc_id}/rematch-bills")
    second_count = len(client.get(f"/api/expense-reports/{report_id}/bills").json())

    assert first_count == second_count
    assert first_count > 0


def test_reprocess_document_does_not_duplicate_bills(client, sample_pdf_path):
    result = _upload(client, sample_pdf_path.name, sample_pdf_path.read_bytes()).json()["results"][0]
    doc_id = result["document_id"]
    report_id = doc_id.split(":")[1]

    before = len(client.get(f"/api/expense-reports/{report_id}/bills").json())
    client.post(f"/api/documents/{doc_id}/reprocess")
    after = len(client.get(f"/api/expense-reports/{report_id}/bills").json())
    assert before == after


# ---------- 20: bulk processing isolation ----------


def test_bulk_bill_extraction_isolated_per_document(client):
    files = [("files", (name, (REPO_ROOT / name).read_bytes(), "application/pdf")) for name in ALL_ER_PDFS]
    files.append(("files", ("bad.pdf", b"not a real pdf", "application/pdf")))
    resp = client.post("/api/uploads/expense-reports", files=files)
    results = resp.json()["results"]
    succeeded = [r for r in results if r["success"]]
    assert len(succeeded) == 4
    for r in succeeded:
        report_id = r["document_id"].split(":")[1]
        # must not raise / must return a real (possibly empty) list, independent of the failed upload
        bills = client.get(f"/api/expense-reports/{report_id}/bills").json()
        assert isinstance(bills, list)


# ---------- 30: real PDF validation across all 4 real ERs ----------


def test_all_real_expense_reports_bill_pipeline_runs_cleanly(client):
    summary = []
    for name in ALL_ER_PDFS:
        content = (REPO_ROOT / name).read_bytes()
        result = _upload(client, name, content).json()["results"][0]
        assert result["success"], name
        report_id = result["document_id"].split(":")[1]
        expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
        bills = client.get(f"/api/expense-reports/{report_id}/bills").json()
        matched = sum(1 for e in expenses if e["bill_match_status"] == "MATCHED")
        summary.append((name, len(expenses), len(bills), matched))
        # No status may be an invented business verdict - only the Phase 4 vocabulary,
        # plus NOT_APPLICABLE (post-UAT correction 2, Part 18: BATTA is never sent
        # through bill matching at all, since it has no supporting-document requirement).
        assert all(
            e["bill_match_status"] in ("MATCHED", "MULTIPLE_CANDIDATES", "NO_MATCH", "REVIEW_REQUIRED", "NOT_PROCESSED", "NOT_APPLICABLE")
            for e in expenses
        )
    # At least some real matching activity happened across the real document set.
    assert sum(m for _, _, _, m in summary) > 0


# ---------- Phase 1/2/3 regression ----------


def test_phase1_policy_master_regression(client):
    assert len(client.get("/api/policy-master").json()) == 29


def test_phase3_extraction_status_unaffected_by_bill_matching(client, sample_pdf_path):
    result = _upload(client, sample_pdf_path.name, sample_pdf_path.read_bytes()).json()["results"][0]
    assert result["extraction_status"] == "EXTRACTED"


# ---------- Post-UAT correction 2: the flagship CAB CHARGES example ----------
#
# ER-41115's real s_no=1: 2026-08-07, CAB CHARGES, claimed Rs.546.00. The
# matching bill (page 5) is a ride-hailing tax invoice: "Date: August 7,
# 2026", "Ride fare (in INR): Rs 520.00", "CGST 2.5%: Rs 13.00", "SGST/
# UTGST 2.5%: Rs 13.00", "Ride fare (in INR): Rs 546.00" - no "Total" label,
# a textual month-name date, and no literal "CAB"/"TAXI" anywhere. Before
# this fix this was reported as BILL MATCH REVIEW REQUIRED ("the matched
# bill's date could not be reliably extracted" + "insufficient to verify
# category consistency") despite the bill being an exact, unambiguous match.


_FLAGSHIP_CAB_BILL_TEXT = """Date: August 7, 2026
Tax invoice for your ride
Invoice number: IN19-g875aK-vxVD
To: Test Employee
Tax invoice issued by: Indsystems IT Private Limited
On behalf of: Transport Service Provider
Nature of service: Passengers Transport Services
Driver's Name: SUKANTA GHOSH
Car details: white Maruti Suzuki Dzire WB23E7998
Description
Amount
Ride fare (in INR)
Rs 520.00
CGST 2.5%
Rs 13.00
SGST/UTGST 2.5%
Rs 13.00
Ride fare (in INR): Rs 546.00
Whether tax is payable under RCM - No"""


def test_flagship_cab_bill_amount_is_exact_via_arithmetic():
    amt, confidence, warnings = bill_parser._find_amount(_FLAGSHIP_CAB_BILL_TEXT)
    assert amt == 546.0
    assert confidence == "MEDIUM"
    assert any("arithmetic" in w.lower() for w in warnings)


def test_flagship_cab_bill_date_is_extracted_from_textual_label():
    parsed = bill_parser.parse_bill_text(_FLAGSHIP_CAB_BILL_TEXT)
    assert parsed.bill_date is not None
    assert (parsed.bill_date.year, parsed.bill_date.month, parsed.bill_date.day) == (2026, 8, 7)


def test_flagship_cab_bill_category_guess_from_ride_fare_text_not_literal_cab_word():
    parsed = bill_parser.parse_bill_text(_FLAGSHIP_CAB_BILL_TEXT)
    assert parsed.category_guess == "Cab/Taxi"


def test_flagship_cab_bill_vendor_is_company_not_driver_name():
    parsed = bill_parser.parse_bill_text(_FLAGSHIP_CAB_BILL_TEXT)
    # Driver's Name/Car details are ride metadata, never mistaken for the vendor.
    assert parsed.vendor is not None
    assert "SUKANTA GHOSH" not in parsed.vendor
    assert "Indsystems" in parsed.vendor


def test_flagship_cab_bill_end_to_end_is_high_confidence_extracted():
    parsed = bill_parser.parse_bill_text(_FLAGSHIP_CAB_BILL_TEXT)
    assert parsed.amount == 546.0
    assert parsed.bill_date is not None
    assert parsed.status == "EXTRACTED"
    assert parsed.confidence == "HIGH"


def test_flagship_cab_bill_matches_expense_exactly():
    import datetime

    expense = {
        "claimed_amount": 546.0,
        "expense_date": datetime.date(2026, 8, 7),
        "category": "CAB CHARGES",
        "expense_details": "Rainy day. Urgent installation at TCGLS of LCMS-2050.",
    }
    parsed = bill_parser.parse_bill_text(_FLAGSHIP_CAB_BILL_TEXT)
    bill = {
        "id": 1,
        "amount": parsed.amount,
        "bill_date": parsed.bill_date,
        "vendor": parsed.vendor,
        "description": parsed.description,
        "category_guess": parsed.category_guess,
        "bill_number": parsed.bill_number,
        "page_number": 5,
    }
    outcome = bill_matcher.match_expense(expense, [bill])
    assert outcome.status == "MATCHED"
    assert outcome.confidence == "HIGH"


# ---------- Post-UAT correction 2: amount present-but-wrong is a real signal ----------


def test_amount_clearly_different_is_negative_signal_not_neutral():
    """A bill sharing only the expense's date/category (common when several
    same-category expenses fall on the same day) must not look "plausible"
    when its own amount is known and clearly wrong - otherwise two same-day
    expenses with the wrong-amount bills both look like ambiguous candidates
    for each other instead of neither being a real match."""
    import datetime

    expense = {"claimed_amount": 500.0, "expense_date": datetime.date(2026, 6, 1), "category": "BATTA", "expense_details": ""}
    bill = {
        "id": 1,
        "amount": 1975.85,
        "bill_date": datetime.date(2026, 6, 1),
        "vendor": None,
        "description": None,
        "category_guess": None,
        "bill_number": None,
        "page_number": 8,
    }
    score, reasons = bill_matcher.score_pair(expense, bill)
    assert any("negative signal" in r.lower() for r in reasons)
    assert score < bill_matcher.MIN_THRESHOLD


# ---------- Post-UAT correction 2, Part 5: category semantic matching ----------


@pytest.mark.parametrize(
    "text,expected_label",
    [
        ("Nature of service: Passengers Transport Services", "Cab/Taxi"),
        ("Your ride fare receipt", "Cab/Taxi"),
        ("Electronic Reservation Slip (ERS) IRCTC PNR 1234567890", "Train"),
    ],
)
def test_category_keywords_recognize_real_bill_wording(text, expected_label):
    label, _snippet = bill_parser._find_category(text)
    assert label == expected_label


# ---------- Post-UAT correction 2, Part 4: labelled amount collision ----------


def test_total_fare_label_wins_over_unrelated_total_tax_boilerplate():
    """A ticket's real total ("Total Fare (all inclusive)") must not collide
    with unrelated boilerplate elsewhere on the page that also contains the
    bare word "total" (e.g. a GST breakdown's "Total Tax:" line) - real
    example: an IRCTC ticket where this previously made the amount
    unextractable ("Multiple differing amounts found at the same label
    priority")."""
    text = (
        "Ticket Fare\n2,412.00\nCatering Charges (Incl. of GST)\n368.00\n"
        "IRCTC Convenience Fee (Incl. of GST)\n23.60\nTravel Insurance Premium (Incl. of GST)\n0.45\n"
        "Total Fare (all inclusive)\n2,804.05\nTotal Tax:\n114.85"
    )
    amt, confidence, warnings = bill_parser._find_amount(text)
    assert amt == 2804.05
    assert confidence == "HIGH"


# ---------- Post-UAT correction 2, Parts 15-17: catering ----------


def test_catering_amount_detected_skipping_column_header_occurrence():
    from app.services.category_map import detect_catering_amount

    text = "Passenger Details\n# Name Age Gender Catering Service Option\n1. TEST EMPLOYEE\nCatering Charges (Incl. of GST)\n368.00"
    assert detect_catering_amount(text) == 368.0


def test_catering_check_is_review_required_not_exception():
    from app.services.validation import checks

    class _Bill:
        page_number = 47
        raw_text = "Catering Charges (Incl. of GST)\n368.00"

    result = checks.check_catering(category="TRAIN / BUS FARE", bill=_Bill(), claimed_amount=1975.85)
    assert result.result == checks.REVIEW_REQUIRED
    # The auditor needs both figures, not just the catering amount in
    # isolation - see checks.py's check_catering.
    assert result.details == (
        "Total claimed amount: Rs.1,975.85. Catering amount: Rs.368.00. "
        "Catering/service component requires separate policy review."
    )
    assert result.claimed_amount == 1975.85
    assert result.bill_amount == 368.0


def test_catering_not_applicable_for_non_travel_category():
    from app.services.validation import checks

    class _Bill:
        page_number = 1
        raw_text = "Catering Charges 368.00"

    result = checks.check_catering(category="STAFF WELFARE", bill=_Bill())
    assert result.result == checks.NOT_APPLICABLE


def test_catering_excluded_amount_matches_claimed_within_tolerance():
    """Part 17: a travel bill's all-inclusive total can legitimately exceed
    the claimed amount by exactly the catering charge - that must not read
    as AMOUNT MISMATCH."""
    from decimal import Decimal

    from app.services.validation import checks

    class _Bill:
        amount = Decimal("2804.05")
        page_number = 47
        raw_text = "Catering Charges (Incl. of GST)\n368.00"

    result = checks.check_bill_amount(claimed_amount=2436.0, bill_match_status="MATCHED", bill=_Bill())
    assert result.result == checks.PASS


# ---------- Post-UAT correction 2, Part 18: BATTA never needs a bill ----------


def test_batta_expense_is_never_sent_to_bill_matching(client):
    """Uploads a synthetic ER with one BATTA row and one bill page, and
    confirms the BATTA row's bill_match_status is NOT_APPLICABLE - never
    NO_MATCH/MISSING BILL/DUPLICATE BILL, regardless of what's on the page."""
    pdf_bytes = _make_er_with_bill_pages([["Some unrelated receipt", "Rs.500.00"]])
    # _make_er_with_bill_pages always uses category "COURIER CHARGES" for its
    # one synthetic row - swap it for BATTA via a fresh, purpose-built PDF instead.
    import fitz
    from io import BytesIO

    doc = fitz.open()
    page = doc.new_page()
    header_lines = [
        "Expense Report", "ER-90002", "Amount to be Reimbursed", "Rs.500.00",
        "Report", "January", "Submitted By", "TEST EMPLOYEE", "test@spincotech.com",
        "Submitted On", "01/01/2026", "EXPENSE SUMMARY", "1.", "01/01/2026",
        "BATTA", "Daily allowance", "Rs.500.00",
        "REPORT SUMMARY BY CURRENCY", "Total Reimbursable Amount", "Rs.500.00",
        "TEST EMPLOYEE ( T001 )",
    ]
    y = 40
    for line in header_lines:
        page.insert_text((30, y), line, fontsize=9)
        y += 12
    bp = doc.new_page()
    bp.insert_text((30, 40), "Some unrelated receipt")
    bp.insert_text((30, 52), "Rs.500.00")
    buf = BytesIO()
    doc.save(buf)
    doc.close()

    result = _upload(client, "batta_no_bill.pdf", buf.getvalue()).json()["results"][0]
    report_id = result["document_id"].split(":")[1]
    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    batta = next(e for e in expenses if e["category"] == "BATTA")
    assert batta["bill_match_status"] == "NOT_APPLICABLE"
    assert batta["bill_found"] is False

    exceptions = client.get(f"/api/expense-reports/{report_id}/exceptions").json()
    batta_exceptions = [e for e in exceptions if e["expense_id"] == batta["id"]]
    assert all(e["exception_type"] not in ("MISSING BILL", "DUPLICATE BILL", "BILL MATCH REVIEW REQUIRED") for e in batta_exceptions)


# ---------- Post-UAT correction 2, Parts 13-14: duplicate bill original/duplicate ----------


def test_duplicate_bill_names_the_original_claim():
    from app.services.validation import checks

    class _Expense:
        s_no = 3
        claimed_amount = 546.0
        expense_date = __import__("datetime").date(2026, 8, 7)

    class _Bill:
        page_number = 5

    result = checks.check_duplicate_bill(
        bill_match_status="MATCHED", bill=_Bill(), is_duplicate=True, original_expense=_Expense()
    )
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "DUPLICATE BILL"
    assert "S.No 3" in result.details
    assert "546" in result.details


def test_original_claim_on_a_shared_bill_is_not_itself_flagged_duplicate():
    """The earlier (lowest s_no) expense referencing a re-used bill is the
    original and must pass this check cleanly - only the later claim(s) are
    the duplicate (Part 12: a legitimate single match is not anomalous)."""
    from app.services.validation import checks

    class _Bill:
        page_number = 5

    original_result = checks.check_duplicate_bill(bill_match_status="MATCHED", bill=_Bill(), is_duplicate=False)
    assert original_result.result == checks.PASS
