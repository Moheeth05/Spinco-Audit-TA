from datetime import date
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


# ---------- 2-digit-year date formats (confirmed on real fuel-pump/thermal
# receipts and a Uber receipt in the reference corpus) ----------


def test_bill_date_extraction_supports_2digit_year_with_label():
    """IndianOil receipt format: 'Date : 07/07/26' - confirmed on two real
    reference bills (ER-40782 s_no=2 and s_no=5)."""
    from app.services.extraction.dates import parse_date

    assert parse_date("Date : \n07/07/26 \nTime: 20:50") == date(2026, 7, 7)
    assert parse_date("Date : \n13/07/26 \nTime: 11:46") == date(2026, 7, 13)


def test_bill_date_extraction_2digit_year_dd_mm_preferred_when_ambiguous():
    """DD/MM is this codebase's convention - preferred even when MM/DD would
    also be a technically-valid interpretation (03/07/26 stays 3 July, never
    silently reinterpreted as March 7)."""
    from app.services.extraction.dates import parse_date

    assert parse_date("Date: 03/07/26") == date(2026, 7, 3)


def test_bill_date_extraction_2digit_year_mm_dd_fallback_when_dd_mm_impossible():
    """A real Uber receipt (ER-41115 s_no=14) prints a bare '8/19/26' with no
    'Date:' label - day 19 can't be a month, so DD/MM is impossible and it
    can only be read as August 19."""
    from app.services.extraction.dates import parse_date

    assert parse_date("Payments Cash Rs.388.26 8/19/26 5:48 am") == date(2026, 8, 19)


def test_bill_date_extraction_2digit_year_rejects_genuinely_impossible_date():
    from app.services.extraction.dates import find_all_dates

    # Neither 32/13 nor 13/32 is a valid day/month pairing either way.
    assert find_all_dates("Date: 32/13/26") == []


def test_bill_date_extraction_2digit_year_does_not_confuse_other_numeric_strings():
    """A GSTIN/invoice/txn-id style numeric string must never be misread as
    a 2-digit-year date just because it contains slash/dash-separated
    digits in the wrong shape."""
    from app.services.extraction.dates import find_all_dates

    assert find_all_dates("Txn Id: 6062209712") == []
    assert find_all_dates("Receipt: AP31EA79/70") == []


def test_bill_date_short_year_echo_corroborates_rather_than_conflicts():
    """Real case (ER-40706 (1).pdf s_no=20, a Uber receipt): 'Jul 10, 2026'
    at the top, then later a bare payment-timestamp echo '7/10/26' next to
    the payment method. Read as DD/MM that echo would be 7 October - a
    false conflict with the already-confirmed 10 July. Since the MM/DD
    reading of the SAME token exactly corroborates the already-found date,
    it must resolve to one confirmed date, not two conflicting ones."""
    from app.services.extraction.dates import find_all_dates, parse_date

    text = "Jul 10, 2026\n7:27 PM\nThanks for riding\nCash\n7/10/26 8:50 P\nM\nUber Go AC"
    assert find_all_dates(text) == [date(2026, 7, 10), date(2026, 7, 10)]
    assert parse_date(text) == date(2026, 7, 10)


def test_bill_date_short_year_never_suppresses_a_genuine_conflict():
    """The corroboration rule must only ever resolve TOWARD an
    already-confirmed date - it must never paper over two actually
    different, unrelated dates just because one of them is in 2-digit-year
    slash form."""
    from app.services.extraction.dates import find_all_dates

    # "5 March 2026" and "9/4/26" (DD/MM -> 4 Sept; MM/DD -> 9 Apr) - neither
    # reading of the short-year date matches 5 March, so both must survive
    # as genuinely distinct dates.
    found = find_all_dates("Invoice dated 5 March 2026, also referencing 9/4/26 on the same page")
    assert date(2026, 3, 5) in found
    assert len(set(found)) == 2


def test_bill_date_recovered_from_transaction_timestamp_when_primary_date_is_ocr_garbled():
    """Real case (ER-39832, bill page 11): the labelled 'Date:' line is
    OCR-garbled ('22-()6- 2026'), but a clean backup timestamp survives
    elsewhere on the same receipt ('TxSt: 22-06-26 20:00:24') and is
    recovered via the 2-digit-year pattern + the existing 'txst' label."""
    text = (
        "Date: 22-()6- 2026 Time: 20:02:25 BayNo: 6 NozzleNo: | Product: DIESEL "
        "PayMode: Cash Txn Id: 6062209712 Attendant: TxSt: 22-06-26 20:00:24 TxEnd: 22-06-26 20:01:44"
    )
    parsed = bill_parser.parse_bill_text(text)
    assert parsed.bill_date == date(2026, 6, 22)


# ---------- NOZZLE/NOZL category evidence (confirmed on every real
# fuel-pump receipt in the reference corpus - a reliable second signal when
# PETROL/DIESEL/FUEL itself is OCR-garbled) ----------


def test_category_nozzle_keyword_detects_fuel_when_fuel_word_is_ocr_garbled():
    """Real case (ER-40706 s_no=29): 'Fue]' (garbled 'Fuel') never matches,
    but 'Nozl No' (garbled 'Nozzle') does."""
    text = "SVFES HP DEALER OUTLET Nozl No Fue] Density: NON Rs. 116.50 Rs. 3612.66"
    cat_guess, _ = bill_parser._find_category(text)
    assert cat_guess == "Fuel"


def test_category_nozzle_keyword_clean_spelling():
    cat_guess, _ = bill_parser._find_category("Nozzle No. :01\nProduct : Diesel")
    assert cat_guess == "Fuel"


# ---------- Position-aware / line-structure extraction (generalized - not
# tied to any specific document). Real patterns confirmed in the reference
# corpus: a native-PDF table whose label/value columns are emitted in
# separated reading order by PyMuPDF's linear get_text(), and an OCR'd
# receipt whose label and value survive individually but lose all line
# structure once flattened into one space-joined string. ----------


def test_ocr_engine_preserves_line_structure_not_flat_string():
    """TesseractOCREngine must group words by Tesseract's own
    (block_num, par_num, line_num) layout fields into real lines, not flatten
    the whole page into one space-joined string - this is what makes an
    existing, correctly-recognized 'Amount(Rs) : 03908.04' pair findable by
    the ordinary same-line label search."""
    import sys
    from io import BytesIO

    from PIL import Image

    from app.services.extraction.ocr_engine import TesseractOCREngine

    class _FakePytesseract:
        Output = type("Output", (), {"DICT": "dict"})

        @staticmethod
        def image_to_data(image, output_type):
            # Two logical lines: "Nozzle No. : 03" and "Amount(Rs) : 03908.04"
            n = 7
            return {
                "text": ["Nozzle", "No.", ":", "03", "Amount(Rs)", ":", "03908.04"],
                "conf": ["95", "95", "95", "95", "90", "90", "90"],
                "block_num": [1, 1, 1, 1, 1, 1, 1],
                "par_num": [1, 1, 1, 1, 1, 1, 1],
                "line_num": [1, 1, 1, 1, 2, 2, 2],
                "left": [0] * n,
                "top": [0] * n,
                "width": [10] * n,
                "height": [10] * n,
            }

        @staticmethod
        def get_tesseract_version():
            return "5.0"

    real_png = BytesIO()
    Image.new("RGB", (10, 10), "white").save(real_png, format="PNG")

    engine = TesseractOCREngine()
    engine._available = True
    sys.modules["pytesseract"] = _FakePytesseract()
    try:
        result = engine.extract_text(real_png.getvalue())
    finally:
        del sys.modules["pytesseract"]

    assert result["text"] == "Nozzle No. : 03\nAmount(Rs) : 03908.04"
    assert len(result["words"]) == 7
    assert result["words"][4]["text"] == "Amount(Rs)"
    assert result["words"][4]["line_num"] == 2


def test_ocr_line_preserved_amount_label_with_parenthetical():
    """Real corpus pattern (ER-40706): 'Amount(Rs) : 03908.04' - the label
    itself is correctly OCR'd, but was previously unreachable once the whole
    page collapsed into one line with no line breaks at all."""
    text = "Rate(Rs/L) : 116.38\nVolume(L) : 00033.58\nAmount(Rs) : 03908.04\nDate : 14/07/26"
    parsed = bill_parser.parse_bill_text(text)
    assert parsed.amount == 3908.04


def test_native_position_aware_fallback_recovers_separated_label_value():
    """Generalized reconstruction: PyMuPDF's get_text('dict') groups spans
    into visual rows by Y-coordinate, recovering a label/value pair that
    linear get_text() separated - confirmed on a real IRCTC ticket's
    Payment Details table. Not testable without a real PDF page object here,
    so this directly tests the reconstruction's underlying text-detection
    heuristic and the pairing logic it feeds."""
    from app.services.extraction.text_extraction import _text_has_labelled_values

    # Values present but their labels were stripped by linear extraction -
    # must be detected as NOT already having labelled values (triggers the
    # position-aware fallback).
    stripped = "₹ 1,766.00\n₹ 174.00\n₹ 35.40\nPG Charges as applicable"
    assert _text_has_labelled_values(stripped) is False

    # Once reconstructed with correct row grouping, label and value share a line.
    reconstructed = "Ticket Fare ₹ 1,766.00\nCatering Charges (Incl. of GST) ₹ 174.00"
    assert _text_has_labelled_values(reconstructed) is True


def test_position_aware_trigger_ignores_boilerplate_prose():
    """Real false-positive this exact heuristic had to be fixed for: long
    terms-and-conditions prose that merely CONTAINS a label word near a
    currency-shaped number ('A clerkage charge of Rs.60 per passenger...')
    must not be mistaken for a genuine short label/value pair - a form-field
    label line is short; a sentence is not."""
    from app.services.extraction.text_extraction import _text_has_labelled_values

    boilerplate = (
        "A clerkage charge of Rs.60 per passenger plus GST for AC Classes and Rs.60 per "
        "passenger for Non AC classes will be deducted if the ticket remains Waitlisted."
    )
    assert _text_has_labelled_values(boilerplate) is False


def test_label_value_lookahead_stops_at_next_label_not_its_value():
    """Negative test: a wider lookahead for a label's value must never
    overshoot into a DIFFERENT field's own value just because the real
    label's own line and the immediate next line were both empty of a
    number."""
    text = "Total\n\nDate: 07/07/26"
    amt, _, _ = bill_parser._find_amount(text)
    assert amt != 7.0  # must never read "07" out of the date as the total
    assert amt is None  # no genuine amount value was ever provided


def test_label_value_lookahead_recovers_value_two_lines_below():
    text = "Grand Total\n\nRs. 2,500.00"
    amt, _, _ = bill_parser._find_amount(text)
    assert amt == 2500.0


def test_fuzzy_amount_label_recovers_single_character_drop():
    """Real corpus pattern (ER-41115 page 49): 'Amount' OCR'd as 'mount'
    (the leading 'A' misattributed to the next word) - a confirmed,
    narrowly-scoped OCR corruption, not a layout/position issue."""
    text = "Nozzle No. : 03\nmount : 546.00\nDate : 09/08/26"
    parsed = bill_parser.parse_bill_text(text)
    assert parsed.amount == 546.0


def test_fuzzy_amount_label_does_not_fire_when_exact_label_already_found():
    """The fuzzy fallback must never override a real, correctly-labelled
    amount found elsewhere on the same bill."""
    text = "Total Rs. 500.00\nSome unrelated tonal description nearby 999.00"
    amt, _, _ = bill_parser._find_amount(text)
    assert amt == 500.0


def test_fuzzy_amount_label_requires_strict_value_not_loose_number():
    """Negative test: a fuzzy-matched label must not grab an unrelated bare
    number via the loose-number fallback - only a properly currency/decimal
    -shaped value is accepted for a fuzzy label match."""
    from app.services.extraction.bill_parser import _find_fuzzy_labelled_amount

    assert _find_fuzzy_labelled_amount("tonal reference 12345 unrelated") is None


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


# ---------- Whole-rupee rounding: BOTH neighbouring integers of a decimal
# bill amount are valid claimed amounts (never ordinary single-direction
# rounding, never a looser percentage-based tolerance) ----------


def _amount_bill(amount):
    return {"id": 1, "amount": amount, "bill_date": None, "vendor": None, "description": None, "category_guess": None, "bill_number": None}


def _amount_expense(claimed):
    return {"claimed_amount": claimed, "expense_date": None, "category": None, "expense_details": None}


@pytest.mark.parametrize(
    "bill_amount,claimed,expect_match",
    [
        (4499.77, 4499, True),   # floor - VALID
        (4499.77, 4500, True),   # ceil - VALID
        (4499.77, 4498, False),  # one below floor - INVALID
        (4499.77, 4501, False),  # one above ceil - INVALID
        (123.67, 123, True),
        (123.67, 124, True),
        (123.67, 122, False),
        (123.67, 125, False),
        (500.01, 500, True),
        (500.01, 501, True),
        (500.01, 499, False),
        (500.01, 502, False),
        (4500.00, 4500, True),   # whole-rupee bill - only the exact value is valid
        (4500.00, 4499, False),  # other whole-rupee amounts remain mismatches
        (4500.00, 4501, False),
    ],
)
def test_matcher_whole_rupee_rounding_rule(bill_amount, claimed, expect_match):
    score, reasons = bill_matcher.score_pair(_amount_expense(claimed), _amount_bill(bill_amount))
    if expect_match:
        assert score >= 40, reasons
        assert any("amount" in r.lower() and ("exact" in r.lower() or "nearest rupee" in r.lower()) for r in reasons)
    else:
        assert score <= -25, reasons
        assert any("clearly different" in r.lower() for r in reasons)


def test_check_bill_amount_accepts_whole_rupee_rounding():
    from decimal import Decimal

    from app.services.validation import checks

    class _Bill:
        amount = Decimal("4499.77")
        page_number = 5
        raw_text = "Total 4499.77"

    floor_result = checks.check_bill_amount(claimed_amount=4499, bill=_Bill())
    assert floor_result.result == checks.PASS
    ceil_result = checks.check_bill_amount(claimed_amount=4500, bill=_Bill())
    assert ceil_result.result == checks.PASS


def test_check_bill_amount_rejects_amounts_beyond_whole_rupee_rounding():
    from decimal import Decimal

    from app.services.validation import checks

    class _Bill:
        amount = Decimal("4499.77")
        page_number = 5
        raw_text = "Total 4499.77"

    too_low = checks.check_bill_amount(claimed_amount=4498, bill=_Bill())
    assert too_low.result == checks.EXCEPTION
    assert too_low.exception_type == "AMOUNT MISMATCH"
    too_high = checks.check_bill_amount(claimed_amount=4501, bill=_Bill())
    assert too_high.result == checks.EXCEPTION


def test_check_bill_amount_whole_rupee_bill_only_accepts_exact():
    from decimal import Decimal

    from app.services.validation import checks

    class _Bill:
        amount = Decimal("4500.00")
        page_number = 5
        raw_text = "Total 4500.00"

    exact = checks.check_bill_amount(claimed_amount=4500, bill=_Bill())
    assert exact.result == checks.PASS
    off_by_one = checks.check_bill_amount(claimed_amount=4501, bill=_Bill())
    assert off_by_one.result == checks.EXCEPTION


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


def test_catering_short_reason_includes_amount_and_total():
    """The displayed reason (remarks - exception_engine.py's _reason_text()
    prefers remarks over details) must be short and name the catering
    charge directly, not a long paragraph."""
    from app.services.validation import checks

    class _Bill:
        page_number = 47
        raw_text = "Catering Charges (Incl. of GST)\n368.00"

    result = checks.check_catering(category="TRAIN / BUS FARE", bill=_Bill(), claimed_amount=1975.85)
    assert result.remarks == "Catering included: Rs.368.00 of Rs.1,975.85 total. Requires policy review."
    assert len(result.remarks) < 100


def test_catering_short_reason_without_claimed_amount():
    from app.services.validation import checks

    class _Bill:
        page_number = 47
        raw_text = "Catering Charges (Incl. of GST)\n368.00"

    result = checks.check_catering(category="TRAIN / BUS FARE", bill=_Bill(), claimed_amount=None)
    assert result.remarks == "Catering included: Rs.368.00. Requires policy review."


def test_catering_short_reason_when_amount_undetectable():
    from app.services.validation import checks

    class _Bill:
        page_number = 1
        raw_text = "Catering charges included in the fare"  # mentioned, no parseable figure

    result = checks.check_catering(category="TRAIN / BUS FARE", bill=_Bill(), claimed_amount=500.0)
    assert result.result == checks.REVIEW_REQUIRED
    assert result.remarks == "Catering charge mentioned; amount could not be determined. Requires policy review."


def test_catering_classifier_derived_amount_in_short_reason():
    """catering_amount sourced from document_classifier.py's fallback (no
    regex-detectable line on the bill's own text) must still appear in the
    short reason, same as a regex-detected one."""
    from types import SimpleNamespace

    from app.services.validation import checks

    fake_bill = SimpleNamespace(
        raw_text="Transaction confirmed. Reference TXN123456. Total Rs. 2804.05.",
        page_number=3,
        catering_amount=368.00,
    )
    result = checks.check_catering(category="TRAIN / BUS FARE", bill=fake_bill, claimed_amount=2804.05)
    assert result.remarks == "Catering included: Rs.368.00 of Rs.2,804.05 total. Requires policy review."


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

    result = checks.check_bill_amount(claimed_amount=2436.0, bill=_Bill())
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


# ---------- Real-world document extraction/classification regression corpus ----------
#
# Excerpts below are taken verbatim (OCR artifacts and all) from real pages
# of the reference corpus (c967267ef0f4_ER-41115.pdf, backed up under
# backups/dipak-cleanup-20260924-170758/) - never a synthetic stand-in for
# a real-world failure mode. None of these hardcode a filename/page number
# into production logic; the production code under test has no idea these
# strings came from that specific document.

# Page 25: a real Cleartrip/Air India Express flight ticket. Its own fare
# breakdown includes a line item literally named "Airline Fuel Charge" -
# confirmed real false positive for the old bare "FUEL" keyword match.
_REAL_FLIGHT_TICKET_TEXT = """25
Trip ID :  260818517608
Kolkata to Bagdogra  Wed, 19 Aug 2026
Air India Express CCU  07:45 09:05  IXB
IX - 1248  Wed, 19 Aug 2026 1h 20min Wed, 19 Aug 2026
Fare type: CLASSIC Kolkata - Netaji Subhas Chandra Economy Bagdogra - Bagdogra
Bose Airport Terminal 2
Baggage (per Adult/Child) - Check-in: 15KG (1 piece), Cabin: 7KG (1 piece)
Seats - 13A
TRAVELLERS AIRLINE PNR TICKET NO.
Mr Dipak Kumar Pal
T5G2KC T5G2KC
FARE BREAKUP
Base Fare: Rs. 3,333
Airline Fuel Charge: Rs. 299
Cute Fee: Rs. 45
Other charges: Rs. 1,545
Total Fare: Rs. 4,797"""

# Page 27: the matching boarding pass for the same flight - no amount at
# all, previously category_guess=None (no keyword in the list matched a
# boarding pass), now Flight via the named airline.
_REAL_BOARDING_PASS_TEXT = """r AIR INDIA
express airindiaexpress.com
DIPAK KUMAR PAL
IX1248 CCU TO IXB 19 Aug 26
GATE E BOARDING DEPARTURE SEAT PNR
07a 2 0705 0745 13A VA
GATE CLOSES 25 MINUTES BEFORE DEPARTURE TIME"""

# Page 44: a real hotel tax invoice (Palm Grove Suites Inn & Spa) whose own
# name never contains "hotel", and whose printed address happens to read
# "...BESIDE KESAR KUNTI FUEL STATION" (a nearby landmark) - confirmed real
# false positive for the old bare "FUEL" keyword, and confirmed real gap in
# the old Lodging keyword list (no "HOTEL"/"LODGING"/"ROOM CHARGE"/
# "ACCOMMODATION" anywhere on this genuine hotel bill).
_REAL_HOTEL_BILL_FUEL_LANDMARK_TEXT = """GUEST NAME
ADDRESS
MOBILE NO
NATIONALITY
GSTIN
DATE
19/08/2026
20/08/2026
ROOM TARIFF
PALM GROVE SUITES INN & SPA
BESIDE KESAR KUNTI FUEL STATION
TAX INVOICE : B2627-00406
BILL DATE : 21/08/2026
ROOMNO : 102
PARTICULARS SAC
Room Tariff 9963"""

# Page 24: a real Uber "not a tax invoice" cash-trip receipt - amount and
# timestamp both already extract correctly today; locked in here as an
# explicit regression test so a future change can't silently break it.
_REAL_UBER_SCREENSHOT_TEXT = """Total =388.26
Congratulations! You unlocked a discount on your ride
Booking fee @ 44.00
Suggested fare 352.18
Promotion -7.92
Payments
Cash 388.26
8/19/26 5:48 am
This receipt reflects the suggested fare (excluding GST) and is not a tax invoice but it can be used for
official reimbursement purposes. No GST is being recovered by Uber from the riders on this trip.
Uber does not provide transport services; Issued on behalf of SADIQUE SHAIKH"""

# Page 28: a real handwritten Tamang Taxi Service receipt. The printed
# letterhead OCRs cleanly; the handwritten amount (boxed "Rs. 4000/-") and
# handwritten date do not, under Tesseract at any tested PSM mode - a
# genuine, confirmed limitation (see PHASE4 investigation), not something
# this suite claims to have solved.
_REAL_HANDWRITTEN_TAXI_RECEIPT_TEXT = """TAMANG TAXI SERVICE
RANGPO, EAST SIKKIM
Mob.:
SLN0. CERNO- SkKBCZa2
Name
From 2PO2OGEK...AIRPORTIo.WMA TS! f
the sum of Rupees AEA th. 8.9 0..Cataag emis See
Signature
28"""

# A real fuel-pump receipt's own printed date field, confirmed on this
# corpus in ISO 8601 (YYYY-MM-DD), a format dates.py did not recognize at
# all before this round.
_REAL_FUEL_RECEIPT_ISO_DATE_TEXT = """WEH TYPE: Petrol
MEH NO:
CUSTOMER NAME:
DATE: 2026-09-23
MODE: Cash
LST NO:
MAT NO:
ATTENDANT ID: not available
Thank You! Visit Again
Save Fuel, Save Money."""


def test_real_flight_ticket_classified_as_flight_not_fuel():
    parsed = bill_parser.parse_bill_text(_REAL_FLIGHT_TICKET_TEXT)
    assert parsed.category_guess == "Flight"


def test_real_flight_ticket_amount_and_date_still_extract_correctly():
    """The category fix must not disturb the amount/date extraction that
    already worked correctly on this real document."""
    parsed = bill_parser.parse_bill_text(_REAL_FLIGHT_TICKET_TEXT)
    assert parsed.amount == 4797.0
    assert parsed.bill_date is not None
    assert (parsed.bill_date.year, parsed.bill_date.month, parsed.bill_date.day) == (2026, 8, 19)


def test_real_boarding_pass_classified_as_flight():
    """Previously category_guess=None entirely - no keyword in the old list
    matched a boarding pass at all."""
    parsed = bill_parser.parse_bill_text(_REAL_BOARDING_PASS_TEXT)
    assert parsed.category_guess == "Flight"


def test_real_hotel_bill_classified_as_lodging_not_fuel():
    """Confirmed real false positive: the old bare FUEL keyword matched this
    hotel bill's own address line ("...FUEL STATION" as a landmark), and the
    old Lodging keyword list had no term matching this bill's actual
    wording (no "hotel"/"lodging"/"room charge"/"accommodation" anywhere on
    it) - it used to resolve to category_guess=None."""
    parsed = bill_parser.parse_bill_text(_REAL_HOTEL_BILL_FUEL_LANDMARK_TEXT)
    assert parsed.category_guess == "Lodging"


def test_genuine_fuel_pump_receipts_still_classify_as_fuel():
    """Regression guard: narrowing the bare FUEL keyword must not weaken
    detection of an actual fuel-pump receipt - NOZZLE/PETROL/DIESEL remain
    unconditionally strong signals, and a bare FUEL not adjacent to the two
    confirmed false-positive contexts (preceded by "Airline ", followed by
    " Station") still matches."""
    assert bill_parser.parse_bill_text("Bharat Petroleum\nProduct : Petrol\nNozzle No. : 03").category_guess == "Fuel"
    assert bill_parser.parse_bill_text("ABC Fuels\nFUEL RECEIPT\nAmount: Rs.500.00").category_guess == "Fuel"


def test_uber_cash_screenshot_amount_and_timestamp_extract_correctly():
    """Locks in already-correct real-corpus behavior (post-UAT: the 'not a
    tax invoice' disclaimer text must never block recognizing this as real
    supporting evidence)."""
    parsed = bill_parser.parse_bill_text(_REAL_UBER_SCREENSHOT_TEXT)
    assert parsed.amount == 388.26
    assert parsed.bill_date is not None
    assert (parsed.bill_date.year, parsed.bill_date.month, parsed.bill_date.day) == (2026, 8, 19)
    assert parsed.category_guess == "Cab/Taxi"


def test_handwritten_taxi_receipt_vendor_and_category_recognized():
    """What Tesseract CAN read on this real handwritten receipt - the
    printed letterhead - is correctly recognized."""
    parsed = bill_parser.parse_bill_text(_REAL_HANDWRITTEN_TAXI_RECEIPT_TEXT)
    assert parsed.category_guess == "Cab/Taxi"
    assert parsed.vendor is not None
    assert "TAMANG" in parsed.vendor.upper()


def test_handwritten_taxi_receipt_amount_and_date_never_fabricated():
    """Confirmed real OCR limitation: the handwritten amount/date on this
    receipt do not survive Tesseract at any tested PSM mode. The system
    must preserve that uncertainty - never invent Rs.4,000 or a date just
    because the document type is otherwise recognized.

    None/None/REVIEW_REQUIRED are also ParsedBill's dataclass defaults, so
    asserting only those would pass even against a no-op parser - the
    category_guess assertion proves real parsing of this exact text ran
    (it is derived from the "TAXI SERVICE" text, never a default)."""
    parsed = bill_parser.parse_bill_text(_REAL_HANDWRITTEN_TAXI_RECEIPT_TEXT)
    assert parsed.category_guess == "Cab/Taxi"
    assert parsed.amount is None
    assert parsed.bill_date is None
    assert parsed.status == "REVIEW_REQUIRED"


def test_iso_date_format_recognized():
    """Real corpus gap: dates.py had no pattern at all for YYYY-MM-DD."""
    from app.services.extraction.dates import find_all_dates

    dates = find_all_dates(_REAL_FUEL_RECEIPT_ISO_DATE_TEXT)
    assert date(2026, 9, 23) in dates


def test_iso_date_never_confused_with_ddmmyyyy():
    """Regression guard: adding YYYY-MM-DD must never change how an
    existing DD/MM/YYYY or DD-MM-YYYY date is read."""
    from app.services.extraction.dates import find_all_dates

    assert find_all_dates("Date: 23-09-2026") == [date(2026, 9, 23)]
    assert find_all_dates("Date: 23/09/2026") == [date(2026, 9, 23)]


def test_airfare_category_maps_to_flight_evidence():
    """Real gap: the AIRFARE policy category had no bill-evidence mapping at
    all - an airfare claim could never reach Category Consistency PASS,
    only REVIEW_REQUIRED, regardless of how good the matched bill's
    evidence was."""
    from app.services.category_map import expected_bill_label

    assert expected_bill_label("AIRFARE") == "Flight"


def test_real_flight_ticket_wrongly_filed_as_lodging_is_category_exception(client):
    """End-to-end, real-corpus-grounded: this exact flight ticket is the
    real candidate bill for a real expense the employee mis-filed under
    LODGING (own expense description literally says "Air ticket from CCU to
    Bagdogra") - amount and date both match the claim exactly, but the
    actual service (Flight) directly contradicts the claimed category
    (LODGING). Must remain a confirmed CATEGORY MISMATCH, never silently
    verified just because amount+date line up."""
    from app.services.validation import checks

    class _Bill:
        page_number = 25
        category_guess = "Flight"

    result = checks.check_category_consistency(category="LODGING", bill=_Bill())
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "CATEGORY MISMATCH"


def test_airfare_claim_with_matching_flight_evidence_passes_category_check():
    """The positive counterpart: a correctly-filed AIRFARE claim whose
    matched bill shows real Flight evidence must now cleanly PASS - this
    was previously impossible (REVIEW_REQUIRED at best) since AIRFARE had
    no expected-evidence mapping at all."""
    from app.services.validation import checks

    class _Bill:
        page_number = 25
        category_guess = "Flight"

    result = checks.check_category_consistency(category="AIRFARE", bill=_Bill())
    assert result.result == checks.PASS
