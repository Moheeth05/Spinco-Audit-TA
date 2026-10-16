"""Tests for conservative multi-page bill grouping (bill_grouping.py) and
the related _find_vendor continuation-marker fix in bill_parser.py.

Covers the required synthetic cases: a two-page invoice (vendor on page 1,
items+total on page 2), a total appearing only on the final page of a
multi-page invoice, repeated invoice identifiers, a continuation page with
no vendor, two distinct invoices in one PDF, unrelated adjacent pages that
must not merge, conflicting invoice identifiers, and regression checks
against all 4 real single-page-bill ER PDFs.
"""
import tempfile
from io import BytesIO
from pathlib import Path

import fitz
import pytest

from app.services.extraction import bill_grouping, bill_parser
from app.services.extraction.text_extraction import PyMuPDFTextExtractor

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _bill_pdf(pages_lines: list[list[str]]) -> Path:
    doc = fitz.open()
    for lines in pages_lines:
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=11)
            y += 16
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(buf.getvalue())
        return Path(f.name)


def _bill_pages(pages_lines: list[list[str]]):
    path = _bill_pdf(pages_lines)
    extractor = PyMuPDFTextExtractor(ocr_engine=None)
    return extractor.bill_pages_from(path, 1)


def _parse_groups(pages_lines: list[list[str]]):
    pages = _bill_pages(pages_lines)
    groups = bill_grouping.group_bill_pages(pages)
    return [
        (bill_parser.parse_bill_text("\n".join(p["text"] for p in g)), g[0]["page_number"], g[-1]["page_number"])
        for g in groups
    ]


# ---------- Required case: two-page invoice, vendor page 1, items+total page 2 ----------


def test_two_page_invoice_vendor_page1_total_page2_same_invoice_number():
    pages_lines = [
        [
            "EVERGREEN EVENTS & CATERING LLP",
            "GSTIN: 19PQRXY7890D1Z3",
            "TAX INVOICE",
            "Invoice No: EV-55213",
            "Items: Venue Rental, AV Setup",
        ],
        [
            "Invoice No: EV-55213",
            "Continued - Catering Items",
            "Subtotal: Rs.40,000.00",
            "Total Amount: Rs.47,200.00",
        ],
    ]
    groups = bill_grouping.group_bill_pages(_bill_pages(pages_lines))
    assert len(groups) == 1, "the two pages should be grouped into one logical bill"
    group = groups[0]
    assert group[0]["page_number"] == 1 and group[-1]["page_number"] == 2
    combined = bill_parser.parse_bill_text("\n".join(p["text"] for p in group))
    assert combined.amount == 47200.0
    assert combined.vendor is not None and "EVERGREEN" in combined.vendor.upper()


# ---------- Required case: total only on the final page of a 3-page invoice ----------


def test_total_only_on_final_page_of_three_page_invoice():
    pages_lines = [
        ["GLOBAL CONSULTING SERVICES PVT LTD", "GSTIN: 29ABCDE1234F1Z5", "TAX INVOICE", "Invoice No: GC-9981", "Page 1 of 3"],
        ["Item: Advisory work continued", "Page 2 of 3"],
        ["Page 3 of 3", "Total Amount: Rs.29,500.00"],
    ]
    groups = bill_grouping.group_bill_pages(_bill_pages(pages_lines))
    assert len(groups) == 1
    group = groups[0]
    assert group[0]["page_number"] == 1 and group[-1]["page_number"] == 3
    combined = bill_parser.parse_bill_text("\n".join(p["text"] for p in group))
    assert combined.amount == 29500.0
    assert combined.vendor is not None and "GLOBAL CONSULTING" in combined.vendor.upper()


# ---------- Required case: continuation page with no vendor name ----------


def test_continuation_page_with_no_vendor_name_does_not_get_a_wrong_vendor():
    """The confirmed real defect from the prior round: a page with no vendor
    of its own picking up continuation text as if it were a vendor. Grouped
    here (continuation marker present), so the group's real vendor (from
    page 1) is used - page 2 contributes no spurious vendor of its own."""
    pages_lines = [
        ["SUNRISE GRAND RESIDENCY", "GSTIN: 07XYZAB5678C1Z9", "Folio No: F-88213", "Room Tariff: Rs.3,500.00"],
        ["Continued from previous page", "Total Amount: Rs.8,260.00"],
    ]
    groups = bill_grouping.group_bill_pages(_bill_pages(pages_lines))
    assert len(groups) == 1
    combined = bill_parser.parse_bill_text("\n".join(p["text"] for p in groups[0]))
    assert combined.vendor is not None and "SUNRISE GRAND RESIDENCY" in combined.vendor.upper()
    assert combined.amount == 8260.0


def test_vendor_fallback_no_longer_invents_vendor_from_continuation_text_alone():
    """The _find_vendor fix itself, independent of grouping: the confirmed
    real defect was specifically a continuation-marker LINE ITSELF ("Continued
    from page 1") being promoted to vendor by the weak fallback. The fallback
    still exists for genuinely plausible-looking text (by design, for every
    other bill) - this only asserts the continuation-marker line can never be
    the value it returns."""
    vendor, strong = bill_parser._find_vendor("Continued from page 1")
    assert vendor is None
    vendor, strong = bill_parser._find_vendor("Continued from page 1\nSubtotal: Rs.40,000.00\nTotal Amount: Rs.47,200.00")
    assert vendor != "Continued from page 1"


# ---------- Required case: repeated invoice identifiers across pages ----------


def test_repeated_invoice_identifier_is_sufficient_continuation_evidence():
    pages_lines = [
        ["QUICK PRINT SOLUTIONS", "GSTIN: 11QWERT9876M1Z1", "Bill No: QP-4471", "Items: Banners, Flyers"],
        ["Bill No: QP-4471", "Amount: Rs.3,200.00"],
    ]
    groups = bill_grouping.group_bill_pages(_bill_pages(pages_lines))
    assert len(groups) == 1


# ---------- Required case: two distinct invoices inside one PDF ----------


def test_two_distinct_invoices_each_with_own_gstin_are_not_merged():
    pages_lines = [
        ["FIRST VENDOR SERVICES LTD", "GSTIN: 19AAAAA1111A1Z1", "Invoice No: FV-001", "Amount: Rs.1,000.00"],
        ["SECOND VENDOR TRADERS LTD", "GSTIN: 27BBBBB2222B1Z2", "Invoice No: SV-002", "Amount: Rs.2,000.00"],
    ]
    groups = bill_grouping.group_bill_pages(_bill_pages(pages_lines))
    assert len(groups) == 2, "each page has its own GSTIN - must never be merged"


# ---------- Required case: unrelated adjacent pages must not be merged ----------


def test_unrelated_adjacent_pages_with_no_shared_evidence_are_not_merged():
    """Page 1 (a cab receipt with no total extractable) immediately followed
    by page 2 (a wholly unrelated hotel invoice, which DOES have its own
    strong vendor evidence) - must never merge just because they're
    adjacent."""
    pages_lines = [
        ["QUICKRIDE CABS PVT LTD", "Trip ID: QR-998213", "Passengers Transport Services"],
        ["PALM GROVE SUITES HOTEL PVT LTD", "GSTIN: 29ZZZZZ3333Z1Z9", "Room Tariff: Rs.4,500.00"],
    ]
    groups = bill_grouping.group_bill_pages(_bill_pages(pages_lines))
    assert len(groups) == 2


def test_unrelated_adjacent_pages_neither_with_strong_evidence_still_not_merged():
    """Even when NEITHER page has a GSTIN/vendor-suffix match (so neither
    looks like a fresh bill's own start), two genuinely unrelated pages with
    no continuation marker and no repeated bill number must stay separate -
    adjacency and "no new-bill evidence" together are still not enough."""
    pages_lines = [
        ["Payment Successful", "Amount", "Rs.450.00", "To", "Anil Kumar Verma", "UPI Ref No: 998213744521"],
        ["Payment Successful", "Amount", "Rs.120.00", "To", "Sunita Devi", "UPI Ref No: 112233445566"],
    ]
    groups = bill_grouping.group_bill_pages(_bill_pages(pages_lines))
    assert len(groups) == 2, "no continuation marker or repeated bill number - must not guess a merge"


# ---------- Required case: missing or conflicting invoice identifiers ----------


def test_conflicting_invoice_numbers_are_never_merged_even_with_no_marker():
    pages_lines = [
        ["THIRD VENDOR LLP", "Bill No: TV-100", "Items: Consulting"],
        ["Bill No: TV-200", "Amount: Rs.5,000.00"],
    ]
    groups = bill_grouping.group_bill_pages(_bill_pages(pages_lines))
    assert len(groups) == 2


def test_conflicting_invoice_numbers_veto_even_with_a_continuation_marker_present():
    """A hard veto: differing bill numbers must block grouping even when a
    continuation marker is ALSO present (the marker could be incidental
    boilerplate, e.g. 'for continued service contact support' - a genuine ID
    conflict is stronger, contradicting evidence)."""
    pages_lines = [
        ["FOURTH VENDOR LLP", "Bill No: FV-100", "Items: Consulting"],
        ["Bill No: FV-999", "Page 2 of 2", "Amount: Rs.5,000.00"],
    ]
    groups = bill_grouping.group_bill_pages(_bill_pages(pages_lines))
    assert len(groups) == 2, "conflicting bill numbers must veto grouping even alongside a continuation marker"


# ---------- Confirmed real case (found via the real-corpus regression test below): a multi-page ----------
# ---------- statement must never be grouped, even though it explicitly prints "Page X of Y" ----------


def test_multi_transaction_statement_pages_are_never_grouped_despite_page_marker():
    """Real confirmed case: a credit card statement explicitly prints
    'Page 1 of 10' / 'Page 2 of 10' pagination (exactly matching the
    continuation-marker signal) while each page lists a dozen-plus distinct,
    unrelated transactions. Grouping these would conflate many real,
    independent transactions into one meaningless 'bill'."""
    pages_lines = [
        [
            "CREDIT CARD STATEMENT",
            "Date SerNo Transaction Details Amount",
            "01/07/2026 1001 MERCHANT A 500.00",
            "02/07/2026 1002 MERCHANT B 1200.00",
            "03/07/2026 1003 MERCHANT C 300.00",
            "04/07/2026 1004 MERCHANT D 900.00",
            "05/07/2026 1005 MERCHANT E 150.00",
            "06/07/2026 1006 MERCHANT F 4500.00",
            "Page 1 of 2",
        ],
        [
            "Date SerNo Transaction Details Amount",
            "07/07/2026 1007 MERCHANT G 650.00",
            "08/07/2026 1008 MERCHANT H 220.00",
            "09/07/2026 1009 MERCHANT I 310.00",
            "10/07/2026 1010 MERCHANT J 890.00",
            "11/07/2026 1011 MERCHANT K 1150.00",
            "Page 2 of 2",
        ],
    ]
    groups = bill_grouping.group_bill_pages(_bill_pages(pages_lines))
    assert len(groups) == 2, "a multi-transaction statement must never be grouped, even with explicit pagination"


# ---------- Required case: bills with several legitimate expense lines (single page, regression) ----------


def test_single_page_bill_with_multiple_line_items_unaffected():
    pages_lines = [
        [
            "CITY MART RETAIL",
            "GSTIN: 07AAAAA5555A1Z5",
            "Retail Invoice",
            "Item 1: Notebooks - Rs.150.00",
            "Item 2: Pens - Rs.80.00",
            "Total: Rs.230.00",
        ]
    ]
    groups = bill_grouping.group_bill_pages(_bill_pages(pages_lines))
    assert len(groups) == 1
    assert len(groups[0]) == 1


# ---------- Regression: real single-page-bill ER PDFs never trigger grouping ----------


@pytest.mark.parametrize("pdf_name", ["ER-39832.pdf", "ER-40223.pdf", "ER-40706 (1).pdf", "ER-40782.pdf"])
def test_real_documents_never_group_pages(pdf_name, client):
    """Every real ER PDF in this corpus has only single-page bills - this
    change must be a true no-op for all of them: same bill count as before,
    and page_number_end always None."""
    pdf_path = REPO_ROOT / pdf_name
    if not pdf_path.exists():
        pytest.skip("real corpus PDF not present in this checkout")
    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (pdf_name, pdf_path.read_bytes(), "application/pdf"))],
    )
    assert resp.status_code == 200, resp.text
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]
    bills = client.get(f"/api/expense-reports/{report_id}/bills").json()
    assert bills, f"{pdf_name} should still produce bill records"
    for b in bills:
        assert b["page_number_end"] is None, f"{pdf_name}: bill id={b['id']} unexpectedly grouped (page_number_end={b['page_number_end']})"
