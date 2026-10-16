"""US-style receipt dates and whole-rupee amount tolerance (ER-38784).

Real failure these cover (traced against the real report, not assumed):

  * S.No 15 claims Rs.231 on 2026-07-06 and S.No 29 claims Rs.93 on 2026-07-09.
    Both stayed MULTIPLE_CANDIDATES / "BILL MATCH REVIEW REQUIRED" with two
    bills "similarly plausible (score 72)".
  * The amount tolerance was never the problem: money.amounts_match already
    accepts a whole-rupee claim against either neighbouring rupee of a decimal
    bill total (231 vs 231.15 / 231.17, 93 vs 93.21 / 93.31), and the stored
    bill amounts are preserved untouched.
  * The cause is the DATE. These Uber receipts print the payment time
    month-first ("7/6/26 10:22 AM" = 6 July), but the day-first default stored
    it as 7 June (2026-06-07); "7/9/26" became 7 September. The other receipts
    ("7/17/26", "7/21/26") can only be month-first and were read correctly. So
    the claim's true receipt scored no date credit and tied with a bill that
    exactly belongs to a different claim.

The receipt texts below are the OCR text the pipeline stored for the real pages
(driver/plate details are as extracted; none affects an assertion).
"""

from datetime import date
from io import BytesIO

import fitz
import pytest

from app.services.extraction import bill_matcher, dates
from app.services.extraction.bill_parser import parse_bill_text
from app.services.extraction.money import amounts_match
from app.services.validation import checks

UBER_P17 = (  # 231.17 on 6 July (stored as 7 June)
    "Uber\nThanks for riding, Rohit\nWe hope you enjoyed your ride this morning.\nTotal €231.17\n"
    "iv) Congratulations! You have offer on UberMoto.\nSuggested fare 233.17\nPromotion -%2.00\nPayments\n"
    "Cash. %231.17\n7/6/26 10:22 AM\nThis receipt reflects the Suggested Fare (excluding GST) and is not a tax invoice, "
    "but can be used for\nofficial reimbursement purposes.\nTrip details\nBike Saver License Plate:\n"
    "SH 25.19 kilometers, 46 minutes DL3SFC2665\n17"
)
UBER_P25 = (  # 93.21 on 9 July (stored as 7 September)
    "Uber\nThanks for riding, Rohit\nWe hope you enjoyed your ride this morning.\nTotal £93.21\n"
    "iv) Congratulations! You have offer on UberMoto.\nSuggested fare 95.21\nPromotion -%2.00\nPayments\n"
    "Cash. 93.21\n7/9/26 6:29 PM\nThis receipt reflects the Suggested Fare (excluding GST) and is not a tax invoice, "
    "but can be used for\nofficial reimbursement purposes.\nTrip details\nBike Saver License Plate:\n"
    "cy) 11.22 kilometers, 21 minutes UP16EH6317\n25"
)
UBER_P40 = (  # 231.15 on 17 July - unambiguous, proves month-first
    "Uber\nThanks for riding, Rohit\nWe hope you enjoyed your ride this morning.\nTotal %231.15\n"
    "iv) Congratulations! You have offer on UberMoto.\nSuggested fare 233.15\nPromotion -%2.00\nPayments\n"
    "Cash. %231.15\n7/17/26 5:56 PM\nThis receipt reflects the Suggested Fare (excluding GST) and is not a tax invoice, "
    "but can be used for\nofficial reimbursement purposes.\nTrip details\nBike Saver License Plate:\n"
    "cao) 25.67 kilometers, 58 minutes DL1SV9417\n40"
)
UBER_P43 = (  # 93.31 on 21 July - unambiguous, proves month-first
    "Uber\nThanks for riding, Rohit\nWe hope you enjoyed your ride this morning.\nTotal €93.31\n"
    "iv) Congratulations! You have offer on UberMoto.\nSuggested fare 295.31\nPromotion -%2.00\nPayments\n"
    "Cash. 93.31\n7/21/26 9:39 AM\nThis receipt reflects the Suggested Fare (excluding GST) and is not a tax invoice, "
    "but can be used for\nofficial reimbursement purposes.\nTrip details\nBike Saver License Plate:\n"
    "cy) 11.12 kilometers, 23 minutes UP14DE7613\n43"
)

D = date


def _bill(bill_id, page, text, bill_date=None, amount=None):
    parsed = parse_bill_text(text)
    return {
        "id": bill_id,
        "page_number": page,
        "amount": parsed.amount if amount is None else amount,
        "bill_date": bill_date if bill_date is not None else parsed.bill_date,
        "vendor": parsed.vendor,
        "description": parsed.description,
        "category_guess": parsed.category_guess,
        "bill_number": parsed.bill_number,
        "raw_text": text,
    }


def _claim(s_no, amount, day, details="Uber ride"):
    return {
        "s_no": s_no,
        "expense_rank": s_no,
        "claimed_amount": amount,
        "expense_date": day,
        "category": "CAB CHARGES",
        "expense_details": details,
    }


# ====================== amount tolerance: already correct, now pinned ======================


@pytest.mark.parametrize(
    "claimed,bill,expected",
    [
        (231, 231.15, True), (231, 231.17, True), (93, 93.21, True), (93, 93.31, True),
        (232, 231.15, True),  # the employee rounded UP to the next whole rupee: also accepted (documented rule)
        (231, 231.99, True),
        (94, 93.21, True),  # ceiling of 93.21
        (230, 231.15, False), (233, 231.15, False), (92, 93.21, False), (95, 93.21, False),
        (231, 232.15, False), (500, 7500, False), (231.0, 231.0, True),
    ],
)
def test_whole_rupee_tolerance_is_exactly_the_two_neighbouring_rupees(claimed, bill, expected):
    assert amounts_match(claimed, bill) is expected


def test_matching_never_overwrites_the_claimed_or_extracted_amounts():
    bills = [_bill(1, 17, UBER_P17, D(2026, 7, 6))]
    claims = [_claim(15, 231.0, D(2026, 7, 6))]
    bill_matcher.allocate_bills(claims, bills, first_bill_page=4)
    assert claims[0]["claimed_amount"] == 231.0
    assert bills[0]["amount"] == 231.17


# ====================== the real cause: month-first receipt dates ======================


def test_stored_dates_of_the_real_receipts_before_the_fix_are_day_first_misreadings():
    """Characterises the defect: parse_bill_text alone still reads 7/6/26 as 7 June."""
    assert parse_bill_text(UBER_P17).bill_date == D(2026, 6, 7)
    assert parse_bill_text(UBER_P25).bill_date == D(2026, 9, 7)
    assert parse_bill_text(UBER_P40).bill_date == D(2026, 7, 17)  # only month-first is possible
    assert parse_bill_text(UBER_P43).bill_date == D(2026, 7, 21)


def test_convention_is_proven_only_by_a_receipt_that_cannot_be_read_the_other_way():
    assert dates.us_timestamp_convention([UBER_P17, UBER_P25, UBER_P40, UBER_P43]) == "MDY"
    assert dates.us_timestamp_convention([UBER_P17, UBER_P25]) is None  # ambiguous receipts prove nothing
    assert dates.us_timestamp_convention([UBER_P40]) == "MDY"
    day_first_proof = "Paid 13/7/26 10:22 AM"
    assert dates.us_timestamp_convention([UBER_P40, day_first_proof]) is None  # the report contradicts itself
    assert dates.us_timestamp_convention([day_first_proof]) == "DMY"
    assert dates.us_timestamp_convention([None, ""]) is None


def test_ambiguous_receipts_are_read_month_first_once_the_format_is_proven():
    convention = dates.us_timestamp_convention([UBER_P17, UBER_P25, UBER_P40, UBER_P43])
    assert dates.month_first_reading(UBER_P17, D(2026, 6, 7), convention) == D(2026, 7, 6)
    assert dates.month_first_reading(UBER_P25, D(2026, 9, 7), convention) == D(2026, 7, 9)


@pytest.mark.parametrize(
    "text,parsed,convention",
    [
        (UBER_P17, D(2026, 6, 7), None),  # no proof of month-first
        (UBER_P17, D(2026, 6, 7), "DMY"),
        (UBER_P40, D(2026, 7, 17), "MDY"),  # already unambiguous: nothing to correct
        ("Uber\nCash 7/7/26 10:22 AM", D(2026, 7, 7), "MDY"),  # symmetric
        ("Date: August 7, 2026\nCash 7/6/26 10:22 AM", D(2026, 8, 7), "MDY"),  # another date on the page
        ("Indian Oil\nDate : 07/06/26\n10:22", D(2026, 6, 7), "MDY"),  # not the US timestamp shape
        (UBER_P17, None, "MDY"),
        (UBER_P17, D(2026, 3, 3), "MDY"),  # stored date is not the day-first misreading
    ],
)
def test_no_reinterpretation_without_proof_or_when_the_date_is_not_a_lone_ambiguous_timestamp(text, parsed, convention):
    assert dates.month_first_reading(text, parsed, convention) is None


# ====================== matching with the corrected dates ======================


def test_s_no_15_gets_its_own_receipt_once_the_date_is_read_correctly():
    """Rs.231 claimed 6 July; Rs.231.17 receipt is 6 July; Rs.231.15 receipt (17 July)
    belongs to the 17 July claim - no tie."""
    bills = [_bill(46, 17, UBER_P17, D(2026, 7, 6)), _bill(61, 40, UBER_P40)]
    s15, s47 = bill_matcher.allocate_bills(
        [_claim(15, 231.0, D(2026, 7, 6)), _claim(47, 231.0, D(2026, 7, 17))], bills, first_bill_page=4
    )
    assert (s15.status, s15.matched_bill_id) == ("MATCHED", 46)
    assert (s47.status, s47.matched_bill_id) == ("MATCHED", 61)


def test_s_no_29_gets_its_own_receipt_once_the_date_is_read_correctly():
    bills = [_bill(50, 25, UBER_P25, D(2026, 7, 9)), _bill(64, 43, UBER_P43)]
    s29, s51 = bill_matcher.allocate_bills(
        [_claim(29, 93.0, D(2026, 7, 9)), _claim(51, 93.0, D(2026, 7, 21))], bills, first_bill_page=4
    )
    assert (s29.status, s29.matched_bill_id) == ("MATCHED", 50)
    assert (s51.status, s51.matched_bill_id) == ("MATCHED", 64)


def test_with_the_misread_dates_the_claims_tie_which_is_what_the_screenshot_showed():
    bills = [_bill(46, 17, UBER_P17), _bill(61, 40, UBER_P40)]  # 17 stored as 7 June
    s15, _ = bill_matcher.allocate_bills(
        [_claim(15, 231.0, D(2026, 7, 6)), _claim(47, 231.0, D(2026, 7, 17))], bills, first_bill_page=4
    )
    assert s15.status == "MULTIPLE_CANDIDATES"


# ====================== near-ties settled by necessity (exclusion) ======================


def _uber(bill_id, page, amount, day):
    return {
        "id": bill_id, "page_number": page, "amount": amount, "bill_date": day, "vendor": "Uber",
        "description": "Uber", "category_guess": "Cab/Taxi", "bill_number": None, "raw_text": None,
    }


def test_er38784_s33_takes_the_only_bill_left_because_s38_can_only_use_the_other():
    """Rs.96 (S.No 33) and Rs.95 (S.No 38), both 13 July; receipts Rs.96.21 and Rs.95.18.
    The Rs.95.18 receipt fits BOTH claims (Rs.96 is its ceiling), but Rs.96.21 cannot support
    Rs.95, so Rs.95.18 is S.No 38's only possible bill and S.No 33 must be Rs.96.21."""
    bills = [_uber(29, 29, 96.21, D(2026, 7, 13)), _uber(32, 32, 95.18, D(2026, 7, 13))]
    s33, s38 = bill_matcher.allocate_bills(
        [_claim(33, 96.0, D(2026, 7, 13)), _claim(38, 95.0, D(2026, 7, 13))], bills, first_bill_page=4
    )
    assert (s33.status, s33.matched_bill_id) == ("MATCHED", 29)
    assert (s38.status, s38.matched_bill_id) == ("MATCHED", 32)


def test_er38784_s69_takes_the_only_bill_left_because_s65_can_only_use_the_other():
    bills = [_uber(52, 52, 95.36, D(2026, 7, 24)), _uber(54, 54, 96.54, D(2026, 7, 24))]
    s65, s69 = bill_matcher.allocate_bills(
        [_claim(65, 95.0, D(2026, 7, 24)), _claim(69, 96.0, D(2026, 7, 24))], bills, first_bill_page=4
    )
    assert (s65.status, s65.matched_bill_id) == ("MATCHED", 52)
    assert (s69.status, s69.matched_bill_id) == ("MATCHED", 54)


def test_exclusion_result_does_not_depend_on_report_order():
    bills = [_uber(29, 29, 96.21, D(2026, 7, 13)), _uber(32, 32, 95.18, D(2026, 7, 13))]
    s38, s33 = bill_matcher.allocate_bills(
        [_claim(38, 95.0, D(2026, 7, 13)), _claim(33, 96.0, D(2026, 7, 13))], bills, first_bill_page=4
    )
    assert (s33.matched_bill_id, s38.matched_bill_id) == (29, 32)


def test_exclusion_explains_itself():
    bills = [_uber(29, 29, 96.21, D(2026, 7, 13)), _uber(32, 32, 95.18, D(2026, 7, 13))]
    s33, _ = bill_matcher.allocate_bills(
        [_claim(33, 96.0, D(2026, 7, 13)), _claim(38, 95.0, D(2026, 7, 13))], bills, first_bill_page=4
    )
    assert "S.No 38" in s33.reasons[0] and "page 29" in s33.reasons[0]


def test_no_exclusion_when_the_other_claim_has_another_option():
    """S.No 38 could also use a third Rs.95.40 receipt, so Rs.95.18 is not spoken for:
    nothing is forced, the Rs.96 claim stays ambiguous."""
    bills = [_uber(29, 29, 96.21, D(2026, 7, 13)), _uber(32, 32, 95.18, D(2026, 7, 13)), _uber(33, 33, 95.40, D(2026, 7, 13))]
    s33, s38 = bill_matcher.allocate_bills(
        [_claim(33, 96.0, D(2026, 7, 13)), _claim(38, 95.0, D(2026, 7, 13))], bills, first_bill_page=4
    )
    assert s33.status != "MATCHED"


def test_exclusion_never_picks_a_bill_without_a_readable_amount():
    bills = [_uber(32, 32, 95.18, D(2026, 7, 13)), _uber(34, 34, None, D(2026, 7, 13))]
    s33, s38 = bill_matcher.allocate_bills(
        [_claim(33, 96.0, D(2026, 7, 13)), _claim(38, 95.0, D(2026, 7, 13))], bills, first_bill_page=4
    )
    assert s33.matched_bill_id != 34


class _CheckBill:
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


def test_a_near_amount_never_overrides_a_genuinely_different_date():
    """Rs.231.15 receipt dated 17 July cannot support a 6 July claim just because
    the amount is within a rupee: the date check still raises DATE MISMATCH."""
    receipt = _bill(61, 40, UBER_P40)
    result = checks.check_bill_date(expense_date=D(2026, 7, 6), bill=_CheckBill(receipt))
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "DATE MISMATCH"


def test_an_unambiguous_month_name_date_is_never_reinterpreted():
    text = UBER_P17.replace("7/6/26 10:22 AM", "Jun 7, 2026 10:22 AM")
    assert parse_bill_text(text).bill_date == D(2026, 6, 7)
    assert dates.month_first_reading(text, D(2026, 6, 7), "MDY") is None
    result = checks.check_bill_date(expense_date=D(2026, 7, 6), bill=_CheckBill(_bill(46, 17, text)))
    assert result.exception_type == "DATE MISMATCH"


# ====================== end-to-end through the real pipeline ======================


def _pdf(pages: list[list[str]]) -> bytes:
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 40
        for line in lines:
            page.insert_text((40, y), line, fontsize=9)
            y += 13
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _er_with_receipts(rows: list[str], receipts: list[str]) -> bytes:
    page1 = [
        "Expense Report", "ER-99777", "Amount to be Reimbursed", "Rs.462.00", "Report", "July", "Submitted By",
        "TEST EMPLOYEE", "test@spincotech.com", "Submitted On", "01/08/2026", "EXPENSE SUMMARY", "S.No",
        "Expense Details", "Category", "Amount (INR)", *rows,
        "REPORT SUMMARY BY CURRENCY", "Total Reimbursable Amount", "Rs.462.00", "TEST EMPLOYEE ( T001 )",
    ]
    # A neutral page between receipts, as the real report has (other pages sit between
    # pages 17 and 40): adjacent receipts would otherwise be merged by bill_grouping
    # because both yield the same bill-number text, an unrelated, pre-existing quirk.
    filler = ["Supporting documents continue on the next page.", "Reference only."]
    pages = [page1]
    for index, receipt in enumerate(receipts):
        if index:
            pages.append(filler)
        pages.append(receipt.split("\n"))
    return _pdf(pages)


def _rows():
    return [
        "1.", "06/07/2026", "Merchant : Uber", "CAB CHARGES", "Uber ride to office", "Rs.231.00",
        "2.", "17/07/2026", "Merchant : Uber", "CAB CHARGES", "Uber ride to site", "Rs.231.00",
    ]


def _upload_and_read(client, pdf: bytes):
    resp = client.post("/api/uploads/expense-reports", files=[("files", ("er.pdf", pdf, "application/pdf"))])
    assert resp.status_code == 200, resp.text
    report_id = int(resp.json()["results"][0]["document_id"].split(":")[1])
    expenses = {e["s_no"]: e for e in client.get(f"/api/expense-reports/{report_id}/expenses").json()}
    bills = {b["page_number"]: b for b in client.get(f"/api/expense-reports/{report_id}/bills").json()}
    return expenses, bills


def test_pipeline_reads_month_first_receipts_and_matches_each_claim_to_its_own_receipt(client):
    expenses, bills = _upload_and_read(client, _er_with_receipts(_rows(), [UBER_P17, UBER_P40]))
    page_a, page_b = sorted(bills)
    assert bills[page_a]["bill_date"] == "2026-07-06"  # corrected from 2026-06-07
    assert bills[page_a]["amount"] == 231.17  # extracted amount preserved, not rounded to 231
    assert "month-first" in (bills[page_a]["extraction_notes"] or "")
    assert bills[page_b]["bill_date"] == "2026-07-17"
    # each claim is matched to ITS OWN receipt (the matched bill's own amount/date prove which)
    assert expenses[1]["bill_match_status"] == "MATCHED"
    assert (expenses[1]["bill_amount"], expenses[1]["bill_date"]) == (231.17, "2026-07-06")
    assert expenses[2]["bill_match_status"] == "MATCHED"
    assert (expenses[2]["bill_amount"], expenses[2]["bill_date"]) == (231.15, "2026-07-17")
    assert expenses[1]["claimed_amount"] == 231.0  # claim untouched


def test_pipeline_leaves_an_ambiguous_date_alone_when_nothing_proves_the_format(client):
    expenses, bills = _upload_and_read(client, _er_with_receipts(_rows()[:6], [UBER_P17]))
    (page,) = bills
    assert bills[page]["bill_date"] == "2026-06-07"  # not guessed from the claim date
    assert "month-first" not in (bills[page]["extraction_notes"] or "")
