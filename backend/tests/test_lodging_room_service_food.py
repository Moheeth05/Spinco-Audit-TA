"""Lodging room-service / food review rule - the LODGING counterpart of the
train-bill catering rule.

A lodging bill that includes room service or food is REVIEW REQUIRED (never an
exception), and - like catering - that charge is left out of the amount
comparison so an otherwise-correct claim isn't flagged as an AMOUNT MISMATCH.

Folio texts below are SYNTHETIC reproductions of the layouts of the sample
Expense Reports' lodging bills (ER-29928 Aurangabad Gymkhana folios, ER-30727
Hotel Monsoon / Hotel Sagar); they contain no real guest or invoice data.
"""

import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.services import category_map
from app.services.exception_engine import evaluate_report
from app.services.extraction import bill_matcher
from app.services.validation import checks
from app.services.validation.pipeline import validate_expense_report

# Folio with a ROOM SERVICE row: 2,550 + 357 + 2,550 = 5,457; claimed 5,100.
FOLIO_ROOM_SERVICE = """Aurangabad Gymkhana Club
Invoice No: 2526/2923
Room Charges 2,550.00
ROOM SERVICE Restaurant Charges RS26/3238 357.00
Room Charges 2,550.00
Total Extra Charges With Inclusion 357.00
SGST 273.22
CGST 273.22
Total 5,457.00
Rate Type: Breakfast
"""
# Same club, no room service; "Rate Type: Breakfast" is only the tariff plan.
FOLIO_PLAIN_BREAKFAST_PLAN = """Aurangabad Gymkhana Club
Invoice No: 2526/3128
Rate Type: Breakfast
Room Charges 2,550.00
Room Charges 2,550.00
Total 5,100.00
"""
# Handwritten small-hotel bill: 1607 + 508 + 96.50 + 96.50 = 2308; claimed 1800.
FOLIO_FOODING = """HOTEL MONSOON
Bill No 0936
CATERING SERVICE
ROOM RENT 1607.00
FOODING 508.00
CGST 96.50
SGST 96.50
TOTAL 2308.00
"""
FOLIO_TARIFF_ONLY = """Hotel Sagar
Tax Invoice FM6353BIL0002247
TARIFF 5,777.00
Grand Total 5,777.00
FSSAI Food Safety and Standards Authority of India Lic 11521999000123
"""


def _bill(raw_text, amount):
    return SimpleNamespace(page_number=4, page_number_end=None, amount=amount, bill_date=date(2026, 9, 24), category_guess="Lodging", raw_text=raw_text)


# ---------- detector ----------


@pytest.mark.parametrize(
    "text, expected",
    [
        (FOLIO_ROOM_SERVICE, 357.0),
        (FOLIO_FOODING, 508.0),
        ("Room Service Rs. 450.50", 450.50),
        ("In-Room Dining 1,250.00", 1250.0),
        ("Food & Beverages 300.00", 300.0),
        ("Food 220.00", 220.0),  # a bare "food" counts when a charge is on its line
        ("ROOM SERVICE\n357.00", 357.0),  # column-extracted: amount alone on the next line
    ],
)
def test_detector_reads_the_charge(text, expected):
    assert category_map.detect_room_service_food_amount(text) == expected
    assert category_map.mentions_room_service_food(text)


@pytest.mark.parametrize(
    "text",
    [
        FOLIO_PLAIN_BREAKFAST_PLAN,
        FOLIO_TARIFF_ONLY,
        "Hotel Food Court Road\nRoom Rent 3,000.00",  # bare "food" with no charge on its line
        "FSSAI Food Safety and Standards Authority 5,000.00",
        "Rate Type: Breakfast 2,550.00",
        "",
        None,
    ],
)
def test_detector_ignores_non_charges(text):
    assert category_map.detect_room_service_food_amount(text) is None
    assert not category_map.mentions_room_service_food(text)


def test_detector_never_reads_a_voucher_reference_as_an_amount():
    # "RS26/3238" must not become "Rs 26"; no real amount on the line -> undetermined.
    text = "ROOM SERVICE Restaurant Charges RS26/3238\nRoom Charges 2,550.00"
    assert category_map.detect_room_service_food_amount(text) is None
    assert category_map.mentions_room_service_food(text)


def test_detector_does_not_reach_across_lines_for_an_amount():
    text = "ROOM SERVICE\nRoom Charges 2,550.00\nTotal 2,907.00"
    assert category_map.detect_room_service_food_amount(text) is None
    assert category_map.mentions_room_service_food(text)


def test_detector_ignores_a_zero_charge():
    assert category_map.detect_room_service_food_amount("ROOM SERVICE 0.00") is None


# ---------- check_room_service_food ----------


def test_room_service_on_lodging_is_review_required_not_exception():
    result = checks.check_room_service_food(category="LODGING", bill=_bill(FOLIO_ROOM_SERVICE, 5457.0), claimed_amount=5100.0)
    assert result.result == checks.REVIEW_REQUIRED
    assert result.exception_type is None
    assert "Rs.357.00" in result.remarks
    assert result.bill_amount == 357.0


def test_fooding_on_lodging_is_review_required():
    result = checks.check_room_service_food(category="LODGING", bill=_bill(FOLIO_FOODING, 2308.0), claimed_amount=1800.0)
    assert result.result == checks.REVIEW_REQUIRED
    assert "Rs.508.00" in result.remarks


def test_raw_category_text_with_suffix_is_still_lodging():
    result = checks.check_room_service_food(category="LODGING (LODGING)", bill=_bill(FOLIO_ROOM_SERVICE, 5457.0))
    assert result.result == checks.REVIEW_REQUIRED


def test_mention_without_readable_amount_is_review_required_and_never_invents_one():
    result = checks.check_room_service_food(category="LODGING", bill=_bill("ROOM SERVICE Restaurant Charges RS26/3238", 5457.0), claimed_amount=5100.0)
    assert result.result == checks.REVIEW_REQUIRED
    assert "could not be determined" in result.remarks
    assert result.bill_amount is None


@pytest.mark.parametrize("text", [FOLIO_PLAIN_BREAKFAST_PLAN, FOLIO_TARIFF_ONLY])
def test_lodging_bill_without_room_service_or_food_passes(text):
    result = checks.check_room_service_food(category="LODGING", bill=_bill(text, 5100.0), claimed_amount=5100.0)
    assert result.result == checks.PASS


@pytest.mark.parametrize("category", ["STAFF WELFARE", "MARKETING EXPENSES - FOOD", "TRAIN / BUS FARE", "CAB CHARGES", None])
def test_not_applicable_outside_lodging(category):
    result = checks.check_room_service_food(category=category, bill=_bill(FOLIO_FOODING, 2308.0))
    assert result.result == checks.NOT_APPLICABLE


def test_no_bill_is_not_applicable():
    assert checks.check_room_service_food(category="LODGING", bill=None).result == checks.NOT_APPLICABLE


# ---------- amount comparison ----------


def test_bill_amount_matches_once_room_service_is_excluded_for_lodging():
    result = checks.check_bill_amount(claimed_amount=5100.0, bill=_bill(FOLIO_ROOM_SERVICE, 5457.0), category="LODGING")
    assert result.result == checks.PASS
    assert "room service / food" in result.remarks


def test_bill_amount_matches_once_fooding_is_excluded_for_lodging():
    result = checks.check_bill_amount(claimed_amount=1800.0, bill=_bill(FOLIO_FOODING, 2308.0), category="LODGING")
    assert result.result == checks.PASS


def test_whole_rupee_rounding_still_applies_after_exclusion():
    result = checks.check_bill_amount(claimed_amount=5100.0, bill=_bill("ROOM SERVICE 357.40", 5457.40), category="LODGING")
    assert result.result == checks.PASS


@pytest.mark.parametrize("category", [None, "STAFF WELFARE", "TRAIN / BUS FARE", "MARKETING EXPENSES - FOOD"])
def test_food_is_never_excluded_outside_lodging(category):
    result = checks.check_bill_amount(claimed_amount=1800.0, bill=_bill(FOLIO_FOODING, 2308.0), category=category)
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "AMOUNT MISMATCH"


def test_a_real_mismatch_on_a_lodging_bill_with_room_service_is_still_an_exception():
    result = checks.check_bill_amount(claimed_amount=4000.0, bill=_bill(FOLIO_ROOM_SERVICE, 5457.0), category="LODGING")
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "AMOUNT MISMATCH"


def test_unreadable_room_service_amount_is_never_guessed_so_mismatch_stays():
    result = checks.check_bill_amount(claimed_amount=5100.0, bill=_bill("ROOM SERVICE RS26/3238\nTotal 5,457.00", 5457.0), category="LODGING")
    assert result.result == checks.EXCEPTION


def test_exact_total_still_passes_for_lodging_with_room_service():
    result = checks.check_bill_amount(claimed_amount=5457.0, bill=_bill(FOLIO_ROOM_SERVICE, 5457.0), category="LODGING")
    assert result.result == checks.PASS
    assert result.remarks == "Amount matches"


# ---------- bill matcher ----------


def _expense(category, claimed):
    return {"claimed_amount": claimed, "expense_date": date(2026, 9, 24), "category": category, "expense_details": None}


def _bill_dict(raw_text, amount):
    return {"id": 1, "amount": amount, "bill_date": None, "vendor": None, "description": None, "category_guess": None, "bill_number": None, "raw_text": raw_text}


def test_matcher_gives_amount_credit_once_room_service_is_excluded_for_lodging():
    score, reasons = bill_matcher.score_pair(_expense("LODGING", 5100.0), _bill_dict(FOLIO_ROOM_SERVICE, 5457.0))
    assert score >= 40, reasons
    assert any("room service / food is excluded" in r for r in reasons)


def test_matcher_does_not_exclude_food_for_other_categories():
    score, reasons = bill_matcher.score_pair(_expense("STAFF WELFARE", 5100.0), _bill_dict(FOLIO_ROOM_SERVICE, 5457.0))
    assert any("negative signal" in r for r in reasons)
    assert score < 0


def test_matcher_amount_supported_for_lodging_only():
    bill = _bill_dict(FOLIO_FOODING, 2308.0)
    assert bill_matcher._amount_supported(_expense("LODGING", 1800.0), bill)
    assert not bill_matcher._amount_supported(_expense("STAFF WELFARE", 1800.0), bill)
    assert not bill_matcher._amount_supported(_expense("LODGING", 1900.0), bill)


# ---------- DB-level: the exception engine ----------


def _run_lodging_case(raw_text, bill_amount, claimed):
    db = SessionLocal()
    try:
        report = ExpenseReport(
            filename="t.pdf", original_filename="t.pdf", file_hash=f"hash-{uuid.uuid4()}", file_size=100,
            source_path="unused.pdf", extraction_status="EXTRACTED", total_claimed=claimed, er_id="ER-LODGING-FOOD",
        )
        db.add(report)
        db.commit()
        db.refresh(report)
        bill = Bill(
            expense_report_id=report.id, page_number=5, amount=bill_amount, bill_date=date(2026, 9, 24),
            category_guess="Lodging", extraction_status="EXTRACTED", match_status="MATCHED", raw_text=raw_text,
        )
        db.add(bill)
        db.commit()
        db.refresh(bill)
        db.add(Expense(
            expense_report_id=report.id, s_no=1, expense_date=date(2026, 9, 24), category="LODGING",
            claimed_amount=claimed, bill_id=bill.id, candidate_bill_id=bill.id, bill_match_status="MATCHED",
            trip_match_status="NOT_PROCESSED",
        ))
        db.commit()
        validate_expense_report(db, report.id)
        evaluate_report(db, report.id)
        report = db.get(ExpenseReport, report.id)
        from app.models.validation import ExceptionRecord

        types = [r.exception_type for r in db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report.id)]
        return report.overall_status, types
    finally:
        db.close()


def test_engine_lodging_room_service_is_review_required_and_not_an_amount_mismatch(client):
    status, types = _run_lodging_case(FOLIO_ROOM_SERVICE, 5457.0, 5100.0)
    assert "ROOM SERVICE / FOOD INCLUDED" in types
    assert "AMOUNT MISMATCH" not in types
    assert status == "REVIEW REQUIRED"


def test_engine_lodging_without_room_service_raises_no_room_service_review(client):
    status, types = _run_lodging_case(FOLIO_PLAIN_BREAKFAST_PLAN, 5100.0, 5100.0)
    assert "ROOM SERVICE / FOOD INCLUDED" not in types
    assert "AMOUNT MISMATCH" not in types
