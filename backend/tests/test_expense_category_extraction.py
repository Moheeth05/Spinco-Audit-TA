"""Tests for the P0 Expense Report category-extraction defect: a reference
number (or any other non-category label line) being stored as the claimed
Category instead of the real category, with the real category pushed down
into free-text expense_details.

This is a DIFFERENT defect from the earlier bill-vocabulary work
(bill_parser.py/category_map.py's CATEGORY_KEYWORDS, which classifies a
SUPPORTING BILL/receipt's content) - these tests exercise
expense_report_parser.extract_expense_lines(), which reads the employee's
OWN claimed category from the Expense Summary table.

Root cause (confirmed against the real ER-40513 document, "reahul
report.pdf"): some real layouts print zero or more optional "Label : value"
lines (Merchant, Ref#, and - discovered only once the full 38-page document
was inspected - also Expense Location, PNR Number, Attendees, Start & End
KM) between the date and the real category line. The prior fix only ever
rescued ONE specific label ("Merchant"); any second label line (most
commonly "Ref# : <value>") was consumed as if it WERE the category, storing
a reference number in Expense.category while the real category was pushed
into expense_details as free text.

The fix (expense_report_parser.extract_expense_lines) recognizes the
category line by what it IS - a real, closed, 29-name Policy Master
category (category_map.is_policy_master_category) - rather than by which
line position it happens to occupy, so it generalizes to any label type, not
just the two seen in the first few rows of one real document. Every
synthetic test here uses different report IDs, employee names, dates,
amounts, merchants and reference numbers than the real document, to prove
the fix isn't keyed to that one document's specific values.
"""
from io import BytesIO
from pathlib import Path

import fitz
import pytest

from app.services.category_map import is_policy_master_category
from app.services.extraction import expense_report_parser

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REAL_ER_40513_PDF = REPO_ROOT / "uat-data-test" / "documents" / "expense_reports" / "506d7dbf9383_reahul report.pdf"


def _pdf_from_pages(pages: list[list[str]]) -> bytes:
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=11)
            y += 16
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _er_pdf(
    rows: list[str],
    *,
    er_id: str = "ER-99001",
    employee: str = "TEST EMPLOYEE",
    employee_code: str = "T001",
    submitted_on: str = "01/01/2026",
    total: str = "Rs.100.00",
    second_page_rows: list[str] | None = None,
) -> bytes:
    page1 = [
        "Expense Report",
        er_id,
        "Amount to be Reimbursed",
        total,
        "Report",
        "January",
        "Submitted By",
        employee,
        "test@spincotech.com",
        "Submitted On",
        submitted_on,
        "EXPENSE SUMMARY",
        "S.No",
        "Expense Details",
        "Category",
        "Amount (INR)",
        *rows,
    ]
    pages = [page1]
    if second_page_rows is not None:
        pages.append(list(second_page_rows))
    pages[-1] = pages[-1] + [
        "REPORT SUMMARY BY CURRENCY",
        "Total Reimbursable Amount",
        total,
        f"{employee} ( {employee_code} )",
    ]
    return _pdf_from_pages(pages)


def _upload(client, filename, content):
    return client.post("/api/uploads/expense-reports", files=[("files", (filename, content, "application/pdf"))])


# ---------- Real-document regression (ER-40513, "reahul report.pdf") ----------


@pytest.mark.skipif(not REAL_ER_40513_PDF.exists(), reason="real corpus PDF not present in this checkout")
def test_er40513_row1_matches_expected_ground_truth(client):
    """The exact expected row from the P0 report: date, category, amount,
    merchant, description and reference must all be correct and none of them
    may have been swapped with another field's value."""
    resp = _upload(client, "reahul report.pdf", REAL_ER_40513_PDF.read_bytes())
    assert resp.status_code == 200, resp.text
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]
    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()

    row1 = next(e for e in expenses if e["s_no"] == 1)
    assert row1["expense_date"] == "2026-09-02"
    assert row1["category"] == "OWN CAR PETROL"
    assert row1["claimed_amount"] == 3688.0
    assert row1["merchant"] == "CHANKYA SERVICE STATION"
    assert row1["reference_id"] == "T2609020947244431394997"
    assert row1["expense_details"] == "Petrol"


@pytest.mark.skipif(not REAL_ER_40513_PDF.exists(), reason="real corpus PDF not present in this checkout")
def test_er40513_no_reference_number_ever_stored_as_category(client):
    """Full-document regression guard: across all 28 real rows (which include
    at least 5 distinct optional prefix-label types - Merchant, Ref#,
    Expense Location, PNR Number, Attendees - observed only once the whole
    38-page document was inspected), not one may end up with reference-
    looking or label-looking text stored as its category."""
    resp = _upload(client, "reahul report.pdf", REAL_ER_40513_PDF.read_bytes())
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]
    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()

    assert len(expenses) == 28
    for e in expenses:
        cat = e["category"] or ""
        assert not cat.upper().startswith("REF"), f"s_no={e['s_no']}: reference text stored as category: {cat!r}"
        assert ":" not in cat, f"s_no={e['s_no']}: a 'Label : value' line was stored as category: {cat!r}"
        if e["category"] is not None:
            assert is_policy_master_category(e["category"]), f"s_no={e['s_no']}: category {cat!r} is not a real Policy Master category"


@pytest.mark.skipif(not REAL_ER_40513_PDF.exists(), reason="real corpus PDF not present in this checkout")
@pytest.mark.parametrize(
    "s_no,expected_category,expected_merchant,expected_reference",
    [
        (2, "BATTA", None, None),
        (4, "TOLL TAX CHARGES", None, "27546701876"),
        (12, "LODGING", "M S Ramada Plaza By Wyndham A", "625317630780"),
        (14, "INTERNET CHARGES", "Jio", None),
        (17, "AIRFARE", "EASEMYTRIP", None),
        (24, "CAB CHARGES", "Ani Technologies Pvt. Ltd.", "CIQIKQTUU1660"),
    ],
)
def test_er40513_spot_check_rows_with_varied_prefix_line_combinations(client, s_no, expected_category, expected_merchant, expected_reference):
    """Each of these rows exercises a different real combination of optional
    prefix lines (none, Ref#-only, Merchant+continuation+Ref#+Attendees,
    Merchant+Expense Location, Merchant+Expense Location+PNR+Start&End KM,
    Merchant+Ref#+Expense Location) - manually verified against the original
    Expense Summary table."""
    resp = _upload(client, "reahul report.pdf", REAL_ER_40513_PDF.read_bytes())
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]
    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    row = next(e for e in expenses if e["s_no"] == s_no)
    assert row["category"] == expected_category
    assert row["merchant"] == expected_merchant
    assert row["reference_id"] == expected_reference


# ---------- Unseen-variation tests: different IDs/names/dates/amounts/merchants/refs ----------


@pytest.mark.parametrize(
    "er_id,employee,date,merchant,ref,category,details,amount",
    [
        ("ER-77213", "PRIYA MENON", "14/03/2026", "SHELL PETROL PUMP", "TXN88213744", "OWN CAR PETROL", "Fuel for site visit", "Rs.2,145.00"),
        ("ER-51208", "ARJUN NAIR", "22/05/2026", "SPEEDY COURIERS LTD", "AWB99021873", "COURIER CHARGES", "Document courier", "Rs.320.00"),
        ("ER-60944", "KAVYA IYER", "03/07/2026", "GRAND RESIDENCY HOTEL", "RES44109823", "LODGING", "Client meeting stay", "Rs.6,720.00"),
        ("ER-88102", "VIKRAM DESHPANDE", "19/08/2026", "SPEEDWAY FASTAG RECHARGE", "FT20394857", "TOLL TAX CHARGES", "Highway toll recharge", "Rs.400.00"),
    ],
)
def test_unseen_reports_with_merchant_and_reference_lines(client, er_id, employee, date, merchant, ref, category, details, amount):
    """Same general layout as the real document (date / Merchant: / Ref#: /
    category / description / amount) but every single value - report ID,
    employee, date, merchant, reference, category, amount - differs from
    both the real document and every other case here, proving the parser
    identifies the category by what it IS, not by matching known values."""
    pdf_bytes = _er_pdf(
        [
            "1.",
            date,
            f"Merchant : {merchant}",
            f"Ref# : {ref}",
            category,
            details,
            amount,
        ],
        er_id=er_id,
        employee=employee,
        total=amount,
    )
    resp = _upload(client, f"{er_id}.pdf", pdf_bytes)
    assert resp.status_code == 200, resp.text
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]
    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    row = expenses[0]
    assert row["category"] == category
    assert row["merchant"] == merchant
    assert row["reference_id"] == ref
    assert row["expense_details"] == details


def test_unseen_report_with_unfamiliar_prefix_label_types(client):
    """Proves the fix generalizes beyond the two label types (Merchant/Ref#)
    that happen to appear in the first few rows of the real document - these
    label types (Expense Location, PNR Number, Attendees, Start & End KM)
    were only discovered deeper into that same real document, and this test
    uses none of that document's actual values."""
    pdf_bytes = _er_pdf(
        [
            "1.",
            "11/11/2026",
            "Merchant : GLOBALTRIP TRAVELS",
            "Expense Location : Pune, India",
            "PNR Number : X9QABC",
            "Start & End KM : mumbai",
            "AIRFARE",
            "Pune to Mumbai flight",
            "Rs.5,200.00",
        ],
        er_id="ER-33170",
        employee="NEHA KAPOOR",
        total="Rs.5,200.00",
    )
    resp = _upload(client, "er33170.pdf", pdf_bytes)
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]
    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    row = expenses[0]
    assert row["category"] == "AIRFARE"
    assert row["merchant"] == "GLOBALTRIP TRAVELS"
    assert row["reference_id"] is None
    assert "Expense Location : Pune, India" in row["expense_details"]
    assert "PNR Number : X9QABC" in row["expense_details"]
    assert "Pune to Mumbai flight" in row["expense_details"]


def test_unseen_report_prefix_label_order_reversed(client):
    """Ref# before Merchant (the opposite order from every real row seen) -
    the fix must not be order-dependent."""
    pdf_bytes = _er_pdf(
        [
            "1.",
            "02/02/2026",
            "Ref# : ZZ998211004",
            "Merchant : CITY CAB SERVICES",
            "CAB CHARGES",
            "Office to airport",
            "Rs.410.00",
        ],
        er_id="ER-44982",
        employee="SAMEER KHAN",
        total="Rs.410.00",
    )
    resp = _upload(client, "er44982.pdf", pdf_bytes)
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]
    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    row = expenses[0]
    assert row["category"] == "CAB CHARGES"
    assert row["merchant"] == "CITY CAB SERVICES"
    assert row["reference_id"] == "ZZ998211004"


# ---------- Continuation pages ----------


def test_category_extraction_across_page_boundary(client):
    """The category line itself (not just the date/Merchant/Ref# lines) can
    land on the page AFTER the row number - the column mapping must survive
    the page break, not just the row-start detection."""
    pdf_bytes = _er_pdf(
        [
            "1.",
            "05/05/2026",
            "MARKETING EXPENSES - FOOD",
            "lunch with client",
            "Rs.540.00",
        ],
        second_page_rows=[
            "2.",
            "06/05/2026",
            "Merchant : PARK HOTEL",
            "Ref# : REF20260506998",
            "LODGING",
            "overnight stay",
            "Rs.4,100.00",
        ],
        er_id="ER-70231",
        employee="DEEPA RAMAN",
        total="Rs.4,640.00",
    )
    resp = _upload(client, "er70231.pdf", pdf_bytes)
    assert resp.status_code == 200, resp.text
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]
    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    assert len(expenses) == 2
    row2 = next(e for e in expenses if e["s_no"] == 2)
    assert row2["category"] == "LODGING"
    assert row2["merchant"] == "PARK HOTEL"
    assert row2["reference_id"] == "REF20260506998"


# ---------- Missing / ambiguous category must stay unresolved, never guessed ----------


def test_row_with_no_recognizable_category_stays_unresolved_not_guessed():
    """If nothing in the pre-amble (within the safety bound) ever names a
    real Policy Master category, the row must be left for review - a
    reference/merchant/description value must never be silently promoted to
    fill the category field."""
    pages = [
        [
            "Expense Report",
            "ER-10001",
            "Amount to be Reimbursed",
            "Rs.50.00",
            "Report",
            "January",
            "Submitted By",
            "TEST EMPLOYEE",
            "x@spincotech.com",
            "Submitted On",
            "01/01/2026",
            "EXPENSE SUMMARY",
            "1.",
            "01/01/2026",
            "Merchant : UNKNOWN VENDOR XYZ",
            "Ref# : REFABC123",
            "Some illegible smudge of text",
            "Rs.50.00",
            "REPORT SUMMARY BY CURRENCY",
            "Total Reimbursable Amount",
            "Rs.50.00",
        ]
    ]
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in pages[0]:
        page.insert_text((72, y), line, fontsize=11)
        y += 16
    buf = BytesIO()
    doc.save(buf)
    doc.close()

    from app.services.extraction.text_extraction import PyMuPDFTextExtractor
    import tempfile as _tempfile

    with _tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(buf.getvalue())
        path = f.name
    extractor = PyMuPDFTextExtractor(ocr_engine=None)
    content_pages = extractor.content_pages(Path(path))
    rows, warnings = expense_report_parser.extract_expense_lines(content_pages)

    assert len(rows) == 1
    row = rows[0]
    # The merchant/reference were still correctly rescued...
    assert row["merchant"] == "UNKNOWN VENDOR XYZ"
    assert row["reference_id"] == "REFABC123"
    # ...but the category must NOT be a reference number, a merchant name,
    # or the garbled text - either None (review-required) or, at worst, the
    # raw unresolved text, never something invented.
    assert row["category"] != "REFABC123"
    assert row["category"] != "UNKNOWN VENDOR XYZ"
    assert row["status"] == "REVIEW_REQUIRED" or row["category"] is None


def test_max_pre_category_lines_bound_prevents_runaway_scan():
    """A pathological row with many junk lines before ever reaching a real
    category (or the amount) must not scan past the next row entirely - the
    safety bound degrades gracefully instead."""
    junk = [f"Some Note {i} : filler text" for i in range(15)]
    pages = [
        [
            "Expense Report",
            "ER-10002",
            "Amount to be Reimbursed",
            "Rs.100.00",
            "Report",
            "January",
            "Submitted By",
            "TEST EMPLOYEE",
            "x@spincotech.com",
            "Submitted On",
            "01/01/2026",
            "EXPENSE SUMMARY",
            "1.",
            "01/01/2026",
            *junk,
            "Rs.50.00",
            "2.",
            "02/01/2026",
            "BATTA",
            "per diem",
            "Rs.50.00",
            "REPORT SUMMARY BY CURRENCY",
            "Total Reimbursable Amount",
            "Rs.100.00",
        ]
    ]
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in pages[0]:
        page.insert_text((72, y), line, fontsize=11)
        y += 16
    buf = BytesIO()
    doc.save(buf)
    doc.close()

    from app.services.extraction.text_extraction import PyMuPDFTextExtractor
    import tempfile as _tempfile

    with _tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(buf.getvalue())
        path = f.name
    extractor = PyMuPDFTextExtractor(ocr_engine=None)
    content_pages = extractor.content_pages(Path(path))
    rows, warnings = expense_report_parser.extract_expense_lines(content_pages)

    assert len(rows) == 2
    # Row 1 never found a real category - must not have consumed row 2's
    # own "BATTA"/amount as its own category/amount.
    assert rows[0]["s_no"] == 1
    assert rows[1]["s_no"] == 2
    assert rows[1]["category"] == "BATTA"
    assert rows[1]["claimed_amount"] == 50.0
