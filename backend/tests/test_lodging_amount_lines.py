"""Lodging room-service / food: amount-on-the-next-line parsing and column safety.

1. A charge amount alone on the line after the label is read in every currency
   form, including "₹ 357.00" with a space or a non-breaking space after the symbol.
2. A label list followed by a COLUMN of amounts
       ROOM RENT / FOODING / 1607.00 / 508.00 / TOTAL 2308.00
   cannot tie any amount to any label: FOODING must not be given the room-rent
   figure. The mention (review) is kept, the amount resolves to None, and no
   exclusion is granted - a claim of 2,308 - 1,607 = 701 must not pass.
3. Valid layouts (label / amount, alternating label / amount rows) still work, and
   next-line rows with DIFFERENT amounts are separate charges (the de-duplication
   key includes the amount).

All text below is SYNTHETIC; no real folio or receipt data.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from app.services import category_map
from app.services.extraction import bill_matcher, bill_parser
from app.services.validation import checks

A = category_map.analyze_room_service_food

NBSP = " "
CURRENCY_FORMS = [
    "357.00",
    "Rs. 357.00",
    "Rs.357.00",
    "Rs 357.00",
    "INR 357.00",
    "INR357.00",
    "₹357.00",
    "₹ 357.00",
    f"₹{NBSP}357.00",
    f"Rs.{NBSP}357.00",
]


# ---------- 1. every currency form on the next line ----------


@pytest.mark.parametrize("form", CURRENCY_FORMS, ids=[ascii(f) for f in CURRENCY_FORMS])
def test_next_line_amount_in_every_currency_form(form):
    result = A(f"ROOM SERVICE\n{form}")
    assert result.amount == 357.0 and result.mentioned is True


@pytest.mark.parametrize("form", CURRENCY_FORMS, ids=[ascii(f) for f in CURRENCY_FORMS])
def test_next_line_amount_followed_by_a_labelled_total(form):
    assert A(f"Room Charges 2,550.00\nROOM SERVICE\n{form}\nTOTAL 2,907.00").amount == 357.0


@pytest.mark.parametrize("form", CURRENCY_FORMS, ids=[ascii(f) for f in CURRENCY_FORMS])
def test_same_line_forms_are_unchanged(form):
    assert A(f"ROOM SERVICE {form}").amount == 357.0


@pytest.mark.parametrize("label", ["ROOM SERVICE", "FOODING", "In Room Dining", "Restaurant Charges"])
def test_the_next_line_form_works_for_every_food_term(label):
    assert A(f"{label}\n₹ 508.00").amount == 508.0


def test_a_next_line_amount_without_decimals_is_still_not_an_amount():
    # Unchanged conservative rule: a bare integer on its own line could be a room number, quantity or voucher.
    result = A("ROOM SERVICE\n357")
    assert result.amount is None and result.mentioned is True


# ---------- 2. label list + column of amounts: ambiguous, never an exclusion ----------

COLUMN = "ROOM RENT\nFOODING\n1607.00\n508.00\nTOTAL 2308.00"
COLUMN_LAYOUTS = [
    COLUMN,
    "ROOM RENT\nROOM SERVICE\n₹ 1607.00\n₹ 508.00\nTOTAL 2308.00",
    f"ROOM RENT\nROOM SERVICE\n₹{NBSP}1607.00\n₹{NBSP}508.00\nTOTAL 2308.00",
    "ROOM RENT\nFOODING\n1607.00\n508.00\n2308.00",
    "ROOM RENT\nFOODING\n\n1607.00\n\n508.00\nTOTAL 2308.00",  # blank lines between the amounts
]


@pytest.mark.parametrize("text", COLUMN_LAYOUTS, ids=[ascii(t)[:50] for t in COLUMN_LAYOUTS])
def test_a_label_list_with_an_amount_column_has_no_food_amount_but_keeps_the_mention(text):
    result = A(text)
    assert result.amount is None
    assert result.mentioned is True
    assert category_map.detect_room_service_food_amount(text) is None
    assert category_map.mentions_room_service_food(text)


def _bill(raw_text, amount=2308.0, **stored):
    return SimpleNamespace(page_number=4, page_number_end=None, amount=amount, bill_date=date(2026, 9, 24), category_guess="Lodging", raw_text=raw_text, **stored)


@pytest.mark.parametrize("claimed", [701.0, 1800.0])
def test_no_exclusion_is_granted_for_a_column_layout_through_the_legacy_raw_text_fallback(claimed):
    bill = _bill(COLUMN)  # no stored evidence at all: the raw_text detector decides
    result = checks.check_bill_amount(claimed_amount=claimed, bill=bill, category="LODGING")
    assert result.result == checks.EXCEPTION and result.exception_type == "AMOUNT MISMATCH"
    review = checks.check_room_service_food(category="LODGING", bill=bill, claimed_amount=claimed)
    assert review.result == checks.REVIEW_REQUIRED and "could not be determined" in review.remarks and review.bill_amount is None


@pytest.mark.parametrize("claimed", [701.0, 1800.0])
def test_no_exclusion_is_granted_for_a_column_layout_through_stored_evidence(claimed):
    parsed = bill_parser.parse_bill_text(COLUMN)
    assert parsed.room_service_food_amount is None and parsed.room_service_food_mentioned is True
    bill = _bill(COLUMN, room_service_food_amount=parsed.room_service_food_amount, room_service_food_mentioned=parsed.room_service_food_mentioned)
    result = checks.check_bill_amount(claimed_amount=claimed, bill=bill, category="LODGING")
    assert result.result == checks.EXCEPTION and result.exception_type == "AMOUNT MISMATCH"


def test_the_matcher_grants_no_amount_credit_for_a_column_layout():
    bill = {"id": 1, "amount": 2308.0, "bill_date": None, "vendor": None, "description": None, "category_guess": None, "bill_number": None, "raw_text": COLUMN}
    expense = {"claimed_amount": 701.0, "expense_date": date(2026, 9, 24), "category": "LODGING", "expense_details": None}
    score, reasons = bill_matcher.score_pair(expense, bill)
    assert any("negative signal" in r for r in reasons) and score < 0
    assert not bill_matcher._amount_supported(expense, bill)


# ---------- 3. valid layouts still work ----------


@pytest.mark.parametrize(
    "text, amount",
    [
        ("ROOM SERVICE\n357.00", 357.0),
        ("ROOM SERVICE\n357.00\nTOTAL 2907.00", 357.0),
        ("FOODING\n508.00\nTOTAL 2308.00", 508.0),
        ("ROOM RENT\n1607.00\nFOODING\n508.00\nTOTAL\n2308.00", 508.0),  # alternating label / amount rows
        ("ROOM RENT 1607.00\nFOODING\n508.00\nTOTAL 2308.00", 508.0),
        ("ROOM SERVICE\n357.00\nROOM SERVICE\n200.00", 557.0),
        ("ROOM SERVICE\n₹ 357.00\nROOM SERVICE\n₹ 200.00", 557.0),
        (f"ROOM SERVICE\n₹{NBSP}357.00\nFOODING\n₹{NBSP}200.00", 557.0),
        ("ROOM SERVICE\n357.00\n\nTOTAL 2907.00", 357.0),  # a blank line, then a label
    ],
)
def test_valid_label_then_amount_layouts_still_give_their_amount(text, amount):
    assert A(text).amount == amount


def test_next_line_rows_with_different_amounts_are_separate_charges_not_one_deduplicated_row():
    result = A("ROOM SERVICE\n357.00\nROOM SERVICE\n200.00")
    assert result.amount == 557.0 and result.basis == "detail"
    assert sorted(result.charges) == [200.0, 357.0]


def test_identical_next_line_rows_without_tokens_still_count_once():
    assert A("ROOM SERVICE\n357.00\nROOM SERVICE\n357.00").amount == 357.0


def test_next_line_rows_with_distinct_vouchers_stay_separate_even_with_the_same_amount():
    assert A("ROOM SERVICE RS26/3238\n357.00\nROOM SERVICE RS26/3301\n357.00").amount == 714.0


def test_a_summary_next_line_row_still_reconciles_with_its_details():
    text = "ROOM SERVICE\n357.00\nROOM SERVICE\n200.00\nTotal Restaurant Charges 557.00"
    assert A(text).amount == 557.0 and A(text).basis == "detail+summary"


def test_the_lodging_aggregation_for_same_line_rows_is_unchanged():
    text = "ROOM SERVICE RS26/3238 22/09/2026 357.00\nROOM SERVICE RS26/3301 23/09/2026 200.00\nTotal Restaurant Charges 557.00"
    assert A(text).amount == 557.0
    assert A("ROOM SERVICE 357.00\nROOM SERVICE 357.00").amount == 357.0
    assert A("ROOM SERVICE 22/09/2026 357.00\nROOM SERVICE 22/09/2026 357.00").amount is None
