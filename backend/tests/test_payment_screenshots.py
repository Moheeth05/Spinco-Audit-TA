"""UPI / wallet transaction screenshots (Google Pay, PhonePe, Paytm, bank apps).

A screenshot is evidence of a TRANSACTION, not a merchant invoice, and only a COMPLETED
one is proof of payment. Fixtures are synthetic and mirror the printed structure of such
apps; nothing here calls a provider.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from app.services.extraction import bill_matcher, bill_pipeline, payment_evidence
from app.services.extraction.bill_parser import parse_bill_text
from app.services.validation import checks

D = date

GPAY_67 = "Vikram Auto\n₹67\nCompleted\n24 Jul 2026, 12:22 PM\nUPI transaction ID\n517258795139\nGoogle transaction ID\nCICAgM5443911529"


# ---------------------------------------------------------------- extraction


def test_completed_rs67_payment_on_24_july_2026_at_1222_pm():
    bill = parse_bill_text(GPAY_67)
    assert bill.amount == 67.0
    assert bill.bill_date == D(2026, 7, 24)
    assert bill.is_payment
    assert (bill.payment_status, bill.transaction_time, bill.transaction_reference) == ("COMPLETED", "12:22", "517258795139")
    assert bill.date_reading.raw == "24 Jul 2026"  # the printed date string is preserved


@pytest.mark.parametrize(
    "date_line,expected",
    [
        ("14 JUNE 2026, 7:05 PM", D(2026, 6, 14)),
        ("14 Jun 2026, 7:05 pm", D(2026, 6, 14)),
        ("Jun 14, 2026 at 7:05 PM", D(2026, 6, 14)),
        ("14/06/2026 19:05", D(2026, 6, 14)),
        ("2026-06-14 19:05:11", D(2026, 6, 14)),
        ("14.06.2026 19:05", D(2026, 6, 14)),
        ("14-Jun-26 19:05", D(2026, 6, 14)),
    ],
)
def test_payment_date_formats_are_normalised_without_guessing(date_line, expected):
    text = f"Anil Cabs\n₹ 231.00\nPayment Successful\n{date_line}\nUPI Ref No: 123456789012"
    bill = parse_bill_text(text)
    assert bill.bill_date == expected
    assert bill.transaction_time == "19:05"
    assert bill.amount == 231.0


@pytest.mark.parametrize("clock,expected", [("12:22 PM", "12:22"), ("12:05 AM", "00:05"), ("7:05 PM", "19:05"), ("11:59 pm", "23:59"), ("19:05", "19:05"), ("00:00", "00:00")])
def test_time_is_normalised_to_24_hour_clock(clock, expected):
    ev = payment_evidence.extract(f"Paid to X\n₹100\nCompleted\n24 Jul 2026, {clock}\nUPI Ref No: 123456789012")
    assert ev.time == expected


def test_an_unreadable_time_stays_none_and_is_never_defaulted():
    ev = payment_evidence.extract("Paid to X\n₹100\nCompleted\n24 Jul 2026\nUPI Ref No: 123456789012")
    assert ev.time is None and ev.time_raw is None


@pytest.mark.parametrize(
    "banner,status",
    [("Completed", "COMPLETED"), ("Payment Successful", "COMPLETED"), ("Paid Successfully", "COMPLETED"), ("Payment Pending", "PENDING"),
     ("Processing", "PENDING"), ("Payment Failed", "FAILED"), ("Transaction declined", "FAILED"), ("Payment cancelled", "FAILED")],
)
def test_status_banners(banner, status):
    assert payment_evidence.extract(f"{banner}\n₹100\nPaid to X\nUPI Ref No: 123456789012").status == status


def test_a_page_with_no_status_has_none_and_is_not_penalised():
    assert payment_evidence.extract("Paid to X\n₹100\n24 Jul 2026, 12:22 PM\nUPI Ref No: 123456789012").status is None


def test_failed_outranks_a_success_word_elsewhere():
    assert payment_evidence.extract("Payment Failed\nPrevious payment successful\n₹100\nUPI Ref No: 123456789012").status == "FAILED"


def test_long_boilerplate_lines_are_not_status_banners():
    long = "If your payment failed or is pending the amount will be refunded within seven working days by your bank"
    assert payment_evidence.extract(f"Paid to X\n₹100\n{long}\nUPI Ref No: 123456789012").status is None


@pytest.mark.parametrize(
    "line,ref",
    [("UPI Ref No: 312790422420", "312790422420"), ("UPI Ref. No. 312790422420", "312790422420"),
     ("UPI transaction ID\n312790422420", "312790422420"), ("Txn ID: T2606141905ABCD12", "T2606141905ABCD12"),
     ("Transaction ID T2606141905ABCD12", "T2606141905ABCD12")],
)
def test_transaction_references(line, ref):
    assert payment_evidence.extract(f"Paid to X\n₹100\nCompleted\n{line}").reference == ref


def test_an_ordinary_invoice_is_not_a_payment_screenshot():
    ev = payment_evidence.extract("Orion Travels Pvt Ltd\nTAX INVOICE\nInvoice No: INV-1001\nInvoice Date: 18/07/2026\nTotal Amount Rs.840.00")
    assert not ev.is_payment and ev.status is None and ev.time is None and ev.reference is None


def test_a_standalone_figure_beside_the_status_banner_is_read_as_the_amount():
    bill = parse_bill_text("Dinesh Taxi\n245\nCompleted\n14 Jul 2026, 11:30 am\nUPI transaction ID\n630898546603")
    assert bill.amount == 245.0
    assert any("standalone figure" in w for w in bill.warnings)


def test_a_figure_not_adjacent_to_a_status_banner_is_never_taken_as_the_amount():
    bill = parse_bill_text("Dinesh Taxi\nCompleted\nSome note\n245\nSomething else\n14 Jul 2026, 11:30 am\nUPI transaction ID\n630898546603")
    assert bill.amount is None


def test_two_competing_standalone_figures_yield_no_amount():
    bill = parse_bill_text("Dinesh Taxi\n245\nCompleted\n390\nPending\n14 Jul 2026, 11:30 am\nUPI transaction ID\n630898546603")
    assert bill.amount is None


# ---------------------------------------------------------------- matching


def _pay(bill_id, page, day, *, amount=67.0, time=None, ref=None, status="COMPLETED", vendor="Vikram Auto", category=None):
    return {"id": bill_id, "page_number": page, "amount": amount, "bill_date": day, "date_candidates": [], "vendor": vendor,
            "description": vendor, "category_guess": category, "bill_number": None, "raw_text": None,
            "payment_status": status, "transaction_time": time, "transaction_reference": ref}


def _claim(s_no, day, amount=67.0, category="CAB CHARGES"):
    return {"s_no": s_no, "expense_rank": s_no, "claimed_amount": amount, "expense_date": day, "category": category, "expense_details": "ride"}


DAY = D(2026, 7, 24)


def test_same_amount_same_date_different_times_are_separate_transactions_each_supporting_a_claim():
    bills = [_pay(1, 2, DAY, time="12:22", ref="111111111111"), _pay(2, 4, DAY, time="18:40", ref="222222222222")]
    c1, c2 = bill_matcher.allocate_bills([_claim(1, DAY), _claim(2, DAY)], bills, first_bill_page=2)
    assert c1.status == c2.status == "MATCHED"
    assert {c1.matched_bill_id, c2.matched_bill_id} == {1, 2}
    assert "AMBIGUOUS PAIRING" in c1.reasons[0]  # explicitly marked, not presented as certain
    assert c1.confidence == c2.confidence == "MEDIUM"  # never HIGH


def test_same_amount_date_and_time_with_different_transaction_ids_are_separate_transactions():
    bills = [_pay(1, 2, DAY, time="12:22", ref="111111111111"), _pay(2, 4, DAY, time="12:22", ref="222222222222")]
    c1, c2 = bill_matcher.allocate_bills([_claim(1, DAY), _claim(2, DAY)], bills, first_bill_page=2)
    assert c1.status == c2.status == "MATCHED" and {c1.matched_bill_id, c2.matched_bill_id} == {1, 2}


def test_same_amount_date_and_time_without_distinguishing_ids_is_not_paired():
    bills = [_pay(1, 2, DAY, time="12:22"), _pay(2, 4, DAY, time="12:22")]
    outcomes = bill_matcher.allocate_bills([_claim(1, DAY), _claim(2, DAY)], bills, first_bill_page=2)
    assert [o.status for o in outcomes] == ["MULTIPLE_CANDIDATES", "MULTIPLE_CANDIDATES"]


def test_unreadable_times_and_no_references_are_not_proof_of_two_payments():
    bills = [_pay(1, 2, DAY), _pay(2, 4, DAY)]
    outcomes = bill_matcher.allocate_bills([_claim(1, DAY), _claim(2, DAY)], bills, first_bill_page=2)
    assert [o.status for o in outcomes] == ["MULTIPLE_CANDIDATES", "MULTIPLE_CANDIDATES"]


def test_one_unreadable_time_is_not_enough_to_call_two_payments_distinct():
    bills = [_pay(1, 2, DAY, time="12:22"), _pay(2, 4, DAY)]
    outcomes = bill_matcher.allocate_bills([_claim(1, DAY), _claim(2, DAY)], bills, first_bill_page=2)
    assert all(o.status != "MATCHED" for o in outcomes)


def test_multiple_same_amount_payments_on_different_dates_each_match_their_own_claim():
    days = [D(2026, 7, 3), D(2026, 7, 15), D(2026, 7, 22)]
    bills = [_pay(i + 1, 2 + i, d, time="10:00", ref=f"{i}" * 12) for i, d in enumerate(days)]
    outcomes = bill_matcher.allocate_bills([_claim(i + 1, d) for i, d in enumerate(days)], bills, first_bill_page=2)
    assert [o.matched_bill_id for o in outcomes] == [1, 2, 3] and all(o.status == "MATCHED" for o in outcomes)


@pytest.mark.parametrize("status", ["PENDING", "FAILED"])
def test_a_pending_or_failed_payment_never_confirms_a_claim(status):
    (outcome,) = bill_matcher.allocate_bills([_claim(1, DAY)], [_pay(1, 2, DAY, time="12:22", ref="111111111111", status=status)], first_bill_page=2)
    assert outcome.status == "REVIEW_REQUIRED" and outcome.matched_bill_id is None
    assert outcome.candidate_bill_id == 1  # still visible to the auditor
    assert status.lower() in outcome.reasons[0].lower() and "not proof" in outcome.reasons[0]


def test_a_completed_payment_wins_over_a_pending_one_without_creating_ambiguity():
    bills = [_pay(1, 2, DAY, time="12:22", ref="111111111111", status="PENDING"), _pay(2, 4, DAY, time="12:25", ref="222222222222")]
    (outcome,) = bill_matcher.allocate_bills([_claim(1, DAY)], bills, first_bill_page=2)
    assert (outcome.status, outcome.matched_bill_id) == ("MATCHED", 2)


def test_the_payment_status_check_requires_review_for_pending_and_failed_only():
    for status, expect in (("PENDING", checks.REVIEW_REQUIRED), ("FAILED", checks.REVIEW_REQUIRED), ("COMPLETED", checks.NOT_APPLICABLE), (None, checks.NOT_APPLICABLE)):
        bill = SimpleNamespace(payment_status=status, page_number=3)
        assert checks.check_payment_status(bill=bill).result == expect


# ---------------------------------------------------------------- category / policy


def test_a_payment_to_a_cab_service_claimed_as_food_keeps_the_category_mismatch():
    bill = SimpleNamespace(category_guess="Cab/Taxi", page_number=3, vendor="Rapid Cab Service", raw_text=None)
    result = checks.check_category_consistency(category="MARKETING EXPENSES - FOOD", bill=bill, bill_match_confidence="HIGH", payment_evidence_agrees=True)
    assert result.result == checks.EXCEPTION and result.exception_type == "CATEGORY MISMATCH"
    assert "MARKETING EXPENSES - FOOD" in result.remarks and "Cab/Taxi" in result.remarks


def test_a_payment_record_with_no_category_wording_supports_the_claimed_category_when_amount_and_date_agree():
    bill = SimpleNamespace(category_guess=None, page_number=3, vendor="Vikram Auto", raw_text=None)
    ok = checks.check_category_consistency(category="CAB CHARGES", bill=bill, bill_match_confidence="MEDIUM", payment_evidence_agrees=True)
    assert ok.result == checks.PASS
    # ...but a MEDIUM match with no payment evidence agreeing is still sent to review, as before
    unsure = checks.check_category_consistency(category="CAB CHARGES", bill=bill, bill_match_confidence="MEDIUM", payment_evidence_agrees=False)
    assert unsure.result == checks.REVIEW_REQUIRED


def test_the_claimed_category_is_never_rewritten_by_a_screenshot():
    claim = _claim(1, DAY, category="LODGING")
    bills = [_pay(1, 2, DAY, time="12:22", ref="111111111111", category="Cab/Taxi")]
    bill_matcher.allocate_bills([claim], bills, first_bill_page=2)
    assert claim["category"] == "LODGING"  # the claim dict is untouched; the conflict surfaces in validation


# ---------------------------------------------------------------- duplicate detection (same document / transaction)


def _entry(page, text, *, ref=None, number=None, amount=67.0, day=DAY, vendor="Vikram Auto"):
    bill = SimpleNamespace(page_number=page, bill_number=number, amount=amount, bill_date=day, vendor=vendor)
    parsed = SimpleNamespace(transaction_reference=ref)
    return bill, text, parsed


LONG_TEXT = "Vikram Auto Completed 24 Jul 2026, 12:22 PM UPI transaction ID 517258795139 Google transaction ID CICAgM5443911529"


def test_the_same_transaction_reference_is_one_transaction():
    first, second = _entry(2, "page A text " * 6, ref="517258795139"), _entry(4, "completely different words here " * 6, ref="517258795139")
    dups = bill_pipeline._find_strong_duplicates([first, second])
    assert list(dups) == [id(second[0])] and dups[id(second[0])][0] is first[0]  # the later page is the duplicate


def test_an_identical_screenshot_pasted_twice_is_a_duplicate_even_without_a_reference():
    first, second = _entry(2, LONG_TEXT), _entry(4, LONG_TEXT.upper())
    assert id(second[0]) in bill_pipeline._find_strong_duplicates([first, second])


def test_same_amount_and_date_with_different_times_or_references_is_not_a_duplicate():
    a = _entry(2, LONG_TEXT, ref="111111111111")
    b = _entry(4, LONG_TEXT.replace("12:22 PM", "6:40 PM").replace("517258795139", "999988887777"), ref="999988887777")
    assert bill_pipeline._find_strong_duplicates([a, b]) == {}


def test_the_same_invoice_number_vendor_amount_and_date_is_a_duplicate_but_a_numberless_one_is_not():
    a, b = _entry(2, "x " * 40, number="INV-1001", amount=840.0), _entry(5, "y " * 40, number="INV-1001", amount=840.0)
    assert id(b[0]) in bill_pipeline._find_strong_duplicates([a, b])
    c, d = _entry(2, "x " * 40, number="officialreimbursementpurposes", amount=840.0), _entry(5, "y " * 40, number="officialreimbursementpurposes", amount=840.0)
    assert bill_pipeline._find_strong_duplicates([c, d]) == {}  # no digit: not an identifying number
