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
        result = c.post(
            "/api/uploads/expense-reports", files=[("files", ("no_bills.pdf", pdf_bytes, "application/pdf"))]
        ).json()["results"][0]
        assert result["success"]
        report_id = result["document_id"].split(":")[1]
        expenses = c.get(f"/api/expense-reports/{report_id}/expenses").json()
        assert expenses[0]["bill_match_status"] == "NO_MATCH"
        assert expenses[0]["bill_id"] is None if "bill_id" in expenses[0] else True


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
        # No status may be an invented business verdict - only the Phase 4 vocabulary.
        assert all(
            e["bill_match_status"] in ("MATCHED", "MULTIPLE_CANDIDATES", "NO_MATCH", "REVIEW_REQUIRED", "NOT_PROCESSED")
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
