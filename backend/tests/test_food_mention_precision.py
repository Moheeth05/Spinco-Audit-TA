"""Phase A precision: descriptive hotel prose is not evidence of a room-service /
food CHARGE.

The detector reads the full bill text, so a folio's footer terms are now seen.
A mention-only hit (a food term with no readable amount) must therefore look
like a charge ROW, not like a sentence describing the hotel. A genuine row whose
amount is unreadable still counts: mention flag kept, amount None, review
required, nothing excluded.

All text below is SYNTHETIC.
"""

import uuid
from datetime import date

import pytest

from app.database import SessionLocal
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.validation import ExceptionRecord
from app.services import category_map
from app.services.exception_engine import evaluate_report
from app.services.extraction import bill_parser
from app.services.validation import checks
from app.services.validation.pipeline import validate_expense_report

RAW_TEXT_CAP = 2000

BOILERPLATE = [
    "Room service is available 24 hours",
    "Food & Beverage outlets are open",
    "Room service is available 24 hours on request.",
    "Food & Beverage outlets are open from 7 am to 11 pm.",
    "In-room dining is offered between 7 am and 11 pm",
    "Breakfast and room service are included in the tariff",
    "Room service will be charged extra at actuals.",
    "24 hr Room Service",
    "Room Service: 7 am - 11 pm",
    "Restaurant charges are payable directly at the outlet",
    "All room service orders are subject to 5% GST",
    "Guests are requested to settle room service and restaurant bills before check out",
]

# (text, expected amount): genuine charge rows with a readable amount.
GENUINE_WITH_AMOUNT = [
    ("ROOM SERVICE 357.00", 357.0),
    ("FOODING 508.00", 508.0),
    ("Food 220.00", 220.0),
    ("ROOM SERVICE Restaurant Charges RS26/3238 357.00", 357.0),
    ("Room Service - Dinner Rs. 450.50", 450.50),
    ("ROOM SERVICE\n357.00", 357.0),
]

# Genuine charge rows whose amount is unreadable: still a mention, never an amount.
GENUINE_UNREADABLE = [
    "ROOM SERVICE Restaurant Charges RS26/3238",
    "FOODING",
    "Outlet: Room Service",
    "ROOM SERVICE RS26/3238 24/09/2026",
    "Restaurant Charges RS26/3300",
    "In Room Dining Order 4417",
    "FOOD & BEVERAGE",
    # Words that appear on real folio rows / column headers must never read as prose.
    "Extra Charges: Room Service",
    "Guest Charges - Room Service",
]


@pytest.mark.parametrize("line", BOILERPLATE)
def test_descriptive_prose_is_not_a_charge_mention(line):
    assert not category_map.mentions_room_service_food(line)
    assert category_map.detect_room_service_food_amount(line) is None


@pytest.mark.parametrize("line", BOILERPLATE)
def test_prose_among_real_folio_lines_is_still_not_a_mention(line):
    folio = f"Hotel Sagar\nRoom Charges 2,550.00\n{line}\nTotal 2,550.00"
    assert not category_map.mentions_room_service_food(folio)


@pytest.mark.parametrize("text, amount", GENUINE_WITH_AMOUNT)
def test_genuine_charge_rows_are_still_detected(text, amount):
    assert category_map.detect_room_service_food_amount(text) == amount
    assert category_map.mentions_room_service_food(text)


@pytest.mark.parametrize("text", GENUINE_UNREADABLE)
def test_genuine_row_with_unreadable_amount_keeps_the_mention_and_never_an_amount(text):
    assert category_map.mentions_room_service_food(text)
    assert category_map.detect_room_service_food_amount(text) is None


@pytest.mark.parametrize("line", BOILERPLATE)
def test_prose_never_hides_a_genuine_row_next_to_it(line):
    for row, amount in (("ROOM SERVICE 357.00", 357.0), ("FOODING 508.00", 508.0)):
        for folio in (f"{line}\n{row}\nTotal 5,457.00", f"{row}\n{line}\nTotal 5,457.00"):
            assert category_map.detect_room_service_food_amount(folio) == amount
            assert category_map.mentions_room_service_food(folio)


def test_an_unreadable_genuine_row_is_not_cancelled_by_prose_elsewhere():
    folio = "Room service is available 24 hours\nROOM SERVICE Restaurant Charges RS26/3238\nTotal 5,457.00"
    assert category_map.mentions_room_service_food(folio)
    assert category_map.detect_room_service_food_amount(folio) is None


def test_existing_exclusions_are_unchanged():
    for text in (
        "Rate Type: Breakfast",
        "FSSAI Food Safety and Standards Authority of India Lic 11521999000123",
        "Hotel Food Court Road\nRoom Rent 3,000.00",
        "Hotel Sagar\nTARIFF 5,777.00",
    ):
        assert not category_map.mentions_room_service_food(text)


# ---------- the same through the parser, the checks and the pipeline (text beyond the raw_text cap) ----------

_PAD_LINE = "Hotel policy note: checkout time is twelve noon and late checkout is subject to availability."


def _long_folio(footer: str, total: str = "Total 5,100.00") -> str:
    padding: list[str] = []
    while sum(len(x) + 1 for x in padding) < 2300:
        padding.append(_PAD_LINE)
    text = "\n".join(["Aurangabad Gymkhana Club", "Date: 24/09/2026", "Room Charges 2,550.00", "Room Charges 2,550.00", *padding, footer, total])
    assert text.index(footer.splitlines()[0]) > RAW_TEXT_CAP
    return text


def test_parser_ignores_boilerplate_footer_beyond_the_cap():
    footer = "Room service is available 24 hours\nFood & Beverage outlets are open from 7 am to 11 pm."
    parsed = bill_parser.parse_bill_text(_long_folio(footer))
    assert len(parsed.raw_text) == RAW_TEXT_CAP
    assert parsed.room_service_food_mentioned is False
    assert parsed.room_service_food_amount is None


def test_parser_still_finds_a_genuine_row_beyond_the_cap_with_boilerplate_before_it():
    footer = "Room service is available 24 hours\nROOM SERVICE Restaurant Charges RS26/3238 357.00"
    parsed = bill_parser.parse_bill_text(_long_folio(footer, total="Total 5,457.00"))
    assert parsed.room_service_food_amount == 357.0
    assert parsed.room_service_food_mentioned is True


def test_parser_keeps_an_unreadable_genuine_row_beyond_the_cap_as_mention_only():
    footer = "Room service is available 24 hours\nROOM SERVICE Restaurant Charges RS26/3238"
    parsed = bill_parser.parse_bill_text(_long_folio(footer, total="Total 5,457.00"))
    assert parsed.room_service_food_mentioned is True
    assert parsed.room_service_food_amount is None


def _stored(mentioned, amount=None):
    from types import SimpleNamespace

    return SimpleNamespace(page_number=4, page_number_end=None, amount=5100.0, bill_date=date(2026, 9, 24), category_guess="Lodging",
                           raw_text="Room Charges 2,550.00", room_service_food_amount=amount, room_service_food_mentioned=mentioned)


def test_check_passes_when_nothing_but_prose_was_found():
    parsed = bill_parser.parse_bill_text(_long_folio("Room service is available 24 hours"))
    bill = _stored(parsed.room_service_food_mentioned, parsed.room_service_food_amount)
    assert checks.check_room_service_food(category="LODGING", bill=bill, claimed_amount=5100.0).result == checks.PASS


def test_unreadable_genuine_row_requires_review_and_excludes_nothing():
    bill = _stored(True, None)
    review = checks.check_room_service_food(category="LODGING", bill=bill, claimed_amount=5000.0)
    assert review.result == checks.REVIEW_REQUIRED and "could not be determined" in review.remarks
    amount = checks.check_bill_amount(claimed_amount=5000.0, bill=bill, category="LODGING")
    assert amount.result == checks.EXCEPTION and amount.exception_type == "AMOUNT MISMATCH"


def _run_pipeline_types(folio, claimed):
    from pathlib import Path

    import fitz

    from app.services.extraction import bill_pipeline
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    import tempfile

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
                               extraction_status="EXTRACTED", total_claimed=claimed, er_id="ER-PRECISION")
        db.add(report)
        db.commit()
        db.refresh(report)
        db.add(Expense(expense_report_id=report.id, s_no=1, expense_date=date(2026, 9, 24), category="LODGING", claimed_amount=claimed,
                       bill_match_status="NO_MATCH", trip_match_status="NOT_PROCESSED"))
        db.commit()
        bill_pipeline.extract_and_match_bills(db, report, PyMuPDFTextExtractor(), content_last_page=0)
        validate_expense_report(db, report.id)
        evaluate_report(db, report.id)
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).one()
        types = [r.exception_type for r in db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report.id)]
        return bill, types
    finally:
        db.close()


def test_pipeline_boilerplate_footer_raises_no_review_item(client):
    footer = "Room service is available 24 hours\nFood & Beverage outlets are open from 7 am to 11 pm."
    bill, types = _run_pipeline_types(_long_folio(footer), 5100.0)
    assert not bill.room_service_food_mentioned
    assert "ROOM SERVICE / FOOD INCLUDED" not in types
    assert "AMOUNT MISMATCH" not in types


def test_pipeline_genuine_row_beyond_the_cap_is_still_reviewed(client):
    footer = "Room service is available 24 hours\nROOM SERVICE Restaurant Charges RS26/3238 357.00"
    bill, types = _run_pipeline_types(_long_folio(footer, total="Total 5,457.00"), 5100.0)
    assert float(bill.room_service_food_amount) == 357.0
    assert "ROOM SERVICE / FOOD INCLUDED" in types
    assert "AMOUNT MISMATCH" not in types
