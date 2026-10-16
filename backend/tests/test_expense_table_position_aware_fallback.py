"""Tests for architecture-audit gap #1: content_pages() previously had no
recovery path when a PDF's content-stream text order diverges from its
visual top-to-bottom reading order for the Expense Summary table - the same
class of defect bill_pages_from() already recovers from via
_apply_position_aware_fallback(), just never applied to the expense-table
region. See text_extraction.py's _apply_position_aware_fallback_to_expense_pages
and _expense_rows_look_well_formed.

This is a different kind of layout problem than the P0 fix in
test_expense_category_extraction.py: that fix handles an unknown NUMBER of
optional "Label : value" lines appearing in the correct top-to-bottom order;
this fix handles the lines themselves being emitted in the WRONG order by
the PDF's own content stream despite occupying the correct visual position.
"""
from io import BytesIO
from pathlib import Path

import fitz
import pytest

from app.services.extraction.expense_report_parser import extract_expense_lines
from app.services.extraction.text_extraction import PyMuPDFTextExtractor


def _scrambled_row_pdf(*, row_fields: list[tuple[str, int]], header_lines: list[str], tail_lines: list[str]) -> bytes:
    """Builds a single-page PDF where header_lines/tail_lines are inserted
    normally (in visual top-to-bottom order), but row_fields - a list of
    (text, y) pairs - are inserted in WHATEVER order the caller passes,
    independent of their y position, reproducing a genuine PyMuPDF content-
    stream-order-vs-visual-order divergence (confirmed empirically: PyMuPDF's
    plain get_text() follows insertion/stream order, not position)."""
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in header_lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 16
    for text, field_y in row_fields:
        page.insert_text((72, field_y), text, fontsize=11)
    tail_y = max(fy for _, fy in row_fields) + 32
    for line in tail_lines:
        page.insert_text((72, tail_y), line, fontsize=11)
        tail_y += 16
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _extract(pdf_bytes: bytes):
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        path = f.name
    extractor = PyMuPDFTextExtractor(ocr_engine=None)
    pages = extractor.content_pages(Path(path))
    return pages, extract_expense_lines(pages)


HEADER = [
    "Expense Report",
    "ER-55001",
    "Amount to be Reimbursed",
    "Rs.100.00",
    "Report",
    "January",
    "Submitted By",
    "TEST SCRAMBLED",
    "x@spincotech.com",
    "Submitted On",
    "01/01/2026",
    "EXPENSE SUMMARY",
    "S.No",
    "Expense Details",
    "Category",
    "Amount (INR)",
]
TAIL = ["REPORT SUMMARY BY CURRENCY", "Total Reimbursable Amount", "Rs.100.00", "TEST SCRAMBLED ( T999 )"]


def test_row_scrambled_in_reverse_stream_order_is_recovered():
    """Amount inserted first in the content stream, row-marker last -
    exactly reversed from visual top-to-bottom order. Without the fix, the
    plain linear read sees 'Amount (INR)\\nRs.100.00\\nBATTA\\n01/01/2026\\n1.'
    immediately followed by the REPORT SUMMARY section, producing a row with
    category=None and the wrong amount association."""
    pdf_bytes = _scrambled_row_pdf(
        header_lines=HEADER,
        row_fields=[
            ("Rs.100.00", 400),  # visually last (bottom), inserted first
            ("BATTA", 384),
            ("01/01/2026", 368),
            ("1.", 352),  # visually first (top), inserted last
        ],
        tail_lines=TAIL,
    )
    pages, (rows, warnings) = _extract(pdf_bytes)
    assert pages[0]["source"] == "native_positioned", "the position-aware fallback should have triggered"
    assert len(rows) == 1
    assert rows[0]["category"] == "BATTA"
    assert rows[0]["expense_date"] is not None and rows[0]["expense_date"].isoformat() == "2026-01-01"
    assert rows[0]["claimed_amount"] == 100.0
    assert not warnings


def test_row_scrambled_with_merchant_and_reference_lines_still_recovered():
    """The same defect class, now combined with the P0 fix's own Merchant/
    Ref# handling - proves the two fixes compose correctly rather than
    interfering with each other."""
    pdf_bytes = _scrambled_row_pdf(
        header_lines=HEADER,
        row_fields=[
            ("Rs.2,145.00", 448),
            ("OWN CAR PETROL", 432),
            ("Ref# : TXN883217", 416),
            ("Merchant : SHELL PETROL PUMP", 400),
            ("14/03/2026", 384),
            ("1.", 368),
        ],
        tail_lines=TAIL,
    )
    pages, (rows, warnings) = _extract(pdf_bytes)
    assert pages[0]["source"] == "native_positioned"
    assert len(rows) == 1
    assert rows[0]["category"] == "OWN CAR PETROL"
    assert rows[0]["merchant"] == "SHELL PETROL PUMP"
    assert rows[0]["reference_id"] == "TXN883217"
    assert rows[0]["claimed_amount"] == 2145.0


@pytest.mark.parametrize(
    "pdf_name",
    ["ER-39832.pdf", "ER-40223.pdf", "ER-40706 (1).pdf", "ER-40782.pdf"],
)
def test_real_documents_never_trigger_the_fallback(pdf_name):
    """Every real document's linear extraction already preserves correct
    structure - this fix must stay a true no-op for all of them, never
    perturbing an already-working extraction."""
    pdf_path = Path(__file__).resolve().parent.parent.parent / pdf_name
    if not pdf_path.exists():
        pytest.skip("real corpus PDF not present in this checkout")
    extractor = PyMuPDFTextExtractor(ocr_engine=None)
    pages = extractor.content_pages(pdf_path)
    for page in pages:
        assert page["source"] != "native_positioned", f"{pdf_name} page {page['page_number']}: fallback fired on an already-well-formed real document"


def test_well_formed_ratio_tolerates_a_single_malformed_row():
    """The 80% well-formedness threshold should not trigger reconstruction
    over one genuinely malformed/unusual row among several good ones (e.g. a
    row missing its date entirely) - only when the page's rows are
    predominantly broken."""
    pdf_bytes = _scrambled_row_pdf(
        header_lines=HEADER,
        row_fields=[
            ("1.", 352),
            ("01/01/2026", 368),
            ("BATTA", 384),
            ("Rs.100.00", 400),
            ("2.", 420),
            ("Some unusual row with no date at all", 436),
            ("Rs.50.00", 452),
            ("3.", 472),
            ("02/01/2026", 488),
            ("LOCAL CONVEYANCE", 504),
            ("Rs.60.00", 520),
            ("4.", 540),
            ("03/01/2026", 556),
            ("LOCAL CONVEYANCE", 572),
            ("Rs.70.00", 588),
            ("5.", 608),
            ("04/01/2026", 624),
            ("LOCAL CONVEYANCE", 640),
            ("Rs.80.00", 656),
        ],
        tail_lines=TAIL,
    )
    pages, (rows, warnings) = _extract(pdf_bytes)
    # 4 of 5 row markers have a date within 2 lines (80%) - at the threshold,
    # should NOT trigger reconstruction (already well-formed enough).
    assert pages[0]["source"] == "native"
