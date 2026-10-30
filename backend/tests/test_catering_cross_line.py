"""The catering detector must not capture an amount that belongs to a DIFFERENT row.

`detect_catering_amount` used to look 40 characters ahead, across line breaks, so
a printed "CATERING SERVICE" row header followed by "ROOM RENT 1607.00" produced a
catering charge of 1607.00 (and "Catering Charges RS26/3238" produced 26.00). The
amount is now taken only from the same line, or from the next line when that line
is nothing but an amount (optionally after blank / "(Incl. of GST)"-style qualifier
lines) and is not the head of a column of amounts.

All text below is SYNTHETIC; no real ticket, folio or receipt data.
"""

import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.validation import ExceptionRecord
from app.services import category_map
from app.services.exception_engine import evaluate_report
from app.services.extraction import bill_matcher, bill_parser
from app.services.validation import checks
from app.services.validation.pipeline import validate_expense_report

RAW_TEXT_CAP = 2000
detect = category_map.detect_catering_amount

# ---------- valid layouts: all must keep working exactly as before ----------

VALID_LAYOUTS = [
    ("Catering Charges (Incl. of GST): Rs. 368.00", 368.0),
    ("Catering Charges 368.00", 368.0),
    ("Catering charges Rs.150.00 included", 150.0),
    ("Catering Charges (Incl. of GST) ₹ 174.00", 174.0),
    ("Ticket Fare ₹ 1,766.00\nCatering Charges (Incl. of GST) ₹ 174.00", 174.0),
    ("Catering Charges (Incl. of GST)\n368.00", 368.0),
    ("Ticket Fare\n2,412.00\nCatering Charges (Incl. of GST)\n368.00\n", 368.0),
    ("Catering Charges (Incl. of GST)\nRs. 368.00", 368.0),
    ("Catering Charges\n₹ 368.00", 368.0),
    ("Catering Charges\n\n368.00", 368.0),  # a blank line between label and amount
    ("Catering Charges\n(Incl. of GST)\n368.00", 368.0),  # a qualifier-only line between them
    ("Passenger Details\n# Name Age Gender Catering Service Option\n1. TEST EMPLOYEE\nCatering Charges (Incl. of GST)\n368.00", 368.0),
    ("Ticket Fare\n2,412.00\nCatering Charges (Incl. of GST)\n368.00\nTotal Fare (all inclusive)\n2,780.00", 368.0),
    ("Fare Rs.500.00\nCatering charges Rs.150.00 included", 150.0),
]


@pytest.mark.parametrize("text, expected", VALID_LAYOUTS)
def test_valid_catering_layouts_still_give_their_amount(text, expected):
    assert detect(text) == expected
    assert category_map.mentions_catering(text)


# ---------- the defect: an unrelated amount must never be captured ----------

NOT_CATERING_AMOUNT = [
    ("catering header then room rent", "HOTEL MONSOON\nCATERING SERVICE\nROOM RENT 1607.00\nFOODING 508.00\nTOTAL 2308.00"),
    ("catering header then tariff", "CATERING SERVICE\nTARIFF 5,777.00"),
    ("label then a different labelled row", "Catering Charges\nConvenience Fee (Incl. of GST) 11.80"),
    ("label then another label then amounts", "Ticket Fare\nCatering Charges\nTotal Fare\n2,412.00\n368.00\n2,780.00"),
    ("label list followed by a column of amounts", "ROOM RENT\nFOODING\nCATERING SERVICE\n1607.00\n508.00\n300.00"),
    ("label, qualifier, then another label", "Catering Charges\n(Incl. of GST)\nRoom Rent 1607.00"),
    ("voucher reference, same line", "Catering Charges RS26/3238"),
    ("voucher reference with a date", "Catering RS26/3238 24/09/2026"),
    ("voucher reference, mixed case", "Catering Charges Rs26/3238"),
    ("order number", "Catering Order No 4417"),
]


@pytest.mark.parametrize("name, text", NOT_CATERING_AMOUNT, ids=[n for n, _ in NOT_CATERING_AMOUNT])
def test_an_unrelated_amount_is_never_taken_as_catering(name, text):
    assert detect(text) is None
    assert category_map.mentions_catering(text)  # the mention is kept: it still needs review


def test_a_real_catering_line_after_a_misleading_header_is_still_found():
    text = "CATERING SERVICE\nROOM RENT 1607.00\nCatering Charges (Incl. of GST)\n368.00"
    assert detect(text) == 368.0


def test_the_same_line_window_never_crosses_a_line_break():
    # 40 chars after "Catering" used to reach the next line's amount even when the label line was full of other text.
    assert detect("Catering Service Option Name\nFare 2,412.00") is None


# ---------- mention without a readable amount: review, but never an amount or an exclusion ----------


def _bill(raw_text, amount, **stored):
    return SimpleNamespace(page_number=4, page_number_end=None, amount=amount, bill_date=date(2026, 9, 24), category_guess="Train", raw_text=raw_text, **stored)


def test_a_header_only_mention_is_reviewed_without_an_amount():
    bill = _bill("IRCTC e-ticket\nCATERING SERVICE\nROOM RENT 1607.00\nTotal Fare 2,308.00", 2308.0)
    review = checks.check_catering(category="TRAIN / BUS FARE", bill=bill, claimed_amount=701.0)
    assert review.result == checks.REVIEW_REQUIRED
    assert review.remarks == "Catering charge mentioned; amount could not be determined. Requires policy review."
    assert review.bill_amount is None


def test_no_catering_exclusion_is_granted_from_an_unrelated_amount():
    bill = _bill("CATERING SERVICE\nROOM RENT 1607.00\nTotal 2,308.00", 2308.0)
    # 2,308 - 1,607 = 701: the claim must NOT be rescued by treating room rent as catering.
    for category in ("TRAIN / BUS FARE", "LODGING", None):
        result = checks.check_bill_amount(claimed_amount=701.0, bill=bill, category=category)
        assert result.result == checks.EXCEPTION and result.exception_type == "AMOUNT MISMATCH", category


def test_the_matcher_grants_no_amount_credit_from_an_unrelated_amount():
    bill = {"id": 1, "amount": 2308.0, "bill_date": None, "vendor": None, "description": None, "category_guess": None, "bill_number": None,
            "raw_text": "CATERING SERVICE\nROOM RENT 1607.00\nTotal 2,308.00"}
    expense = {"claimed_amount": 701.0, "expense_date": date(2026, 9, 24), "category": "TRAIN / BUS FARE", "expense_details": None}
    score, reasons = bill_matcher.score_pair(expense, bill)
    assert any("negative signal" in r for r in reasons) and score < 0
    assert not bill_matcher._amount_supported(expense, bill)


def test_a_genuine_catering_amount_still_earns_the_exclusion():
    bill = _bill("Ticket Fare\n2,000.00\nCatering Charges (Incl. of GST)\n368.00\nTotal Fare\n2,368.00", 2368.0)
    assert checks.check_bill_amount(claimed_amount=2000.0, bill=bill, category="TRAIN / BUS FARE").result == checks.PASS
    assert checks.check_catering(category="TRAIN / BUS FARE", bill=bill, claimed_amount=2000.0).result == checks.REVIEW_REQUIRED


# ---------- precedence is unchanged ----------


def test_below_the_cap_the_raw_text_reading_still_beats_a_stored_amount():
    regex_wins = _bill("Catering charges Rs.150.00", 500.0, catering_amount=999.0)
    assert "Rs.150.00" in checks.check_catering(category="TRAIN / BUS FARE", bill=regex_wins).remarks


def test_the_classifier_amount_is_still_the_fallback_when_raw_text_gives_none():
    stored = _bill("CATERING SERVICE\nROOM RENT 1607.00", 2308.0, catering_amount=250.0)
    assert "Rs.250.00" in checks.check_catering(category="TRAIN / BUS FARE", bill=stored).remarks  # not Rs.1,607.00


def test_stored_full_text_amount_still_wins_once_raw_text_is_capped():
    capped = ("Ticket fare 2,000.00\n" + "x" * RAW_TEXT_CAP)[: RAW_TEXT_CAP - len("\nCatering Charges Rs.3")] + "\nCatering Charges Rs.3"
    assert len(capped) == RAW_TEXT_CAP
    bill = _bill(capped, 2368.0, catering_amount=368.0)
    assert "Rs.368.00" in checks.check_catering(category="TRAIN / BUS FARE", bill=bill, claimed_amount=2000.0).remarks


# ---------- beyond the 2,000-character cap ----------

_PAD_LINE = "Hotel policy note: checkout time is twelve noon and late checkout is subject to availability."


def _long(before_cap: list[str], after_cap: list[str], total: str) -> str:
    padding: list[str] = []
    while sum(len(x) + 1 for x in padding) < 2300:
        padding.append(_PAD_LINE)
    text = "\n".join(["IRCTC e-ticket", "Date: 24/09/2026", "Ticket fare 2,000.00", *before_cap, *padding, *after_cap, total])
    assert text.rindex(after_cap[0]) > RAW_TEXT_CAP, "fixture must put these lines beyond the raw_text cap"
    return text


def test_a_separate_line_catering_charge_beyond_the_cap_is_stored():
    parsed = bill_parser.parse_bill_text(_long([], ["Catering Charges (Incl. of GST)", "368.00"], "Total Fare 2,368.00"))
    assert len(parsed.raw_text) == RAW_TEXT_CAP and "Catering" not in parsed.raw_text
    assert parsed.catering_amount == 368.0 and parsed.catering_mentioned is True


def test_a_misleading_header_beyond_the_cap_stores_a_mention_and_no_amount():
    parsed = bill_parser.parse_bill_text(_long([], ["CATERING SERVICE", "ROOM RENT 1607.00"], "Total 2,368.00"))
    assert parsed.catering_amount is None and parsed.catering_mentioned is True


# ---------- existing lodging behaviour is unchanged ----------

MONSOON = "HOTEL MONSOON\nBill No 0936\nCATERING SERVICE\nROOM RENT 1607.00\nFOODING 508.00\nCGST 96.50\nSGST 96.50\nTOTAL 2308.00"


def test_the_monsoon_style_lodging_bill_keeps_its_food_exclusion_and_loses_the_bogus_catering_amount():
    parsed = bill_parser.parse_bill_text(MONSOON)
    assert parsed.catering_amount is None and parsed.catering_mentioned is True
    assert parsed.room_service_food_amount == 508.0 and parsed.room_service_food_mentioned is True
    bill = _bill(MONSOON, 2308.0, catering_amount=parsed.catering_amount, catering_mentioned=parsed.catering_mentioned,
                 room_service_food_amount=parsed.room_service_food_amount, room_service_food_mentioned=parsed.room_service_food_mentioned)
    assert checks.check_bill_amount(claimed_amount=1800.0, bill=bill, category="LODGING").result == checks.PASS  # 2,308 - 508
    assert checks.check_bill_amount(claimed_amount=701.0, bill=bill, category="LODGING").result == checks.EXCEPTION  # 2,308 - 1,607 no longer passes
    assert checks.check_room_service_food(category="LODGING", bill=bill, claimed_amount=1800.0).result == checks.REVIEW_REQUIRED


def test_multi_charge_room_service_aggregation_is_untouched():
    text = "ROOM SERVICE RS26/3238 22/09/2026 357.00\nROOM SERVICE RS26/3301 23/09/2026 200.00\nTotal Restaurant Charges 557.00"
    assert category_map.analyze_room_service_food(text).amount == 557.0


# ---------- end to end through bill_pipeline ----------


def _run_pipeline(folio, claimed, category="TRAIN / BUS FARE"):
    import tempfile
    from pathlib import Path

    import fitz

    from app.services.extraction import bill_pipeline
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    pdf_path = Path(tempfile.mkdtemp()) / f"{uuid.uuid4()}.pdf"
    doc = fitz.open()
    page = doc.new_page()
    y = 20
    for line in folio.split("\n"):
        page.insert_text((24, y), line, fontsize=7)
        y += 8.5
    doc.save(pdf_path)
    doc.close()
    db = SessionLocal()
    try:
        report = ExpenseReport(filename="t.pdf", original_filename="t.pdf", file_hash=f"hash-{uuid.uuid4()}", file_size=100, source_path=str(pdf_path),
                               extraction_status="EXTRACTED", total_claimed=claimed, er_id="ER-CATERING-XLINE")
        db.add(report)
        db.commit()
        db.refresh(report)
        db.add(Expense(expense_report_id=report.id, s_no=1, expense_date=date(2026, 9, 24), category=category, claimed_amount=claimed,
                       bill_match_status="NO_MATCH", trip_match_status="NOT_PROCESSED"))
        db.commit()
        bill_pipeline.extract_and_match_bills(db, report, PyMuPDFTextExtractor(), content_last_page=0)
        validate_expense_report(db, report.id)
        evaluate_report(db, report.id)
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).one()
        types = [r.exception_type for r in db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report.id)]
        return {
            "amount": float(bill.amount) if bill.amount is not None else None,
            "catering_amount": float(bill.catering_amount) if bill.catering_amount is not None else None,
            "catering_mentioned": bill.catering_mentioned,
            "types": types,
        }
    finally:
        db.close()


def test_pipeline_separate_line_catering_beyond_the_cap_is_reviewed_and_excluded(client):
    out = _run_pipeline(_long([], ["Catering Charges (Incl. of GST)", "368.00"], "Total Fare 2,368.00"), 2000.0)
    assert out["amount"] == 2368.0
    assert out["catering_amount"] == 368.0 and out["catering_mentioned"] is True
    assert "CATERING INCLUDED" in out["types"]
    assert "AMOUNT MISMATCH" not in out["types"]


def test_pipeline_misleading_header_is_reviewed_with_no_amount(client):
    # The claim equals the printed total, so the bill matches directly; the review is raised, no amount invented.
    out = _run_pipeline(_long([], ["CATERING SERVICE", "ROOM RENT 1607.00"], "Total 2,368.00"), 2368.0)
    assert out["catering_amount"] is None and out["catering_mentioned"] is True
    assert "CATERING INCLUDED" in out["types"]


def test_pipeline_misleading_header_grants_no_exclusion(client):
    # Claim = total - 1,607: only rescued if room rent were (wrongly) read as catering. It must stay unsupported.
    out = _run_pipeline(_long([], ["CATERING SERVICE", "ROOM RENT 1607.00"], "Total 2,368.00"), 761.0)
    assert out["catering_amount"] is None
    assert "MISSING BILL" in out["types"] or "AMOUNT MISMATCH" in out["types"]
