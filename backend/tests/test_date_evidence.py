"""Supporting-document dates: day/month order is established from independent
EVIDENCE or not at all.

Policy under test (dates.read_dates / apply_group_evidence, bill_parser, checks):
  * the raw printed string, every valid reading, the chosen method and its confidence are kept;
  * order is settled, in strength order, by (1) an explicit cue in the document, (2) the same
    document's own unambiguous dates in the same format (or an unambiguous date it echoes),
    (3) other documents of the SAME TEMPLATE whose dates prove the order, none contradicting;
  * otherwise the date is UNRESOLVED: value None, both candidates kept, no date credit;
  * the claimed date and the report month are never evidence;
  * different templates in one PDF keep their own conventions.
All fixtures are synthetic.
"""

from datetime import date

import pytest

from app.services.extraction import bill_matcher, dates
from app.services.extraction.bill_parser import parse_bill_text
from app.services.validation import checks

D = date


def first(text: str) -> dates.DateReading:
    readings = dates.read_dates(text)
    assert readings, f"no date read from {text!r}"
    return readings[0]


# ------------------------------------------------------------------ raw preservation / formats


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Date: 14 JULY 2026", D(2026, 7, 14)),
        ("Date: July 14, 2026", D(2026, 7, 14)),
        ("Date: 14th July 2026", D(2026, 7, 14)),
        ("Date: 14-Jul-26", D(2026, 7, 14)),
        ("Date: 14 Jul 2026, 12:22 pm", D(2026, 7, 14)),
        ("Date: 2026-07-14", D(2026, 7, 14)),
        ("Date: 2026-07-14 19:05:11", D(2026, 7, 14)),
        ("Date: 14/07/2026", D(2026, 7, 14)),  # 14 cannot be a month
        ("Date: 07/18/2026", D(2026, 7, 18)),  # 18 cannot be a month
        ("Date: 18/07/2026", D(2026, 7, 18)),
        ("Date: 14-07-2026", D(2026, 7, 14)),
        ("Date: 07-18-2026", D(2026, 7, 18)),
        ("Date: 14.07.2026", D(2026, 7, 14)),
        ("Date: 7/18/26", D(2026, 7, 18)),
        ("Date: 18/7/26", D(2026, 7, 18)),
        ("Date: 07/07/2026", D(2026, 7, 7)),  # symmetric: the same either way
        ("Date: 14/Jun/2026", D(2026, 6, 14)),
        ("Date: 14Jun26", D(2026, 6, 14)),
        ("Date: 14 Jun'26", D(2026, 6, 14)),
        ("Date: Jun-14-2026", D(2026, 6, 14)),
        ("Date: June14,2026", D(2026, 6, 14)),
        ("Date: 14th of June 2026", D(2026, 6, 14)),
        ("Date: 2026/06/14", D(2026, 6, 14)),
    ],
)
def test_unambiguous_formats_resolve_without_any_other_evidence(text, expected):
    reading = first(text)
    assert reading.value == expected
    assert reading.confidence == "HIGH"
    assert reading.raw in text  # the exact printed string is kept


@pytest.mark.parametrize("text", ["Paid 14 Jun 12:30 PM", "5 Mar 12:30", "Ref 1.2.26 version", "date 14.06.26", "Total 12.50.2026x"])
def test_strings_that_only_look_like_dates_are_not_read_as_dates(text):
    assert [r for r in dates.read_dates(text) if r.method != "invalid"] == []


@pytest.mark.parametrize("text", ["Date: 32/13/2026", "Date: 31/04/2026", "Date: 29/02/2026", "Date: 7/71/26", "Date: 00/05/2026", "Date: 12/50/1999"])
def test_impossible_or_malformed_dates_are_rejected_never_repaired(text):
    readings = [r for r in dates.read_dates(text) if r.method != "invalid"]
    assert readings == []


def test_leap_day_is_valid_only_where_the_calendar_allows_it():
    assert first("Date: 02/29/2028").value == D(2028, 2, 29)  # month-first reading is the only valid one
    assert first("Date: 29/02/2028").value == D(2028, 2, 29)


# ------------------------------------------------------------------ ambiguous: cue / same document / unresolved


def test_ambiguous_numeric_date_without_evidence_stays_unresolved_with_both_candidates():
    reading = first("Paid on 07/02/2026")
    assert reading.value is None
    assert reading.method == "unresolved"
    assert reading.confidence is None
    assert set(reading.candidates) == {D(2026, 7, 2), D(2026, 2, 7)}
    assert reading.ambiguous
    assert reading.raw == "07/02/2026"


def test_explicit_mmddyyyy_cue_makes_the_same_string_2_july():
    reading = first("Trip Receipt\nDate (MM/DD/YYYY): 07/02/2026")
    assert reading.value == D(2026, 7, 2)
    assert (reading.method, reading.confidence) == ("cue", "HIGH")


def test_explicit_ddmmyyyy_cue_makes_the_same_string_7_february():
    reading = first("Trip Receipt\nDate (DD/MM/YYYY): 07/02/2026")
    assert reading.value == D(2026, 2, 7)
    assert (reading.method, reading.confidence) == ("cue", "HIGH")


def test_contradictory_cues_prove_nothing():
    reading = first("Date (MM/DD/YYYY) ... Date (DD/MM/YYYY): 07/02/2026")
    assert reading.value is None and reading.ambiguous


def test_same_document_corroboration_by_an_unambiguous_date_in_the_same_format():
    text = "Check-in 07/02/2026\nCheck-out 18/02/2026"  # 18 proves DD/MM for this document
    first_reading, second = dates.read_dates(text)
    assert first_reading.value == D(2026, 2, 7) and first_reading.method == "same_document"
    assert second.value == D(2026, 2, 18)


def test_same_document_proofs_that_disagree_leave_it_unresolved():
    text = "A 07/02/2026\nB 18/02/2026\nC 02/18/2026"
    readings = dates.read_dates(text)
    assert readings[0].value is None and readings[0].ambiguous


def test_an_unambiguous_date_the_receipt_echoes_settles_the_order():
    reading = dates.read_dates("Date: Jul 10, 2026\nPaid 7/10/26")[1]  # same calendar day printed twice
    assert reading.value == D(2026, 7, 10)
    assert reading.method == "same_document"


def test_a_july_report_may_contain_a_legitimate_february_receipt():
    """Nothing about 'the report is for July' is consulted: a cue-proven February
    date stays February."""
    reading = first("Claim period: July 2026\nReceipt dated (DD/MM/YYYY) 07/02/2026")
    assert reading.value == D(2026, 2, 7)


# ------------------------------------------------------------------ report/template-level evidence


UBER_AMBIGUOUS = "Uber\nThanks for riding\nWe hope you enjoyed your ride\nTotal 231.17\nCash 7/6/26 10:22 AM\nTrip details kilometers"
UBER_PROOF = "Uber\nThanks for riding\nWe hope you enjoyed your ride\nTotal 231.15\nCash 7/17/26 5:56 PM\nTrip details kilometers"
INVOICE_AMBIGUOUS = "Orion Travels Pvt Ltd\nTAX INVOICE\nInvoice No: INV-1001\nInvoice Date: 06/07/2026\nTotal Amount Rs.840.00"
INVOICE_PROOF = "Orion Travels Pvt Ltd\nTAX INVOICE\nInvoice No: INV-1002\nInvoice Date: 18/07/2026\nTotal Amount Rs.1260.00"


def group(*texts):
    own = [dates.read_dates(t) for t in texts]
    return dates.apply_group_evidence(list(texts), own)


def test_same_template_receipt_proves_month_first_for_its_ambiguous_sibling():
    ambiguous, proof = group(UBER_AMBIGUOUS, UBER_PROOF)
    assert ambiguous[0].value == D(2026, 7, 6)
    assert (ambiguous[0].method, ambiguous[0].confidence) == ("report_group", "MEDIUM")
    assert ambiguous[0].raw == "7/6/26"  # raw string preserved
    assert proof[0].value == D(2026, 7, 17)


def test_a_lone_ambiguous_receipt_is_not_resolved_by_anything():
    (only,) = group(UBER_AMBIGUOUS)
    assert only[0].value is None and only[0].ambiguous


def test_mixed_formats_from_different_templates_keep_their_own_conventions():
    """US-style rides (month-first) and DD/MM invoices (day-first) in one PDF: each ambiguous
    date takes its order only from its OWN template's proof."""
    uber_a, uber_p, inv_a, inv_p = group(UBER_AMBIGUOUS, UBER_PROOF, INVOICE_AMBIGUOUS, INVOICE_PROOF)
    assert uber_a[0].value == D(2026, 7, 6)  # month-first
    assert inv_a[0].value == D(2026, 7, 6)  # 06/07/2026 read DAY-first: 6 July (month-first would be 7 June)
    assert inv_a[0].order == "DMY" and uber_a[0].order == "MDY"


def test_a_template_with_no_proof_does_not_borrow_another_templates_convention():
    """The invoice has no same-template proof; the Uber receipts' month-first proof must not leak in."""
    uber_p, inv_a = group(UBER_PROOF, INVOICE_AMBIGUOUS)
    assert inv_a[0].value is None and inv_a[0].ambiguous


def test_an_unrelated_template_with_the_identical_printed_format_is_not_pooled():
    """Both print zero-padded DD?MM?YYYY with no clock, but they are different issuers/templates:
    the fuel receipt's month-first proof says nothing about the hotel folio's order."""
    hotel = "Lakeview Residency\nGuest Folio\nRoom Tariff\nCheck-in 07/06/2026\nGrand Total Rs. 5400"
    fuel = "Highway Petro Plaza\nProduct PETROL\nVolume 18.00 L\nDate 07/18/2026\nAmount Rs. 1800"
    assert dates.read_dates(hotel)[0].signature == dates.read_dates(fuel)[0].signature  # same printed format
    hotel_readings, _ = group(hotel, fuel)
    assert hotel_readings[0].value is None and hotel_readings[0].ambiguous


def test_contradicting_proofs_inside_one_template_leave_siblings_unresolved():
    day_first_proof = UBER_PROOF.replace("7/17/26 5:56 PM", "17/7/26 5:56 PM")
    ambiguous, *_ = group(UBER_AMBIGUOUS, UBER_PROOF, day_first_proof)
    assert ambiguous[0].value is None and ambiguous[0].ambiguous


def test_group_resolutions_are_not_used_as_proof_for_further_documents():
    """No chains: only documents that PROVE the order (a component > 12 or a cue) count."""
    already_resolved_by_group = [dates._with_order(dates.read_dates(UBER_AMBIGUOUS)[0], "MDY", "report_group", "MEDIUM")]
    sibling_text = UBER_AMBIGUOUS.replace("231.17", "99.00")
    out = dates.apply_group_evidence([UBER_AMBIGUOUS, sibling_text], [already_resolved_by_group, dates.read_dates(sibling_text)])
    assert out[1][0].value is None and out[1][0].ambiguous  # a derived resolution is not proof


def test_different_printed_formats_are_not_pooled_even_in_a_similar_template():
    padded_4 = UBER_AMBIGUOUS.replace("7/6/26 10:22 AM", "07/06/2026 10:22 AM")
    ambiguous, proof = group(padded_4, UBER_PROOF)  # proof prints 7/17/26 (2-digit year): a different format
    assert ambiguous[0].value is None


# ------------------------------------------------------------------ parser output / audit trail


def test_parser_returns_no_date_for_an_unresolved_bill_and_keeps_the_candidates():
    bill = parse_bill_text("Neha Taxi\nPaid 275.00\nDate 07/02/2026\nUPI Ref No 123456789012")
    assert bill.bill_date is None
    assert bill.date_reading.ambiguous
    assert {c.isoformat() for c in bill.date_reading.candidates} == {"2026-07-02", "2026-02-07"}
    assert any("ambiguous" in w and "07/02/2026" in w for w in bill.warnings)
    assert not any("could not be reliably determined" in w and "date" in w.lower() for w in bill.warnings)


def test_parser_resolves_the_same_text_when_a_cue_is_present():
    bill = parse_bill_text("Neha Taxi\nTotal 275.00\nDate (MM/DD/YYYY): 07/02/2026")
    assert bill.bill_date == D(2026, 7, 2)
    assert bill.date_reading.method == "cue"


# ------------------------------------------------------------------ matching & validation behaviour


class _Bill:
    def __init__(self, bill_date, raw, candidates):
        self.bill_date = bill_date
        self.bill_date_raw = raw
        self.bill_date_candidates = candidates
        self.page_number = 5
        self.raw_text = None


def test_validation_routes_an_unresolved_date_to_review_even_when_one_reading_equals_the_claim():
    result = checks.check_bill_date(expense_date=D(2026, 7, 2), bill=_Bill(None, "07/02/2026", "2026-07-02|2026-02-07"))
    assert result.result == checks.REVIEW_REQUIRED
    assert result.exception_type is None  # not a DATE MISMATCH
    assert "2026-07-02" in result.details and "2026-02-07" in result.details
    assert "not evidence" in result.details  # the coincidence is explicitly not counted


def test_validation_does_not_raise_a_false_date_mismatch_for_an_unresolved_date():
    result = checks.check_bill_date(expense_date=D(2026, 3, 15), bill=_Bill(None, "07/02/2026", "2026-07-02|2026-02-07"))
    assert result.result == checks.REVIEW_REQUIRED and result.exception_type != "DATE MISMATCH"


def test_validation_keeps_a_genuine_date_mismatch_for_an_evidence_resolved_date():
    """Resolved by an explicit DD/MM cue to 7 February: a 2 July claim is a real mismatch, even though
    the other reading of the string would equal the claim."""
    result = checks.check_bill_date(expense_date=D(2026, 7, 2), bill=_Bill(D(2026, 2, 7), "07/02/2026", "2026-02-07|2026-07-02"))
    assert result.result == checks.EXCEPTION and result.exception_type == "DATE MISMATCH"


def test_the_claimed_date_printed_elsewhere_on_a_multi_date_bill_is_not_a_mismatch():
    """Real case (hotel folio): arrival 28-JUL-26, departure/invoice 30-JUL-26. The bill's primary date is the
    arrival; a claim dated at departure is supported by a date the document itself prints unambiguously."""
    bill = _Bill(D(2026, 7, 28), "28-JUL-26", "")
    bill.raw_text = "INVOICE 30-07-26 | Arrival 28-JUL-26 | Departure 30-JUL-26"
    result = checks.check_bill_date(expense_date=D(2026, 7, 30), bill=bill)
    assert result.result == checks.PASS and "is printed on the bill" in result.remarks


def test_an_ambiguous_token_that_could_be_the_claimed_date_does_not_rescue_a_mismatch():
    """Real case (fuel slip): printed date 18-06-2026, transaction stamps '08-06-26' (order unproven). The stamp
    could be read as 8 June, but that reading is not established, so the mismatch stands."""
    bill = _Bill(D(2026, 6, 18), "18-06-2026", "")
    bill.raw_text = "Date: 18-06-2026 | TxSt: 08-06-26 07:50:33 | TxEnd: 08-06-26 07:51:56"
    result = checks.check_bill_date(expense_date=D(2026, 6, 8), bill=bill)
    assert result.result == checks.EXCEPTION and result.exception_type == "DATE MISMATCH"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Date -20/07/2026 Time -10:44:44", D(2026, 7, 20)),  # OCR dash where the label's colon was
        ("Receipt >24/07/2026 -10:38:00", D(2026, 7, 24)),
        ("Time of Ride Sep 21st 2026, 7:37 PM", D(2026, 9, 21)),
    ],
)
def test_ocr_punctuation_in_front_of_a_date_does_not_hide_it(text, expected):
    assert [r.value for r in dates.read_dates(text)] == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "Bill No Jul -293763-ORGNL",  # day and year fused: not 29 Jul 3763
        "Payment History\n31-JUL\n30-JUN\n706.82",  # a statement's column heads on adjacent lines: not 31 Jul 2030
        "Order 12 Jun 1875",  # implausible year
    ],
)
def test_text_that_only_resembles_a_date_is_not_read_as_one(text):
    assert [r.value for r in dates.read_dates(text) if r.value is not None] == []


def test_a_leading_dash_does_not_permit_a_date_glued_to_a_longer_number():
    assert [r.value for r in dates.read_dates("07-20-2026") if r.value] == [D(2026, 7, 20)]
    assert [r.raw for r in dates.read_dates("07-20-2026")] == ["07-20-2026"]  # not also "20-2026..."


def _bill_dict(bill_id, bill_date, candidates=(), amount=275.0):
    return {"id": bill_id, "page_number": 5, "amount": amount, "bill_date": bill_date, "date_candidates": list(candidates),
            "vendor": "Neha Taxi", "description": None, "category_guess": "Cab/Taxi", "bill_number": None, "raw_text": None}


CLAIM = {"s_no": 1, "expense_rank": 1, "claimed_amount": 275.0, "expense_date": D(2026, 7, 2), "category": "CAB CHARGES", "expense_details": "ride"}


def test_an_unresolved_date_earns_no_date_credit_even_if_a_candidate_equals_the_claim_date():
    unresolved = _bill_dict(1, None, [D(2026, 7, 2), D(2026, 2, 7)])
    resolved = _bill_dict(2, D(2026, 7, 2))
    score_unresolved, reasons = bill_matcher.score_pair(CLAIM, unresolved)
    score_resolved, _ = bill_matcher.score_pair(CLAIM, resolved)
    assert score_resolved - score_unresolved == 30  # exactly the exact-date credit
    assert any("ambiguous" in r and "no date credit" in r for r in reasons)


def test_the_matcher_never_treats_an_unresolved_date_as_a_strong_exact_date():
    (outcome,) = bill_matcher.allocate_bills([CLAIM], [_bill_dict(1, None, [D(2026, 7, 2), D(2026, 2, 7)])], first_bill_page=1)
    assert not bill_matcher._date_delta(CLAIM, _bill_dict(1, None)) == 0
    assert "Date: exact match" not in " ".join(outcome.reasons)


# ------------------------------------------------------------------ the parser does not pick a format to match the claim


def test_the_reader_has_no_access_to_the_claimed_date():
    """read_dates takes only the document text: the same text always gives the same readings."""
    import inspect

    assert list(inspect.signature(dates.read_dates).parameters) == ["text"]
    assert list(inspect.signature(dates.apply_group_evidence).parameters) == ["texts", "readings"]
    a = dates.read_dates("Paid on 07/02/2026")
    b = dates.read_dates("Paid on 07/02/2026")
    assert a == b and a[0].value is None
