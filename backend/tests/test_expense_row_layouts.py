"""Expense Summary rows in layouts other than the original one-value-per-line form.

The Expense Report's claim list is the authority on which claims exist. Rows are located
by what they CONTAIN (a date, a Policy Master category, a currency amount), never by a
fixed column position; every printed row stays a distinct claim; a row missing a field is
kept for review rather than dropped or guessed; receipt pages never create claims; and a
layout the parser cannot read yields NO rows plus a warning - it never invents any.
"""

from datetime import date

from app.services.extraction import expense_report_parser as erp

D = date


def parse(*page_texts: str):
    pages = [{"page_number": i + 1, "text": t} for i, t in enumerate(page_texts)]
    return erp.extract_expense_lines(pages)


def by_s_no(rows):
    return {r["s_no"]: r for r in rows}


def test_one_line_rows_with_wrapped_description_and_repeated_header_across_pages():
    page1 = (
        "EXPENSE SUMMARY\nS.No  Date  Category  Description  Amount (INR)\n"
        "1  07/07/2026  CAB CHARGES  Uber ride to office  Rs.231.00\n      Alkem plant visit\n"
        "2  08/07/2026  OWN CAR PETROL  fuel for site visit  Rs.1,200.00"
    )
    page2 = (
        "EXPENSE SUMMARY (continued)\nS.No  Date  Category  Description  Amount (INR)\n"
        "3  09/07/2026  LODGING  hotel for two nights  Rs.4,800.00\n"
        "REPORT SUMMARY BY CURRENCY\nTotal Reimbursable Amount\nRs.6,231.00"
    )
    rows, warnings = parse(page1, page2)
    rows = by_s_no(rows)
    assert sorted(rows) == [1, 2, 3] and warnings == []
    assert (rows[1]["expense_date"], rows[1]["category"], rows[1]["claimed_amount"]) == (D(2026, 7, 7), "CAB CHARGES", 231.0)
    assert "Alkem plant visit" in rows[1]["expense_details"]  # wrapped line kept as evidence
    assert rows[3]["source_page"] == 2  # the original page of each row is preserved
    assert rows[2]["claimed_amount"] == 1200.0


def test_labelled_dash_separated_rows():
    text = "EXPENSE SUMMARY\n4 — Date: 14/07/2026 — Category: CAB CHARGES — Details: Uber ride (Vendor) — Amount: Rs. 231.00"
    (row,), warnings = parse(text)
    assert (row["s_no"], row["expense_date"], row["category"], row["claimed_amount"]) == (4, D(2026, 7, 14), "CAB CHARGES", 231.0)


def test_category_first_pipe_separated_rows_with_month_name_dates():
    text = "EXPENSE SUMMARY\n7) CAB CHARGES | 14 Jul 2026 | Uber ride (Vendor) | INR 231.00"
    (row,), _ = parse(text)
    assert (row["s_no"], row["expense_date"], row["category"], row["claimed_amount"]) == (7, D(2026, 7, 14), "CAB CHARGES", 231.0)


def test_columns_in_a_different_order_with_the_serial_printed_last():
    text = "EXPENSE SUMMARY\nDate | Category | Details | Amount | Ref\n14/07/2026 | CAB CHARGES | Uber ride (Vendor) | Rs. 231.00 | #3"
    (row,), warnings = parse(text)
    assert (row["s_no"], row["expense_date"], row["category"], row["claimed_amount"]) == (3, D(2026, 7, 14), "CAB CHARGES", 231.0)
    assert warnings == []  # the serial WAS printed, so nothing is inferred


def test_rows_that_span_two_lines_with_the_amount_on_the_second():
    text = "EXPENSE SUMMARY\n3) 14/07/2026 CAB CHARGES\n     Uber ride (Vendor)  Rs. 231.00"
    (row,), _ = parse(text)
    assert (row["s_no"], row["expense_date"], row["category"], row["claimed_amount"]) == (3, D(2026, 7, 14), "CAB CHARGES", 231.0)
    assert row["amount_source_page"] == 1


def test_a_row_with_no_printed_serial_is_numbered_by_order_and_says_so():
    text = "EXPENSE SUMMARY\n14/07/2026 CAB CHARGES Uber ride Rs. 231.00\n15/07/2026 CAB CHARGES Uber ride Rs. 120.00"
    rows, warnings = parse(text)
    assert [r["s_no"] for r in rows] == [1, 2]
    assert sum("no serial number is printed" in w for w in warnings) == 2


def test_identical_rows_stay_separate_claims():
    line = "{n}  14/07/2026  CAB CHARGES  Uber ride  Rs.500.00"
    rows, _ = parse("EXPENSE SUMMARY\n" + line.format(n=1) + "\n" + line.format(n=2) + "\n" + line.format(n=3))
    assert [r["s_no"] for r in rows] == [1, 2, 3]
    assert len({(r["expense_date"], r["category"], r["claimed_amount"], r["expense_details"]) for r in rows}) == 1


def test_a_malformed_row_is_kept_for_review_not_dropped_or_guessed():
    rows, warnings = parse("EXPENSE SUMMARY\n1  07/07/2026  CAB CHARGES  Uber ride\n2  08/07/2026  CAB CHARGES  Uber ride  Rs.120.00")
    by = by_s_no(rows)
    assert by[1]["status"] == "REVIEW_REQUIRED" and by[1]["claimed_amount"] is None
    assert by[2]["status"] == "EXTRACTED"
    assert any("Row 1" in w and "amount" in w for w in warnings)


def test_a_row_naming_no_policy_category_is_kept_with_the_category_unresolved():
    (row,), warnings = parse("EXPENSE SUMMARY\n1  07/07/2026  Mystery spending  Rs.120.00")
    assert row["category"] is None and row["status"] == "REVIEW_REQUIRED"
    assert any("category" in w for w in warnings)


def test_receipt_pages_never_create_claims():
    """Only the table between the EXPENSE SUMMARY heading and the REPORT SUMMARY footer is read:
    receipt text before or after it - even receipt lines that look exactly like claim rows - is ignored."""
    cover = "Receipt note: 99  01/01/2026  CAB CHARGES  Rs.999.00"
    table = "EXPENSE SUMMARY\n1  07/07/2026  CAB CHARGES  Uber ride  Rs.231.00\nREPORT SUMMARY BY CURRENCY\nTotal Reimbursable Amount\nRs.231.00"
    receipts = "Uber\n5  08/07/2026  CAB CHARGES  Rs.500.00\nTotal 500.00"
    rows, _ = parse(cover, table, receipts)
    assert [r["s_no"] for r in rows] == [1]


def test_the_original_one_value_per_line_layout_still_uses_the_primary_parser():
    text = "EXPENSE SUMMARY\nS.No\nExpense Details\nCategory\nAmount (INR)\n1.\n07/08/2026\nMerchant : TCGLS\nCAB CHARGES\nRainy day, urgent installation\nRs.546.00"
    (row,), _ = parse(text)
    assert (row["s_no"], row["expense_date"], row["category"], row["merchant"], row["claimed_amount"]) == (1, D(2026, 8, 7), "CAB CHARGES", "TCGLS", 546.0)


def test_a_one_label_per_line_form_is_a_known_unsupported_layout_and_yields_no_invented_rows():
    """Documented limitation (measured by the held-out benchmark): a form-style block per claim, one label
    per line, is not read. The honest outcome is no rows and a warning - never fabricated claims."""
    text = "EXPENSE SUMMARY\nSr No: 1\nDate: 14/07/2026\nCategory: CAB CHARGES\nDescription: Uber ride\nAmount: Rs. 231.00\n"
    rows, warnings = parse(text)
    assert rows == []
    assert any("No expense line items" in w for w in warnings)
