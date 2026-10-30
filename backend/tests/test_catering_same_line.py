"""Catering: an amount on the SAME line must be linked to the label, not merely near it.

The detector used to accept any amount within 40 characters of the word "catering"
on its line, so a table row "Catering Service Option Fare 2,412.00" or a form row
"CATERING SERVICE ROOM RENT 1607.00" produced a catering charge that belongs to a
different column. A same-line amount is now accepted only when the text between
the label and the amount is nothing but qualifier words / punctuation:

  accepted words (deliberate)   charges / charge, service, incl(uding|usive), of, gst, tax(es),
                                amount, applicable, fee(s), cost, bill, total   + punctuation / brackets
  anything else                 (Option, Fare, Convenience, Room Rent, Veg Meal, No, ...) blocks the match

"fee / cost / bill / total" are accepted ONLY directly after the label (they then name the
catering charge itself: "Catering Fee 150.00", "Catering Total 368.00"); "Catering Total Fare
2,780.00" and "Catering Convenience Fee 11.80" are rejected because "Fare" / "Convenience" sit
in between. A label whose amount cannot be linked keeps the mention (review) with no amount
and no exclusion.

All text below is SYNTHETIC; no real ticket, folio or receipt data.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from app.services import category_map
from app.services.extraction import bill_matcher, bill_parser
from app.services.validation import checks

detect = category_map.detect_catering_amount
RAW_TEXT_CAP = 2000

# ---------- must be rejected: an amount that belongs to something else ----------

REJECTED = [
    "Catering Service Option Fare 2,412.00",
    "Catering Option Total Fare 2,780.00",
    "CATERING SERVICE ROOM RENT 1607.00",
    "Catering Convenience Fee 11.80",
    "Catering Total Fare 2,780.00",
    "Catering Fee Room Rent 1607.00",
    "# Name Age Gender Catering Service Option Total Fare 2,780.00",
    "Catering Bill No. 4417 368.00",
    "Catering Charges - Veg Meal 150.00",  # documented trade-off: an extra word between label and amount is not linked
    "Catering Charges RS26/3238",
]


@pytest.mark.parametrize("text", REJECTED)
def test_an_amount_that_is_not_linked_to_the_label_is_rejected(text):
    assert detect(text) is None
    assert category_map.mentions_catering(text)  # the mention is kept: it still needs review


# ---------- must keep working: known valid formats ----------

VALID = [
    ("Catering Charges (Incl. of GST): Rs. 368.00", 368.0),
    ("Catering Charges 368.00", 368.0),
    ("Catering charges Rs.250.00 included", 250.0),
    ("Catering Charges (Incl. of GST) ₹ 174.00", 174.0),
    ("Catering : ₹ 368.00", 368.0),
    ("Catering Charges Rs. 368/-", 368.0),
    ("Catering Charges (Incl. of GST) ₹ 174.00", 174.0),
    ("Catering charges Rs.150.00 included", 150.0),
    ("Ticket Fare ₹ 1,766.00\nCatering Charges (Incl. of GST) ₹ 174.00", 174.0),
    ("Fare Rs.500.00\nCatering charges Rs.150.00 included", 150.0),
    ("CATERING SERVICE 150.00", 150.0),  # a header that carries its OWN amount on the same row
    ("Catering Service Charges 150.00", 150.0),
]


@pytest.mark.parametrize("text, expected", VALID)
def test_known_valid_same_line_formats_still_give_their_amount(text, expected):
    assert detect(text) == expected
    assert category_map.mentions_catering(text)


# ---------- fee / cost / bill / total: accepted only directly after the label ----------

ACCEPTED_NAMING_WORDS = [
    ("Catering Fee 150.00", 150.0),
    ("Catering Fees Rs. 150.00", 150.0),
    ("Catering Cost 150.00", 150.0),
    ("Catering Bill 150.00", 150.0),
    ("Catering Total 368.00", 368.0),
    ("Catering Charges Total 368.00", 368.0),
    ("CATERING FEE: ₹ 150.00", 150.0),
]


@pytest.mark.parametrize("text, expected", ACCEPTED_NAMING_WORDS)
def test_fee_cost_bill_total_directly_after_the_label_name_the_catering_charge(text, expected):
    assert detect(text) == expected


# ---------- unchanged behaviour ----------


def test_a_real_catering_line_after_a_rejected_header_is_still_found():
    assert detect("Catering Service Option Fare 2,412.00\nCatering Charges (Incl. of GST)\n368.00") == 368.0
    assert detect("# Name Age Gender Catering Service Option Total Fare 2,780.00\nCatering Charges (Incl. of GST): Rs. 368.00") == 368.0


def test_the_first_linked_occurrence_wins():
    assert detect("Catering Charges 150.00\nCatering Charges 368.00") == 150.0


def test_multi_line_layouts_are_unchanged():
    assert detect("Ticket Fare\n2,412.00\nCatering Charges (Incl. of GST)\n368.00\n") == 368.0
    assert detect("Catering Charges\n(Incl. of GST)\n₹ 368.00") == 368.0
    assert detect("CATERING SERVICE\nROOM RENT 1607.00") is None
    assert detect("ROOM RENT\nFOODING\nCATERING SERVICE\n1607.00\n508.00\n300.00") is None


# ---------- a rejected amount grants no exclusion; the review is kept ----------


def _bill(raw_text, amount, **stored):
    return SimpleNamespace(page_number=4, page_number_end=None, amount=amount, bill_date=date(2026, 9, 24), category_guess="Train", raw_text=raw_text, **stored)


@pytest.mark.parametrize("text, unrelated", [("Catering Service Option Fare 2,412.00", 2412.0), ("CATERING SERVICE ROOM RENT 1607.00", 1607.0), ("Catering Option Total Fare 2,780.00", 2780.0)])
def test_no_catering_exclusion_is_granted_from_an_unlinked_amount(text, unrelated):
    bill = _bill(text, 5000.0)
    for category in ("TRAIN / BUS FARE", "LODGING", None):
        result = checks.check_bill_amount(claimed_amount=5000.0 - unrelated, bill=bill, category=category)
        assert result.result == checks.EXCEPTION and result.exception_type == "AMOUNT MISMATCH", (category, text)
    review = checks.check_catering(category="TRAIN / BUS FARE", bill=bill, claimed_amount=5000.0 - unrelated)
    assert review.result == checks.REVIEW_REQUIRED
    assert review.remarks == "Catering charge mentioned; amount could not be determined. Requires policy review."
    assert review.bill_amount is None


def test_the_matcher_grants_no_amount_credit_from_an_unlinked_amount():
    bill = {"id": 1, "amount": 5000.0, "bill_date": None, "vendor": None, "description": None, "category_guess": None, "bill_number": None,
            "raw_text": "Catering Service Option Fare 2,412.00"}
    expense = {"claimed_amount": 2588.0, "expense_date": date(2026, 9, 24), "category": "TRAIN / BUS FARE", "expense_details": None}
    score, reasons = bill_matcher.score_pair(expense, bill)
    assert any("negative signal" in r for r in reasons) and score < 0
    assert not bill_matcher._amount_supported(expense, bill)


def test_a_linked_amount_still_earns_the_exclusion():
    bill = _bill("Ticket Fare 2,000.00\nCatering Charges (Incl. of GST): Rs. 368.00\nTotal Fare 2,368.00", 2368.0)
    assert checks.check_bill_amount(claimed_amount=2000.0, bill=bill, category="TRAIN / BUS FARE").result == checks.PASS


# ---------- precedence and long bills are unchanged ----------


def test_below_the_cap_the_raw_text_reading_still_beats_a_stored_amount_and_the_classifier_fallback_still_works():
    regex_wins = _bill("Catering charges Rs.150.00", 500.0, catering_amount=999.0)
    assert "Rs.150.00" in checks.check_catering(category="TRAIN / BUS FARE", bill=regex_wins).remarks
    fallback = _bill("Catering Service Option Fare 2,412.00", 5000.0, catering_amount=250.0)  # raw_text has no LINKED amount
    assert "Rs.250.00" in checks.check_catering(category="TRAIN / BUS FARE", bill=fallback).remarks


_PAD_LINE = "Hotel policy note: checkout time is twelve noon and late checkout is subject to availability."


def _long(after_cap: list[str], total: str) -> str:
    padding: list[str] = []
    while sum(len(x) + 1 for x in padding) < 2300:
        padding.append(_PAD_LINE)
    text = "\n".join(["IRCTC e-ticket", "Date: 24/09/2026", "Ticket fare 2,000.00", *padding, *after_cap, total])
    assert text.rindex(after_cap[0]) > RAW_TEXT_CAP
    return text


def test_a_linked_same_line_charge_beyond_the_cap_is_stored():
    parsed = bill_parser.parse_bill_text(_long(["Catering Charges (Incl. of GST): Rs. 368.00"], "Total Fare 2,368.00"))
    assert len(parsed.raw_text) == RAW_TEXT_CAP and "Catering" not in parsed.raw_text
    assert parsed.catering_amount == 368.0 and parsed.catering_mentioned is True


def test_an_unlinked_same_line_amount_beyond_the_cap_stores_a_mention_and_no_amount():
    parsed = bill_parser.parse_bill_text(_long(["Catering Option Total Fare 2,780.00"], "Total 5,000.00"))
    assert parsed.catering_amount is None and parsed.catering_mentioned is True


# ---------- lodging behaviour is untouched ----------


def test_lodging_room_service_food_aggregation_is_untouched():
    text = "ROOM SERVICE 357.00\nROOM SERVICE 200.00\nTotal Restaurant Charges 557.00"
    assert category_map.analyze_room_service_food(text).amount == 557.0
    monsoon = "HOTEL MONSOON\nCATERING SERVICE\nROOM RENT 1607.00\nFOODING 508.00\nTOTAL 2308.00"
    parsed = bill_parser.parse_bill_text(monsoon)
    assert parsed.catering_amount is None and parsed.room_service_food_amount == 508.0
