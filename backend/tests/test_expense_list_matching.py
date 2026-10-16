"""Expense-list-aware bill matching and the Rs.4,797 flight classification.

Real failure these cover (ER-41115, traced against the real PDF, not assumed):

  * The Expense Summary (page 2) lists two separate Rs.500 CAB CHARGES claims on
    2026-08-19 (S.No 17, 18) and further Rs.500 claims on 08-20 / 08-21. Their
    receipts are PhonePe/Google Pay screenshots (pages 31, 32, 36, 39) that each
    show "Rs.500" and spell it out as "Rupees Five Hundred Only". OCR read the
    rupee glyph as a leading digit, so every one of those pages was stored as
    Rs.7,500 - and each Rs.500 claim was reported MISSING BILL (closest candidate
    "score 22/25 needed"), or compared against an unrelated Lodging page.
  * Row 15 (Rs.4,797, 19/08) reads "LODGING (LODGING)" in the source PDF with the
    details "Air ticket from CCU to Bagdogra"; its bill (page 25) is an Air India
    Express e-ticket. The claim really is categorised LODGING, so CATEGORY
    MISMATCH is a genuine employee categorisation error and must be kept.

Fixtures reproduce the real report structure: Expense Summary rows go through
the real row parser, and the UPI pages' OCR text is the text the pipeline stored
(third-party payee names are left as they were; none affects an assertion).
"""

from datetime import date

import pytest

from app.services.category_map import normalize_category
from app.services.extraction import bill_matcher, expense_report_parser
from app.services.extraction.bill_parser import _find_amount, parse_bill_text
from app.services.extraction.money import parse_amount_in_words
from app.services.validation import checks

# ---------- real OCR text of the UPI receipts (ER-41115) ----------

UPI_P31 = (
    "Amount\n7500 @\nRupees Five Hundred Only\n£5 Money Transfer Edit\nTo\nSara Kumari Urow @ SU\n"
    "UPI ID: 9002755932@axl on @ phonePe\nFrom\nDipak Kumar Pal\nUPI ID: 9331952798@ptyes\nICICI Bank - 2910\n"
    "Paid at 03:09 PM, 19 Aug 2026\nUPI Ref No: 312790422420 Copy\nPayment Details V\n31"
)
UPI_P32 = (
    "Amount\n7500 @\nRupees Five Hundred Only\n£5 Money Transfer Edit\nTo\nSara Kumari Urow @ SU\n"
    "UPI ID: 9002755932@axI on © phonePe\nFrom\nDipak Kumar Pal\nUPI ID: 9331952798@ptyes\nICICI Bank - 2910 @\n"
    "Paid at 07:11 PM, 19 Aug 2026\nUPI Ref No: 312809017941 Copy\nPayment Details V 32"
)
UPI_P36 = (
    "Amount\n7500 @\nRupees Five Hundred Only\n&5 Money Transfer Edit\nTo\nSara Kumari Urow @ SU\n"
    "UPI ID: 9002755932@axl on © PhonePe\nPay Again View History\nFrom\nDipak Kumar Pal\nUPI ID: 9331952798@ptyes\n"
    "ICICI Bank - 2910 #\nPaid at 07:57 PM, 20 Aug 2026\nUPI Ref No: 312884580983 Copy\nPayment Details “ 36"
)
UPI_P39 = (
    "Amount\n7500 &\nRupees Five Hundred Only\n£5 Money Transfer Edit\nTo\nKeshar Tamang & KT\n"
    "UPI ID: trajutamang1992@oksbi\nOn @ Google Pay\nPay Again View History\nFrom\nDipak Kumar Pal 63\n"
    "UPI ID: ******2798@ptyes\nICICI Bank - 2910 #@\nPaid at 10:03 AM, 21 Aug 2026\nUPI Ref No: 213722805888 Copy\n39"
)
UPI_P37_GENUINE_1460 = (
    "< Paid Successfully Share Help\nAmount\n1,460 @\nRupees One Thousand Four Hundred\nSixty Only\n<< Travel Edit\n"
    "Split this Payment\nTo\nPalm Grove Suites Inn Q\nUPI ID: eazypay.584913851@icici\nFrom\nDipak Kumar Pal 63\n"
    "UPI ID: 9331952798@ptyes\nICICI Bank - 2910\nPaid at 08:01 AM, 21 Aug 2026\nUPI Ref No: 612577388527 Copy"
)
AIR_INDIA_EXPRESS_TICKET = """Trip ID : 260818517608
Mr Dipak Kumar Pal
4,797
Kolkata to Bagdogra
Wed, 19 Aug 2026
Air India Express
IX - 1248
Fare type: CLASSIC
CCU 07:45
09:05 IXB
TRAVELLERS
AIRLINE PNR
TICKET NO.
FARE BREAKUP
Base Fare: Rs. 3,333
Airline Fuel Charge: Rs. 299
Other charges: Rs. 1,545
Discounts And Cashbacks: Rs. -425
Total Fare: Rs. 4,797
Air India Express helpline 044 24301930
"""

# ---------- real Expense Summary rows (ER-41115 page 2, rows 14-18) ----------

EXPENSE_SUMMARY_PAGE_2 = """EXPENSE SUMMARY
S.No
Expense Details
Category
Amount (INR)
14.
19/08/2026
Merchant : Kolkata Airport
CAB CHARGES
Residence to Kolkata airport
Rs.388.00
15.
19/08/2026
Merchant : Bagdigra Airport
LODGING (LODGING)
Air ticket from CCU to Bagdogra
Rs.4,797.00
16.
19/08/2026
Merchant : Sikkim Hotel
CAB CHARGES
Bagdogra Airport to Majitar Hotel
Rs.4,000.00
17.
19/08/2026
Merchant : Alkem U-5
CAB CHARGES
Cab Hotel to Alkem-5
Rs.500.00
18.
19/08/2026
Merchant : Hotel Majitar
CAB CHARGES
Cab Alkem-5 to Hotel Majitar
Rs.500.00
21.
21/08/2026
Merchant : ALKEM
CAB CHARGES
Cab hotel to Alkem
Rs.500.00
REPORT SUMMARY BY CURRENCY
"""


def _claims(page_text: str = EXPENSE_SUMMARY_PAGE_2) -> dict[int, dict]:
    rows, warnings = expense_report_parser.extract_expense_lines([{"page_number": 2, "text": page_text}])
    assert warnings == []
    return {
        r["s_no"]: {
            "s_no": r["s_no"],
            "expense_rank": r["s_no"],
            "claimed_amount": r["claimed_amount"],
            "expense_date": r["expense_date"],
            "category": normalize_category(r["category"]),
            "expense_details": r["expense_details"],
        }
        for r in rows
    }


def _bill_from_text(bill_id: int, page: int, text: str) -> dict:
    parsed = parse_bill_text(text)
    return {
        "id": bill_id,
        "page_number": page,
        "amount": parsed.amount,
        "bill_date": parsed.bill_date,
        "vendor": parsed.vendor,
        "description": parsed.description,
        "category_guess": parsed.category_guess,
        "bill_number": parsed.bill_number,
        "raw_text": text,
    }


def _bill(bill_id, page, amount, bill_date, vendor=None, category_guess=None):
    return {
        "id": bill_id,
        "page_number": page,
        "amount": amount,
        "bill_date": bill_date,
        "vendor": vendor,
        "description": vendor,
        "category_guess": category_guess,
        "bill_number": None,
        "raw_text": None,
    }


def _claim(s_no, amount, day, category="CAB CHARGES", details="cab ride"):
    return {
        "s_no": s_no,
        "expense_rank": s_no,
        "claimed_amount": amount,
        "expense_date": day,
        "category": category,
        "expense_details": details,
    }


D19, D20, D21 = date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21)


class _CheckBill:
    """Attribute view of a bill dict, as validation/checks.py reads a Bill row."""

    def __init__(self, bill: dict):
        self.page_number = bill["page_number"]
        self.page_number_end = None
        self.amount = bill["amount"]
        self.bill_date = bill["bill_date"]
        self.vendor = bill["vendor"]
        self.category_guess = bill["category_guess"]
        self.raw_text = bill.get("raw_text")
        self.extraction_status = "EXTRACTED"
        self.extraction_confidence = "MEDIUM"
        self.catering_amount = None
        self.document_type = None


def _matched_ids(outcomes):
    return [o.matched_bill_id for o in outcomes if o is not None and o.status == "MATCHED"]


# ====================== Amount in words (the root cause) ======================


@pytest.mark.parametrize("page_text,page_date", [(UPI_P31, D19), (UPI_P32, D19), (UPI_P36, D20), (UPI_P39, D21)])
def test_real_upi_receipts_read_as_rs_500_not_7500(page_text, page_date):
    """The rupee glyph read as a leading '7' turned Rs.500 into Rs.7,500."""
    bill = parse_bill_text(page_text)
    assert bill.amount == 500.0
    assert bill.bill_date == page_date
    assert bill.confidence == "MEDIUM"  # never HIGH: the printed figure itself was not read cleanly
    assert bill.status == "EXTRACTED"
    assert any("amount in words" in w and "7,500" in w for w in bill.warnings)


def test_matching_printed_and_written_amounts_are_left_alone():
    amount, confidence, warnings = _find_amount(UPI_P37_GENUINE_1460)
    assert amount == 1460.0
    assert not any("disagrees" in w or "amount in words" in w for w in warnings)


def test_a_genuine_rs_7500_receipt_is_not_rewritten():
    """Figure and words agree -> a real Rs.7,500 bill stays Rs.7,500."""
    text = "Amount\n7500\nRupees Seven Thousand Five Hundred Only\nPaid at 10:03 AM, 21 Aug 2026"
    assert _find_amount(text)[0] == 7500.0


def test_unrelated_disagreement_between_figure_and_words_needs_review():
    """Not the 'symbol read as a digit' pattern: never silently pick either
    reading - keep the printed figure but send the bill to review."""
    text = "Amount\n900\nRupees Five Hundred Only\nPaid at 10:03 AM, 21 Aug 2026"
    amount, confidence, warnings = _find_amount(text)
    assert amount == 900.0
    assert confidence == "LOW"
    assert any("disagrees with the amount in words" in w for w in warnings)
    assert parse_bill_text(text).status == "REVIEW_REQUIRED"


def test_amount_in_words_alone_never_creates_an_amount():
    """An unreadable figure stays unreadable (review) - the words are only used
    to check a figure that WAS read, never to manufacture a confirmable amount."""
    text = "Money Transfer\nRupees Five Hundred Only\nPaid at 10:03 AM, 21 Aug 2026"
    assert _find_amount(text)[0] is None
    assert parse_bill_text(text).status == "REVIEW_REQUIRED"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Rupees Five Hundred Only", 500.0),
        ("Five Hundred Rupees Only", 500.0),
        ("Rupees One Thousand Four Hundred\nSixty Only", 1460.0),
        ("Rupees Two Thousand and Fifty Only", 2050.0),
        ("Rupees One Lakh Twenty-Five Thousand Only", 125000.0),
        ("Rupees Ten Crore Only", 100000000.0),
        ("Rupees Five Hundred and Fifty Paise Only", None),  # paise: ambiguous, never guessed
        ("pay rupees to the bearer only", None),  # not number words
        ("Rupees Five Hundred Only ... Rupees Seven Hundred Only", None),  # conflicting lines
        ("no words here", None),
        (None, None),
    ],
)
def test_parse_amount_in_words(text, expected):
    assert parse_amount_in_words(text) == expected


# ====================== The expense list is the authority ======================


def test_expense_list_rows_are_kept_as_separate_claims_with_their_row_numbers():
    claims = _claims()
    assert sorted(claims) == [14, 15, 16, 17, 18, 21]
    # Two separate, identical-looking Rs.500 cab claims on the same date stay two rows.
    assert (claims[17]["claimed_amount"], claims[17]["expense_date"], claims[17]["category"]) == (500.0, D19, "CAB CHARGES")
    assert (claims[18]["claimed_amount"], claims[18]["expense_date"], claims[18]["category"]) == (500.0, D19, "CAB CHARGES")
    assert claims[17]["expense_details"] != claims[18]["expense_details"]


# ====================== Issue 1: repeated Rs.500 claims ======================


def test_two_rs500_cab_claims_on_different_dates_each_get_their_own_bill():
    bills = [_bill_from_text(25, 31, UPI_P31), _bill_from_text(31, 39, UPI_P39)]
    claims = _claims()
    s17, s21 = bill_matcher.allocate_bills([claims[17], claims[21]], bills, first_bill_page=4)
    assert (s17.status, s17.matched_bill_id) == ("MATCHED", 25)  # 19 Aug receipt
    assert (s21.status, s21.matched_bill_id) == ("MATCHED", 31)  # 21 Aug receipt


def test_two_rs500_cab_claims_on_the_same_date_each_get_a_distinct_bill():
    """The real S.No 17 / 18 case: two claims, two receipts, one date, one amount."""
    bills = [_bill_from_text(25, 31, UPI_P31), _bill_from_text(26, 32, UPI_P32)]
    claims = _claims()
    s17, s18 = bill_matcher.allocate_bills([claims[17], claims[18]], bills, first_bill_page=4)

    assert s17.status == s18.status == "MATCHED"
    assert {s17.matched_bill_id, s18.matched_bill_id} == {25, 26}  # distinct bills, nobody MISSING, nobody shared
    # document order: S.No 17 <-> page 31, S.No 18 <-> page 32
    assert (s17.matched_bill_id, s18.matched_bill_id) == (25, 26)
    # the audit trail says exactly what happened
    assert "separate claims" in s17.reasons[0] and "S.No 17, S.No 18" in s17.reasons[0]
    assert "pages 31, 32" in s17.reasons[0]


def test_same_date_pairing_does_not_depend_on_report_order():
    bills = [_bill_from_text(25, 31, UPI_P31), _bill_from_text(26, 32, UPI_P32)]
    claims = _claims()
    s18, s17 = bill_matcher.allocate_bills([claims[18], claims[17]], bills, first_bill_page=4)
    assert (s17.matched_bill_id, s18.matched_bill_id) == (25, 26)
    assert s17.status == s18.status == "MATCHED"


def test_three_same_date_claims_but_only_two_bills_are_not_force_paired():
    """k claims need exactly k equal bills. With one claim too many the evidence
    cannot say which claim has no bill, so nothing is confidently assigned."""
    bills = [_bill_from_text(25, 31, UPI_P31), _bill_from_text(26, 32, UPI_P32)]
    claims = [_claim(17, 500.0, D19), _claim(18, 500.0, D19), _claim(19, 500.0, D19)]
    outcomes = bill_matcher.allocate_bills(claims, bills, first_bill_page=4)
    assert _matched_ids(outcomes) == []
    assert all(o.status == "MULTIPLE_CANDIDATES" for o in outcomes)


def test_three_same_date_bills_for_two_claims_are_not_force_paired():
    bills = [_bill_from_text(25, 31, UPI_P31), _bill_from_text(26, 32, UPI_P32), _bill(27, 33, 500.0, D19, "UPI payee C")]
    outcomes = bill_matcher.allocate_bills([_claim(17, 500.0, D19), _claim(18, 500.0, D19)], bills, first_bill_page=4)
    assert _matched_ids(outcomes) == []


def test_same_amount_in_different_categories_is_never_merged():
    """Rs.500 cab and Rs.500 fuel on one day: separate claims, each matched on
    its own category evidence, never to the other's bill."""
    bills = [_bill(1, 10, 500.0, D19, "Metro Cabs", "Cab/Taxi"), _bill(2, 11, 500.0, D19, "Bharat Petroleum", "Fuel")]
    cab, fuel = bill_matcher.allocate_bills(
        [_claim(1, 500.0, D19, "CAB CHARGES", "airport cab"), _claim(2, 500.0, D19, "OWN CAR PETROL", "fuel")],
        bills,
        first_bill_page=4,
    )
    assert (cab.status, cab.matched_bill_id) == ("MATCHED", 1)
    assert (fuel.status, fuel.matched_bill_id) == ("MATCHED", 2)


def test_rs500_claim_with_only_a_different_amount_candidate_is_not_matched():
    """Same date, but the bill says Rs.7,500 (a real, different amount - not an
    OCR misread, nothing in words disagrees): amount-equals-date is not enough."""
    bills = [_bill(1, 31, 7500.0, D19, "Sara Kumari Urow @ SU")]
    (outcome,) = bill_matcher.allocate_bills([_claim(17, 500.0, D19)], bills, first_bill_page=4)
    assert outcome.status != "MATCHED"
    assert outcome.matched_bill_id is None


def test_unreadable_amount_candidate_stays_visible_and_the_amount_check_requires_review():
    """The unreadable-amount page is kept as a visible candidate, but a missing
    amount is never evidence: the Bill Amount Check returns REVIEW_REQUIRED, so
    the claim cannot be verified on it."""
    page = _bill(23, 28, None, D19, "TAMANG TAXI SERVICE", "Cab/Taxi")
    (outcome,) = bill_matcher.allocate_bills([_claim(17, 500.0, D19)], [page], first_bill_page=4)
    assert outcome.candidate_bill_id == 23  # visible to the auditor
    assert 23 in [c.bill_id for c in outcome.candidates]

    result = checks.check_bill_amount(claimed_amount=500.0, bill=_CheckBill(page), bill_match_reasons="; ".join(outcome.reasons))
    assert result.result == checks.REVIEW_REQUIRED
    assert result.result != checks.PASS


def test_unreadable_amount_bill_is_never_used_to_pair_interchangeable_claims():
    """Two Rs.500 same-day claims, one readable receipt and one unreadable one:
    the unreadable page may be the second claim's bill, but that is a guess -
    neither claim is confirmed, and the readable bill is not shared."""
    bills = [_bill_from_text(25, 31, UPI_P31), _bill(23, 28, None, D19, "TAMANG TAXI SERVICE", "Cab/Taxi")]
    outcomes = bill_matcher.allocate_bills([_claim(17, 500.0, D19), _claim(18, 500.0, D19)], bills, first_bill_page=4)
    assert _matched_ids(outcomes) == []


def test_one_receipt_for_two_independent_same_day_claims_is_genuine_reuse():
    """Two Rs.500 claims, ONE Rs.500 receipt, nothing else on that date: both
    claims stay on the same bill (existing rule), which the Duplicate Bill
    Check then reports - a reused document is the duplicate, not the repeated
    amount."""
    bills = [_bill_from_text(25, 31, UPI_P31)]
    s17, s18 = bill_matcher.allocate_bills([_claim(17, 500.0, D19), _claim(18, 500.0, D19)], bills, first_bill_page=4)
    assert s17.status == s18.status == "MATCHED"
    assert s17.matched_bill_id == s18.matched_bill_id == 25


def test_a_receipt_on_a_different_date_still_raises_date_mismatch():
    """Rs.500 receipt dated 22 Aug for a Rs.500 claim of 19 Aug: the repeated
    amount alone must never verify it - the Bill Date Check keeps its exception."""
    receipt = _bill_from_text(28, 36, UPI_P36.replace("20 Aug 2026", "22 Aug 2026"))
    assert receipt["bill_date"] == date(2026, 8, 22)
    result = checks.check_bill_date(expense_date=D19, bill=_CheckBill(receipt))
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "DATE MISMATCH"


def test_full_er41115_dates_19_to_21_every_rs500_claim_has_its_own_receipt():
    """Whole slice of the real report: S.No 17 and 18 (19/08), 21 (21/08 here)
    against the four UPI receipts plus the other real bills of those days."""
    claims = _claims()
    bills = [
        _bill(20, 24, 388.26, D19, "Total =388.26", "Cab/Taxi"),
        _bill_from_text(21, 25, AIR_INDIA_EXPRESS_TICKET),
        _bill(24, 29, 4000.0, D19, "Mr Sunil Tamang & ( MT"),
        _bill_from_text(25, 31, UPI_P31),
        _bill_from_text(26, 32, UPI_P32),
        _bill_from_text(31, 39, UPI_P39),
    ]
    outcomes = bill_matcher.allocate_bills([claims[n] for n in (14, 15, 16, 17, 18, 21)], bills, first_bill_page=4)
    by_s_no = dict(zip((14, 15, 16, 17, 18, 21), outcomes))
    assert {n: o.matched_bill_id for n, o in by_s_no.items()} == {14: 20, 15: 21, 16: 24, 17: 25, 18: 26, 21: 31}
    assert all(o.status == "MATCHED" for o in outcomes)
    assert len(set(_matched_ids(outcomes))) == 6  # no bill reused


# ====================== Issue 2: the Rs.4,797 air ticket ======================


def test_air_india_express_ticket_is_flight_evidence_from_its_own_content():
    bill = parse_bill_text(AIR_INDIA_EXPRESS_TICKET)
    assert bill.category_guess == "Flight"
    assert bill.amount == 4797.0
    assert bill.bill_date == D19


def test_the_source_row_really_is_lodging_so_the_claim_keeps_that_category():
    """Traced to the source: row 15 prints 'LODGING (LODGING)'. The parser must
    report exactly what the employee wrote, not silently 'fix' it to airfare."""
    claim = _claims()[15]
    assert claim["category"] == "LODGING"
    assert "Air ticket" in claim["expense_details"]


class _Bill:
    def __init__(self, category_guess, page_number=25, vendor="Air India Express"):
        self.category_guess = category_guess
        self.page_number = page_number
        self.vendor = vendor
        self.raw_text = None


def test_lodging_claim_against_the_flight_ticket_is_a_category_mismatch_with_a_specific_reason():
    result = checks.check_category_consistency(
        category="LODGING (LODGING)", bill=_Bill("Flight"), bill_match_confidence="HIGH"
    )
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "CATEGORY MISMATCH"
    assert result.source_page == 25  # source page preserved
    assert "LODGING" in result.remarks and "Flight" in result.remarks and "page 25" in result.remarks
    assert result.details == "Expected evidence: Lodging; bill evidence: Flight"  # audit evidence unchanged


def test_the_same_ticket_claimed_under_airfare_has_no_category_mismatch():
    result = checks.check_category_consistency(category="AIRFARE", bill=_Bill("Flight"), bill_match_confidence="HIGH")
    assert result.result == checks.PASS


def test_full_chain_row_15_lodging_claim_is_matched_to_its_ticket_then_flagged():
    """Expense row -> bill match -> category check, as the pipeline runs them."""
    claim = _claims()[15]
    bills = [_bill_from_text(21, 25, AIR_INDIA_EXPRESS_TICKET)]
    (outcome,) = bill_matcher.allocate_bills([claim], bills, first_bill_page=4)
    assert outcome.matched_bill_id == 21  # amount + date support the claim
    check = checks.check_category_consistency(
        category=claim["category"], bill=_Bill(bills[0]["category_guess"]), bill_match_confidence=outcome.confidence
    )
    assert check.exception_type == "CATEGORY MISMATCH"


def test_full_chain_same_claim_filed_as_airfare_passes_the_category_check():
    claim = {**_claims()[15], "category": "AIRFARE"}
    bills = [_bill_from_text(21, 25, AIR_INDIA_EXPRESS_TICKET)]
    (outcome,) = bill_matcher.allocate_bills([claim], bills, first_bill_page=4)
    assert outcome.matched_bill_id == 21
    check = checks.check_category_consistency(
        category=claim["category"], bill=_Bill(bills[0]["category_guess"]), bill_match_confidence=outcome.confidence
    )
    assert check.result == checks.PASS
