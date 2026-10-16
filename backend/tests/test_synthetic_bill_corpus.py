"""Synthetic supporting-bill corpus (architecture-audit Section 10B):
measures real extraction/classification accuracy across document types and
layouts this repo's real corpus doesn't have examples of, rather than
claiming coverage it can't back up. Every ground-truth value here is
authored BY this test file, not copied from production code - production
code (bill_parser.py, category_map.py, text_extraction.py) has zero
knowledge of any vendor/amount/date used below, so a pass here is evidence
of real generalization, not memorization.

Honest by design: several cases are EXPECTED to resolve to None/
REVIEW_REQUIRED (a payment screenshot naming only a person, a receipt with
no amount at all, two conflicting totals, a genuinely novel service type
never researched) - those assertions are proof the system doesn't fabricate
evidence, not failures. A multi-page invoice case that USED to document a
confirmed architecture limitation (one Bill row per page, vendor lost/
corrupted on the continuation page) is now a regression test for the fix -
see bill_grouping.py and test_bill_grouping.py for the full test suite of
that fix's own conservative grouping rules.
"""
import tempfile
from io import BytesIO
from pathlib import Path

import fitz
import pytest

from app.services.extraction import bill_grouping, bill_parser
from app.services.extraction.text_extraction import PyMuPDFTextExtractor


def _image_only_pdf(lines: list[str]) -> Path:
    text_doc = fitz.open()
    text_page = text_doc.new_page()
    y = 40
    for line in lines:
        text_page.insert_text((30, y), line, fontsize=14)
        y += 22
    pix = text_page.get_pixmap(dpi=200)
    img_bytes = pix.tobytes("png")
    text_doc.close()

    img_doc = fitz.open()
    rect = fitz.Rect(0, 0, pix.width, pix.height)
    page = img_doc.new_page(width=pix.width, height=pix.height)
    page.insert_image(rect, stream=img_bytes)
    buf = BytesIO()
    img_doc.save(buf)
    img_doc.close()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(buf.getvalue())
        path = Path(f.name)
    return path


# ---------- Direct bill_parser.parse_bill_text() cases (native-text, single page) ----------

CASES = [
    pytest.param(
        "digital_invoice_generic_consulting",
        [
            "GLOBAL CONSULTING SERVICES PVT LTD",
            "GSTIN: 29ABCDE1234F1Z5",
            "TAX INVOICE",
            "Invoice No: INV-2026-00981",
            "Invoice Date: 15/03/2026",
            "Bill To: Spinco Tech Pvt Ltd",
            "Description: Professional Service Charges - Q1 Advisory",
            "Subtotal: Rs.25,000.00",
            "CGST @9%: Rs.2,250.00",
            "SGST @9%: Rs.2,250.00",
            "Total Amount: Rs.29,500.00",
        ],
        {"amount": 29500.0, "category_guess": "Service Purchase", "vendor_contains": "GLOBAL CONSULTING"},
        id="digital_invoice_generic_consulting",
    ),
    pytest.param(
        "fuel_receipt_unfamiliar_brand",
        [
            "TURBO ENERGY FUEL STATION",
            "Dealer Code: TEFS-4521",
            "Date: 22/04/2026",
            "Nozzle No: 02",
            "Product: Petrol",
            "Rate/L: 96.80",
            "Volume(L): 25.50",
            "Amount: Rs.2,468.40",
        ],
        {"amount": 2468.40, "category_guess": "Fuel"},
        id="fuel_receipt_unfamiliar_brand",
    ),
    pytest.param(
        "hotel_invoice_excluded_brand_words_but_real_structure",
        [
            "SUNRISE GRAND RESIDENCY",
            "GSTIN: 07XYZAB5678C1Z9",
            "TAX INVOICE",
            "Folio No: F-88213",
            "Guest Name: RAHUL SHARMA",
            "Room No: 204",
            "Check-In: 10/05/2026",
            "Check-Out: 12/05/2026",
            "Room Tariff: Rs.3,500.00 per night",
            "Total Amount: Rs.8,260.00",
        ],
        {"amount": 8260.0, "category_guess": "Lodging"},
        id="hotel_invoice_excluded_brand_words_but_real_structure",
    ),
    pytest.param(
        "cab_receipt_lakhs_format_stress_test",
        [
            "QUICKRIDE CABS PVT LTD",
            "Trip Invoice",
            "Trip ID: QR-998213",
            "Pickup Date: 01/06/2026",
            "Ride Fare: Rs.1,25,450.00",
            "Passengers Transport Services",
        ],
        {"amount": 125450.0, "category_guess": "Cab/Taxi"},
        id="cab_receipt_lakhs_format_stress_test",
    ),
    pytest.param(
        "toll_receipt",
        [
            "NHAI TOLL PLAZA - KHERKI DAULA",
            "FASTag Transaction",
            "Vehicle Class: Car/Jeep/Van",
            "Date: 03/06/2026",
            "Amount: Rs.95.00",
        ],
        {"amount": 95.0, "category_guess": "Toll"},
        id="toll_receipt",
    ),
    pytest.param(
        "tax_invoice_courier_whole_rupee",
        [
            "SPEEDLINE COURIERS",
            "TAX INVOICE",
            "AWB No: SL88213744",
            "Invoice Date: 05/06/2026",
            "Consignment: Document Parcel",
            "Amount: Rs.180",
        ],
        {"amount": 180.0, "category_guess": "Courier"},
        id="tax_invoice_courier_whole_rupee",
    ),
    pytest.param(
        "train_ticket_irctc_month_name_date",
        [
            "INDIAN RAILWAYS",
            "IRCTC E-TICKET",
            "PNR: 4521889213",
            "Date of Journey: 22-Jun-2026",
            "Train No: 12301",
            "Passenger: RAHUL SHARMA",
            "Total Fare (all inclusive): Rs.1,245.00",
        ],
        {"amount": 1245.0, "category_guess": "Train"},
        id="train_ticket_irctc_month_name_date",
    ),
    pytest.param(
        "payment_screenshot_person_only_no_service_evidence",
        [
            "Payment Successful",
            "Amount",
            "Rs.450.00",
            "To",
            "Anil Kumar Verma",
            "UPI ID: anilverma@okaxis",
            "Paid at 14:32, 08 Jul 2026",
            "UPI Ref No: 998213744521",
        ],
        {"amount": 450.0, "category_guess": None},  # honest: no service evidence, must not be guessed
        id="payment_screenshot_person_only_no_service_evidence",
    ),
    pytest.param(
        "missing_amount_field",
        [
            "CITY STATIONERY MART",
            "Retail Invoice",
            "Items: Notebooks, Pens, Stapler",
            "Thank you for your purchase",
        ],
        {"amount": None, "category_guess": "Printing & Stationery"},  # amount honestly missing; category still identifiable
        id="missing_amount_field",
    ),
    pytest.param(
        "unfamiliar_novel_service_type_outside_policy_master",
        [
            "GREENLEAF PEST CONTROL SERVICES",
            "Service Invoice",
            "Service: Quarterly Pest Control Treatment",
            "Date: 10/07/2026",
            "Amount: Rs.1,200.00",
        ],
        {"amount": 1200.0, "category_guess": None},  # honest: genuinely never-researched service type
        id="unfamiliar_novel_service_type_outside_policy_master",
    ),
]


@pytest.mark.parametrize("name,lines,expected", CASES)
def test_synthetic_bill_case(name, lines, expected):
    text = "\n".join(lines)
    parsed = bill_parser.parse_bill_text(text)
    assert parsed.amount == expected["amount"], f"{name}: amount {parsed.amount} != expected {expected['amount']}"
    assert parsed.category_guess == expected["category_guess"], (
        f"{name}: category_guess {parsed.category_guess!r} != expected {expected['category_guess']!r}"
    )
    if "vendor_contains" in expected:
        assert parsed.vendor is not None and expected["vendor_contains"] in parsed.vendor.upper()


def test_conflicting_totals_are_flagged_not_guessed():
    """Two differing amounts at the same label priority - the system must
    surface the ambiguity (None + a warning), never silently pick one."""
    text = "\n".join(["RETAIL INVOICE", "Item A", "Total: Rs.500.00", "Item B", "Total: Rs.750.00"])
    parsed = bill_parser.parse_bill_text(text)
    assert parsed.amount is None
    assert any("multiple differing amounts" in w.lower() for w in parsed.warnings)


# ---------- Real OCR path (genuine Tesseract, not mocked) ----------


def test_scanned_receipt_via_real_ocr():
    """An image-only page (no selectable text at all) run through the real
    local Tesseract engine, not a fake - measures actual OCR-path accuracy
    rather than assuming the deterministic parser's own unit coverage
    implies the OCR path works too."""
    from app.services.extraction.ocr_engine import TesseractOCREngine

    path = _image_only_pdf(
        [
            "CORNER STATIONERY SHOP",
            "Cash Memo",
            "Date: 12/07/2026",
            "Printing and Photocopy Services",
            "Amount: Rs.350.00",
        ]
    )
    extractor = PyMuPDFTextExtractor(ocr_engine=TesseractOCREngine())
    pages = extractor.bill_pages_from(path, 1)
    assert len(pages) == 1
    page = pages[0]
    assert page["source"] == "ocr", f"expected OCR to run, source was {page['source']!r}"
    parsed = bill_parser.parse_bill_text(page["text"])
    # OCR introduces real-world noise - report what actually happens rather
    # than asserting perfection. The category (short, common printed words)
    # and a plausible amount should still come through.
    print(f"[OCR result] amount={parsed.amount} category_guess={parsed.category_guess} raw_text={page['text']!r}")
    assert parsed.category_guess == "Printing & Stationery", f"OCR text was: {page['text']!r}"
    assert parsed.amount == 350.0, f"OCR text was: {page['text']!r}"


# ---------- Fixed (was a confirmed architecture limitation; see bill_grouping.py) ----------


def test_multipage_invoice_is_now_correctly_grouped_into_one_bill():
    """Was a confirmed real limitation: this pipeline used to extract one
    Bill row PER PAGE unconditionally, so this exact document (vendor/items
    on page 1, total on page 2, a 'Continued from page 1' marker) produced
    two separate, each-partial records, and page 2's own vendor fallback
    picked up its continuation text as a spurious vendor value. Both are now
    fixed by bill_grouping.py's conservative continuation-evidence grouping
    (full test suite in test_bill_grouping.py) - this test keeps the exact
    real-world-shaped document from that original finding as a regression
    guard for the fix."""
    doc = fitz.open()
    page1 = doc.new_page()
    y = 72
    for line in ["EVERGREEN EVENTS & CATERING LLP", "GSTIN: 19PQRXY7890D1Z3", "TAX INVOICE", "Invoice No: EV-55213", "Items: Venue, Catering, AV Setup"]:
        page1.insert_text((72, y), line, fontsize=11)
        y += 16
    page2 = doc.new_page()
    y = 72
    for line in ["Continued from page 1", "Subtotal: Rs.40,000.00", "Total Amount: Rs.47,200.00"]:
        page2.insert_text((72, y), line, fontsize=11)
        y += 16
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(buf.getvalue())
        path = Path(f.name)

    extractor = PyMuPDFTextExtractor(ocr_engine=None)
    pages = extractor.bill_pages_from(path, 1)
    groups = bill_grouping.group_bill_pages(pages)

    assert len(groups) == 1, "the two pages should now be grouped into one logical bill"
    group = groups[0]
    assert group[0]["page_number"] == 1 and group[-1]["page_number"] == 2
    combined = bill_parser.parse_bill_text("\n".join(p["text"] for p in group))
    assert combined.vendor is not None and "EVERGREEN" in combined.vendor.upper()
    assert combined.amount == 47200.0
