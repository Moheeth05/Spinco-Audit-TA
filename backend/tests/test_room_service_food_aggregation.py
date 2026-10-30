"""Phase B: several room-service / food charges on one lodging bill.

The detector reads ONE amount per charge LINE (a line naming several food terms
is one row), classifies each row as DETAIL / SUMMARY / TAX, de-duplicates
identical rows conservatively, and reconciles:

    details only                     -> their sum
    one distinct summary only        -> that summary
    details + a summary that agrees  -> the detail sum (the summary is never added on top)
    anything else that is ambiguous  -> amount None, mention flag still True, no exclusion

The review flag never depends on the amount reconciling. Everything below is
SYNTHETIC; no real guest, invoice or receipt data.
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
from app.services.extraction import bill_parser
from app.services.validation import checks
from app.services.validation.pipeline import validate_expense_report

RAW_TEXT_CAP = 2000


def _analyze(text):
    return category_map.analyze_room_service_food(text)


# ---------- 1. reconciliation ----------


def test_two_distinct_detail_rows_reconcile_to_their_sum():
    text = "ROOM SERVICE RS26/3238 22/09/2026 357.00\nROOM SERVICE RS26/3301 23/09/2026 200.00"
    result = _analyze(text)
    assert result.amount == 557.0
    assert result.mentioned is True
    assert result.basis == "detail"
    assert sorted(result.charges) == [200.0, 357.0]
    assert category_map.detect_room_service_food_amount(text) == 557.0


def test_the_sum_is_exact_to_the_paisa():
    assert _analyze("ROOM SERVICE 357.10\nFOODING 200.20").amount == 557.30


@pytest.mark.parametrize(
    "text",
    [
        "ROOM SERVICE 357.00\nROOM SERVICE 200.00\nTotal Restaurant Charges 557.00",
        "Total Restaurant Charges 557.00\nROOM SERVICE 357.00\nROOM SERVICE 200.00",
        "ROOM SERVICE 357.00\nTotal Restaurant Charges 557.00\nROOM SERVICE 200.00",
        "ROOM SERVICE 357.00\nFOODING 200.00\nFood Total 557.00",
    ],
)
def test_details_plus_a_matching_summary_are_557_never_1114(text):
    result = _analyze(text)
    assert result.amount == 557.0
    assert result.basis == "detail+summary"


def test_a_single_detail_row_and_its_matching_summary_are_357_never_714():
    result = _analyze("ROOM SERVICE 357.00\nTotal Restaurant Charges 357.00")
    assert result.amount == 357.0 and result.basis == "detail+summary"


def test_a_single_distinct_summary_alone_is_used():
    result = _analyze("Total Restaurant Charges 557.00")
    assert result.amount == 557.0 and result.basis == "summary"
    assert _analyze("Total Restaurant Charges 557.00\nSub Total Food 557.00").amount == 557.0  # same value twice is still ONE distinct summary


def test_a_conflicting_summary_leaves_the_amount_unresolved_but_keeps_the_mention():
    result = _analyze("ROOM SERVICE 357.00\nROOM SERVICE 200.00\nTotal Restaurant Charges 600.00")
    assert result.amount is None
    assert result.mentioned is True
    assert result.basis == "unresolved"
    assert result.note and "could not be reconciled" in result.note
    assert category_map.detect_room_service_food_amount("ROOM SERVICE 357.00\nROOM SERVICE 200.00\nTotal Restaurant Charges 600.00") is None
    assert category_map.mentions_room_service_food("ROOM SERVICE 357.00\nROOM SERVICE 200.00\nTotal Restaurant Charges 600.00")


def test_a_summary_equal_to_only_one_of_the_details_is_a_disagreement():
    result = _analyze("ROOM SERVICE 357.00\nROOM SERVICE 200.00\nTotal Restaurant Charges 357.00")
    assert result.amount is None and result.mentioned is True


def test_several_different_summary_values_are_unresolved():
    result = _analyze("Total Room Service 557.00\nTotal Food 600.00")
    assert result.amount is None and result.mentioned is True and result.basis == "unresolved"
    assert _analyze("ROOM SERVICE 557.00\nTotal Room Service 557.00\nTotal Food 600.00").amount is None


def test_more_than_eight_candidate_rows_are_unresolved_but_eight_are_fine():
    nine = "\n".join(f"ROOM SERVICE RS26/{3200 + i} {10 + i}/09/2026 {100 + i}.00" for i in range(9))
    eight = "\n".join(f"ROOM SERVICE RS26/{3200 + i} {10 + i}/09/2026 {100 + i}.00" for i in range(8))
    unresolved = _analyze(nine)
    assert unresolved.amount is None and unresolved.mentioned is True and "more than 8" in unresolved.note
    assert _analyze(eight).amount == float(sum(100 + i for i in range(8)))


# ---------- 2. one amount per LINE ----------


@pytest.mark.parametrize(
    "line, amount",
    [
        ("ROOM SERVICE Restaurant Charges RS26/3238 357.00", 357.0),
        ("Food & Beverage - Room Service 200.00", 200.0),
        ("In Room Dining Restaurant Charges FOODING 508.00", 508.0),
    ],
)
def test_a_line_naming_several_food_terms_counts_once(line, amount):
    assert _analyze(line).amount == amount
    assert _analyze(line + "\n" + line.replace("357.00", "357.00")).amount == amount  # an exact repeat is still one


def test_two_different_lines_each_with_several_terms_still_add_up():
    text = "ROOM SERVICE Restaurant Charges RS26/3238 357.00\nROOM SERVICE Restaurant Charges RS26/3301 200.00"
    assert _analyze(text).amount == 557.0


# ---------- 3. de-duplication ----------


def test_identical_rows_without_date_or_voucher_tokens_count_once():
    assert _analyze("ROOM SERVICE 357.00\nROOM SERVICE 357.00").amount == 357.0
    assert _analyze("ROOM SERVICE 357.00\nroom   service 357.00").amount == 357.0  # case / spacing only


def test_the_same_voucher_repeated_is_one_charge():
    text = "ROOM SERVICE RS26/3238 357.00\nROOM SERVICE RS26/3238 357.00"
    assert _analyze(text).amount == 357.0


def test_distinct_vouchers_with_the_same_amount_are_separate_charges():
    text = "ROOM SERVICE RS26/3238 357.00\nROOM SERVICE RS26/3301 357.00"
    assert _analyze(text).amount == 714.0


def test_distinct_dates_with_the_same_amount_are_separate_charges():
    text = "ROOM SERVICE 22/09/2026 357.00\nROOM SERVICE 23/09/2026 357.00"
    assert _analyze(text).amount == 714.0


def test_identical_rows_carrying_only_a_date_are_ambiguous_so_unresolved():
    # Could be a repeated page or two orders on the same day: never guessed.
    result = _analyze("ROOM SERVICE 22/09/2026 357.00\nROOM SERVICE 22/09/2026 357.00")
    assert result.amount is None and result.mentioned is True and result.basis == "unresolved"


# ---------- 4. tax / GST / service-charge rows are not food-charge details ----------


def test_tax_rows_are_never_added_to_the_food_charge():
    text = "\n".join(
        [
            "ROOM SERVICE 357.00",
            "GST on Room Service 17.85",
            "CGST 2.5% on Room Service 8.93",
            "SGST on Room Service 8.93",
            "Service Charge on Room Service 35.70",
            "Room Service Tax 5.00",
            "IGST Food & Beverage 12.00",
        ]
    )
    result = _analyze(text)
    assert result.amount == 357.0 and result.mentioned is True


def test_a_room_service_charge_row_is_a_detail_not_a_service_charge_tax_row():
    assert _analyze("Room Service Charge 357.00").amount == 357.0


def test_tax_rows_alone_keep_the_mention_but_give_no_amount():
    result = _analyze("GST on Room Service 17.85\nService Charge on Room Service 35.70")
    assert result.amount is None and result.mentioned is True


# ---------- 5. voucher references are never amounts ----------


def test_voucher_references_are_never_read_as_amounts():
    for text in ("ROOM SERVICE RS26/3238", "ROOM SERVICE Rs26/3238 24/09/2026", "ROOM SERVICE VCH RS26/3238 KOT 4417"):
        result = _analyze(text)
        assert result.amount is None and result.mentioned is True, text
    assert _analyze("ROOM SERVICE RS26/3238 357.00").amount == 357.0
    assert _analyze("ROOM SERVICE RS26/3238\nROOM SERVICE RS26/3301 200.00").amount == 200.0  # the unreadable row adds nothing, invents nothing


# ---------- 6. existing controls ----------


@pytest.mark.parametrize(
    "text",
    [
        "Rate Type: Breakfast",
        "Rate Type: Breakfast 2,550.00",
        "FSSAI Food Safety and Standards Authority of India Lic 11521999000123",
        "Hotel Food Court Road\nRoom Rent 3,000.00",
        "Hotel Sagar\nTARIFF 5,777.00",
        "Room service is available 24 hours",
        "Food & Beverage outlets are open from 7 am to 11 pm.",
        "",
        None,
    ],
)
def test_existing_non_charge_controls_still_give_nothing(text):
    result = _analyze(text)
    assert result.amount is None and result.mentioned is False and result.basis == "none"


def test_an_unreadable_amount_row_is_a_mention_with_no_amount():
    result = _analyze("ROOM SERVICE Restaurant Charges RS26/3238")
    assert result.amount is None and result.mentioned is True


def test_the_previous_single_row_behaviour_is_unchanged():
    for text, amount in (("ROOM SERVICE 357.00", 357.0), ("FOODING 508.00", 508.0), ("Food 220.00", 220.0), ("ROOM SERVICE\n357.00", 357.0)):
        assert _analyze(text).amount == amount
    assert _analyze("Hotel Food Court Road").mentioned is False


# ---------- 7. the resolvers: stored evidence is authoritative, NULL is a legacy row ----------

SINGLE_CLEAN_ROW_IN_THE_PREFIX = "Aurangabad Gymkhana Club\nROOM SERVICE RS26/3238 22/09/2026 357.00\nTotal 5,657.00"


def test_a_stored_unresolved_result_never_falls_back_to_a_clean_looking_prefix():
    # Full text was ambiguous (amount stored None, flag True); raw_text only shows one tidy row. No exclusion.
    assert category_map.resolve_room_service_food_amount(SINGLE_CLEAN_ROW_IN_THE_PREFIX, None, True) is None
    assert category_map.resolve_room_service_food_mentioned(SINGLE_CLEAN_ROW_IN_THE_PREFIX, True, None) is True


def test_a_stored_no_charge_result_is_final_too():
    assert category_map.resolve_room_service_food_amount(SINGLE_CLEAN_ROW_IN_THE_PREFIX, None, False) is None
    assert category_map.resolve_room_service_food_mentioned("ROOM SERVICE 357.00", False, None) is False


def test_a_stored_reconciled_amount_wins():
    assert category_map.resolve_room_service_food_amount("ROOM SERVICE 357.00", 557.0, True) == 557.0


def test_a_legacy_row_with_null_evidence_falls_back_to_the_aggregating_detector_on_raw_text():
    text = "ROOM SERVICE RS26/3238 22/09/2026 357.00\nROOM SERVICE RS26/3301 23/09/2026 200.00"
    assert category_map.resolve_room_service_food_amount(text, None, None) == 557.0
    assert category_map.resolve_room_service_food_mentioned(text, None, None) is True
    assert category_map.resolve_room_service_food_amount("Rate Type: Breakfast", None, None) is None


# ---------- 8. the checks use the reconciled amount ----------


def _bill(raw_text="Room Charges 2,550.00", amount=5657.0, **stored):
    return SimpleNamespace(page_number=4, page_number_end=None, amount=amount, bill_date=date(2026, 9, 24), category_guess="Lodging", raw_text=raw_text, **stored)


def test_a_reconciled_total_is_excluded_from_the_amount_comparison_for_lodging():
    bill = _bill(room_service_food_amount=557.0, room_service_food_mentioned=True)
    amount = checks.check_bill_amount(claimed_amount=5100.0, bill=bill, category="LODGING")
    assert amount.result == checks.PASS and "room service / food" in amount.remarks
    review = checks.check_room_service_food(category="LODGING", bill=bill, claimed_amount=5100.0)
    assert review.result == checks.REVIEW_REQUIRED and "Rs.557.00" in review.remarks


def test_an_unresolved_total_grants_no_exclusion_but_still_requires_review():
    bill = _bill(raw_text=SINGLE_CLEAN_ROW_IN_THE_PREFIX, room_service_food_amount=None, room_service_food_mentioned=True)
    amount = checks.check_bill_amount(claimed_amount=5100.0, bill=bill, category="LODGING")
    assert amount.result == checks.EXCEPTION and amount.exception_type == "AMOUNT MISMATCH"
    review = checks.check_room_service_food(category="LODGING", bill=bill, claimed_amount=5100.0)
    assert review.result == checks.REVIEW_REQUIRED and "could not be determined" in review.remarks and review.bill_amount is None


@pytest.mark.parametrize("category", [None, "STAFF WELFARE", "MARKETING EXPENSES - FOOD", "TRAIN / BUS FARE", "CAB CHARGES"])
def test_the_reconciled_total_is_never_used_outside_lodging(category):
    bill = _bill(room_service_food_amount=557.0, room_service_food_mentioned=True)
    assert checks.check_room_service_food(category=category, bill=bill, claimed_amount=5100.0).result == checks.NOT_APPLICABLE
    amount = checks.check_bill_amount(claimed_amount=5100.0, bill=bill, category=category)
    assert amount.result == checks.EXCEPTION and amount.exception_type == "AMOUNT MISMATCH"


def test_a_legacy_bill_with_two_charges_in_raw_text_reconciles_through_the_fallback():
    legacy = _bill(raw_text="ROOM SERVICE RS26/3238 22/09/2026 357.00\nROOM SERVICE RS26/3301 23/09/2026 200.00")
    assert checks.check_bill_amount(claimed_amount=5100.0, bill=legacy, category="LODGING").result == checks.PASS
    assert checks.check_room_service_food(category="LODGING", bill=legacy).result == checks.REVIEW_REQUIRED


# ---------- 9. the parser stores the reconciled result and an audit note ----------

_PAD_LINE = "Hotel policy note: checkout time is twelve noon and late checkout is subject to availability."


def _long_folio(before_cap: list[str], after_cap: list[str], total: str) -> str:
    padding: list[str] = []
    while sum(len(x) + 1 for x in padding) < 2300:
        padding.append(_PAD_LINE)
    text = "\n".join(["Aurangabad Gymkhana Club", "Date: 24/09/2026", "Room Charges 2,550.00", "Room Charges 2,550.00", *before_cap, *padding, *after_cap, total])
    first_after = after_cap[0]
    assert text.rindex(first_after) > RAW_TEXT_CAP, "fixture must put these rows beyond the raw_text cap"
    return text


def test_parser_reconciles_rows_split_across_the_raw_text_cap():
    folio = _long_folio(["ROOM SERVICE RS26/3238 22/09/2026 357.00"], ["ROOM SERVICE RS26/3301 23/09/2026 200.00"], "Total 5,657.00")
    parsed = bill_parser.parse_bill_text(folio)
    assert len(parsed.raw_text) == RAW_TEXT_CAP and "RS26/3301" not in parsed.raw_text
    assert parsed.room_service_food_amount == 557.0 and parsed.room_service_food_mentioned is True
    assert any("2 charges" in w and "557.00" in w for w in parsed.warnings)


def test_parser_stores_none_with_the_mention_when_the_full_text_conflicts():
    folio = _long_folio(["ROOM SERVICE RS26/3238 22/09/2026 357.00"], ["ROOM SERVICE RS26/3301 23/09/2026 200.00", "Total Restaurant Charges 600.00"], "Total 5,657.00")
    parsed = bill_parser.parse_bill_text(folio)
    assert parsed.room_service_food_amount is None and parsed.room_service_food_mentioned is True
    assert any("could not be reconciled" in w for w in parsed.warnings)


def test_parser_adds_no_note_for_a_single_clean_charge_or_no_charge():
    one = bill_parser.parse_bill_text("Room Charges 2,550.00\nROOM SERVICE 357.00\nTotal 2,907.00")
    assert one.room_service_food_amount == 357.0 and not any("charge" in w and "Room service/food" in w for w in one.warnings)
    none = bill_parser.parse_bill_text("Room Charges 2,550.00\nTotal 2,550.00")
    assert none.room_service_food_mentioned is False and not any("Room service/food" in w for w in none.warnings)


# ---------- 10. end to end through bill_pipeline ----------


def _run_pipeline(folio, claimed):
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
                               extraction_status="EXTRACTED", total_claimed=claimed, er_id="ER-AGGREGATION")
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
        return {
            "amount": float(bill.amount) if bill.amount is not None else None,
            "rs_amount": float(bill.room_service_food_amount) if bill.room_service_food_amount is not None else None,
            "rs_mentioned": bill.room_service_food_mentioned,
            "notes": bill.extraction_notes or "",
            "types": types,
        }
    finally:
        db.close()


def test_pipeline_two_charges_split_across_the_cap_are_reconciled_and_excluded(client):
    folio = _long_folio(["ROOM SERVICE RS26/3238 22/09/2026 357.00"], ["ROOM SERVICE RS26/3301 23/09/2026 200.00"], "Total 5,657.00")
    out = _run_pipeline(folio, 5100.0)
    assert out["amount"] == 5657.0
    assert out["rs_amount"] == 557.0 and out["rs_mentioned"] is True
    assert "2 charges" in out["notes"]
    assert "ROOM SERVICE / FOOD INCLUDED" in out["types"]
    assert "AMOUNT MISMATCH" not in out["types"]


def test_pipeline_ambiguous_repeated_rows_are_reviewed_with_no_amount(client):
    # Identical date-only rows, one on each side of the cap: repeat or two orders? Unresolved.
    # The claim equals the printed total so the bill is matched directly; the point here is that the
    # review flag is raised even though no amount could be reconciled.
    folio = _long_folio(["ROOM SERVICE 22/09/2026 357.00"], ["ROOM SERVICE 22/09/2026 357.00"], "Total 5,457.00")
    out = _run_pipeline(folio, 5457.0)
    assert out["amount"] == 5457.0
    assert out["rs_amount"] is None and out["rs_mentioned"] is True
    assert "could not be reconciled" in out["notes"]
    assert "ROOM SERVICE / FOOD INCLUDED" in out["types"]  # the review flag never depends on the amount reconciling


def test_pipeline_unresolved_amount_grants_no_exclusion(client):
    # Same ambiguous folio, but the claim is the total MINUS one 357.00 row: it must NOT be rescued by an
    # exclusion, so the claim is not accepted as matching the 5,457.00 bill.
    folio = _long_folio(["ROOM SERVICE 22/09/2026 357.00"], ["ROOM SERVICE 22/09/2026 357.00"], "Total 5,457.00")
    out = _run_pipeline(folio, 5100.0)
    assert out["rs_amount"] is None and out["rs_mentioned"] is True
    assert "excluded" not in out["notes"]
    assert "MISSING BILL" in out["types"] or "AMOUNT MISMATCH" in out["types"]  # the 5,100 claim stays unsupported
