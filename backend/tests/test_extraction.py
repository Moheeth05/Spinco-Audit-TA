from io import BytesIO
from pathlib import Path

import fitz
import pytest

from app.services.extraction.classifier import EXPENSE_REPORT, TRIP_REPORT, UNKNOWN, classify_text
from app.services.extraction.dates import parse_date
from app.services.extraction.expense_report_parser import extract_embedded_trip_summary
from app.services.extraction.money import parse_amount

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

ALL_ER_PDFS = ["ER-39832.pdf", "ER-40223.pdf", "ER-40706 (1).pdf", "ER-40782.pdf"]
ALL_TRIP_PDFS = [
    "TRIP-24935_Trip_To_Indore_Pithampur.pdf",
    "TRIP-25167_Raipur,_Billai,_Ahmedabad-_27thJuly_-30th_July_2026.pdf",
    "TRIP-25845_Chandigarh_tour30.06.2026_to_12.07.2026.pdf",
    "TRIP-25921_Indore.pdf",
]


def _make_text_pdf(lines: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 16
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _make_image_only_pdf(text: str) -> bytes:
    """A page with no selectable text at all - only a rendered image of text,
    to exercise the OCR fallback path."""
    text_doc = fitz.open()
    text_page = text_doc.new_page()
    text_page.insert_text((30, 40), text, fontsize=14)
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
    return buf.getvalue()


def _upload(client, endpoint, filename, content):
    return client.post(f"/api/uploads/{endpoint}", files=[("files", (filename, content, "application/pdf"))])


# ---------- 6/7: money + date unit tests ----------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("₹2,000", 2000.0),
        ("Rs.2,000.00", 2000.0),
        ("Rs. 2000", 2000.0),
        ("INR 2,000", 2000.0),
        ("16,845.00", 16845.0),
        ("(-) 0.00", 0.0),
        ("not a number", None),
        ("", None),
        (None, None),
    ],
)
def test_money_parsing(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("01/06/2026", (2026, 6, 1)),
        ("Submitted on 09/08/2026 please", (2026, 8, 9)),
        ("not a date", None),
        ("32/13/2026", None),  # invalid calendar date
    ],
)
def test_date_parsing(text, expected):
    result = parse_date(text)
    if expected is None:
        assert result is None
    else:
        assert (result.year, result.month, result.day) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # Post-UAT correction 2, Part 2: real supporting bills print textual
        # month-name dates, not just DD/MM/YYYY - a cab receipt's own
        # "Date: August 7, 2026" and an IRCTC ticket's "22-Aug-2026" must
        # both be recognized, not just silently dropped.
        ("Date: August 7, 2026", (2026, 8, 7)),
        ("August 7, 2026", (2026, 8, 7)),
        ("7 August 2026", (2026, 8, 7)),
        ("22-Aug-2026", (2026, 8, 22)),
        ("Departure* 14:55 22-Aug-2026", (2026, 8, 22)),
        ("21-Aug-2026 16:31:16 HRS", (2026, 8, 21)),
        ("Paid at 09:50 AM, 11 Aug 2026", (2026, 8, 11)),
        ("32 August 2026", None),  # invalid day, must not guess
    ],
)
def test_textual_month_name_date_parsing(text, expected):
    result = parse_date(text)
    if expected is None:
        assert result is None
    else:
        assert (result.year, result.month, result.day) == expected


def test_find_all_dates_orders_by_position_across_formats():
    from app.services.extraction.dates import find_all_dates

    dates = find_all_dates("First 01/06/2026 then August 7, 2026 then 22-Aug-2026")
    assert [(d.month, d.day) for d in dates] == [(6, 1), (8, 7), (8, 22)]


# ---------- 1/2/3: classification ----------


def test_classifies_real_expense_reports_as_expense_report():
    for name in ALL_ER_PDFS:
        doc = fitz.open(REPO_ROOT / name)
        text = "\n".join(doc[i].get_text() for i in range(min(2, doc.page_count)))
        doc.close()
        assert classify_text(text) == EXPENSE_REPORT, f"{name} misclassified"


def test_classifies_real_trip_reports_as_trip_report():
    for name in ALL_TRIP_PDFS:
        doc = fitz.open(REPO_ROOT / name)
        text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
        doc.close()
        assert classify_text(text) == TRIP_REPORT, f"{name} misclassified"


def test_classifies_unrelated_text_as_unknown():
    assert classify_text("Just a random memo about lunch plans with no relevant keywords.") == UNKNOWN
    assert classify_text("") == UNKNOWN


# ---------- 4/5/8/11/12: expense report header + lines + confidence ----------


def test_er39832_full_extraction_matches_known_values(client, sample_pdf_path):
    result = _upload(client, "expense-reports", sample_pdf_path.name, sample_pdf_path.read_bytes()).json()
    doc_id = result["results"][0]["document_id"]
    detail = client.get(f"/api/documents/{doc_id}/extraction").json()

    assert detail["extraction_status"] == "EXTRACTED"
    # Phase 4 folds bill-extraction warnings (many of these bill pages are hard-to-OCR
    # thermal receipts) into the same report-level confidence signal, so a report with
    # otherwise-perfect header/line extraction can still land at MEDIUM here - see
    # tests/test_bills.py for the bill-extraction-specific assertions.
    assert detail["extraction_confidence"] in ("HIGH", "MEDIUM")
    header = detail["header"]
    assert header["er_id"] == "ER-39832"
    assert header["employee_name"] == "MANEESH KUMAR P. N."
    assert header["employee_code"] == "TL085"
    assert header["total_claimed"] == 16845.0
    assert header["total_expenses"] == 12
    assert header["er_id_source_page"] == 1

    expenses = detail["expenses"]
    assert len(expenses) == 12
    first = expenses[0]
    # The critical accuracy case called out in the spec: this must stay Rs.2,000, never drift to 2,500.
    assert first["expense_date"] == "2026-06-01"
    assert first["category"] == "OWN CAR PETROL"
    assert first["claimed_amount"] == 2000.0
    assert first["source_page"] == 1
    assert all(e["extraction_status"] == "EXTRACTED" for e in expenses)
    assert sum(e["claimed_amount"] for e in expenses) == 16845.0


def test_all_real_expense_reports_extract_with_no_invented_values(client):
    for name in ALL_ER_PDFS:
        content = (REPO_ROOT / name).read_bytes()
        result = _upload(client, "expense-reports", name, content).json()["results"][0]
        assert result["success"], f"{name} failed to upload"
        detail = client.get(f"/api/documents/{result['document_id']}/extraction").json()
        assert detail["extraction_status"] in ("EXTRACTED", "REVIEW_REQUIRED"), name
        assert detail["header"]["er_id"] is not None, f"{name}: er_id must be found"
        assert len(detail["expenses"]) == detail["header"]["total_expenses"]
        # Department/location aren't present in any real sample - must stay null, never guessed.
        assert detail["header"]["department"] is None
        assert detail["header"]["location"] is None


# ---------- 10: embedded trip summary ----------


def test_er40223_embedded_trip_summary_detected(client):
    name = "ER-40223.pdf"
    content = (REPO_ROOT / name).read_bytes()
    result = _upload(client, "expense-reports", name, content).json()["results"][0]
    detail = client.get(f"/api/documents/{result['document_id']}/extraction").json()

    embedded = detail["embedded_trip_summary"]
    assert embedded is not None
    assert embedded["trip_id"] == "TRIP-25167"
    assert embedded["destination"] == "Raipur"
    assert len(embedded["legs"]) == 5
    assert embedded["legs"][0]["from_location"] == "Bangalore"
    assert embedded["legs"][0]["to_location"] == "Raipur"

    # The embedded trip must NOT show up in the standalone Trip Reports listing.
    trip_reports = client.get("/api/trip-reports").json()
    assert embedded["document_id"] not in [t["document_id"] for t in trip_reports]


def test_er_without_trip_summary_has_no_embedded_section(client, sample_pdf_path):
    result = _upload(client, "expense-reports", sample_pdf_path.name, sample_pdf_path.read_bytes()).json()
    doc_id = result["results"][0]["document_id"]
    detail = client.get(f"/api/documents/{doc_id}/extraction").json()
    assert detail["embedded_trip_summary"] is None


def test_embedded_trip_summary_with_transport_mode_labels_pairs_legs_correctly():
    """Post-UAT correctness fix: a real document (ER-41115) prints a
    transport-mode label ("Train") ahead of some legs' from-location, and a
    free-text description after each leg's to-date - both bare text lines
    indistinguishable from a real location by content alone. The old
    from/to pairing logic treated every bare line as a location in strict
    sequence, so a leading "Train" line shifted every subsequent pair by one
    slot, corrupting every leg's from/to locations and - critically -
    leaving the very first leg's from_date unset, which made the derived
    trip start_date None ("travel period could not be reliably determined")
    even though the source PDF states it plainly."""
    pages = [
        {
            "page_number": 1,
            "text": "\n".join(
                [
                    "Trip Number: TRIP-25630",
                    "TRIP SUMMARY",
                    "City: Balasore",
                    "From",
                    "To",
                    "Description",
                    "Train",
                    "Howrah",
                    "11/08/2026",
                    "Balasore",
                    "11/08/2026",
                    "Falcon LCMS call",
                    "Balasore",
                    "12/08/2026",
                    "Balasore",
                    "13/08/2026",
                    "Falcon LCMS call",
                    "Train",
                    "Balasore",
                    "13/08/2026",
                    "Howrah",
                    "13/08/2026",
                    "Falcon LCMS call",
                    "EXPENSE SUMMARY",
                ]
            ),
        }
    ]
    result = extract_embedded_trip_summary(pages)
    assert result is not None
    assert result["trip_number"] == "TRIP-25630"
    legs = result["legs"]
    assert len(legs) == 3
    assert legs[0]["from_location"] == "Howrah"
    assert legs[0]["to_location"] == "Balasore"
    assert legs[0]["from_date"] == parse_date("11/08/2026")
    assert legs[1]["from_location"] == "Balasore"
    assert legs[1]["to_location"] == "Balasore"
    assert legs[2]["from_location"] == "Balasore"
    assert legs[2]["to_location"] == "Howrah"
    assert legs[2]["to_date"] == parse_date("13/08/2026")
    # This is what actually drives TripReport.start_date/end_date (see
    # extraction/pipeline.py) - both must be real dates, not None.
    assert legs[0]["from_date"] is not None
    assert legs[-1]["to_date"] is not None


# ---------- 11: category vs. merchant separation (post-UAT correctness fix) ----------
#
# A real document layout (ER-41115) prints a "Merchant : <name>" line between
# the date and the category for most rows - and prior to this fix that line
# was misread AS the category itself ("Merchant : TCGLS" stored verbatim in
# Expense.category), which then failed every Policy Master lookup and
# cascaded into POLICY NOT FOUND / bill-matching exceptions. Also covers:
# merchant is never re-labelled as category (except when the merchant text
# itself independently resolves to a real Policy Master row, e.g. self-driven
# fuel with no third-party vendor), and a resolved category is always stored
# as the canonical Policy Master string, not the raw noisy extracted text
# (a trailing "[1]" or a repeated "(LODGING)"), so Analytics/Excel never
# fragment one real category into several cosmetic-noise buckets.


def _make_expense_report_pdf(rows: list[str]) -> bytes:
    lines = [
        "Expense Report",
        "ER-90099",
        "Amount to be Reimbursed",
        "Rs.100.00",
        "Report",
        "January",
        "Submitted By",
        "TEST EMPLOYEE",
        "test@spincotech.com",
        "Submitted On",
        "01/01/2026",
        "EXPENSE SUMMARY",
        *rows,
        "REPORT SUMMARY BY CURRENCY",
        "Total Reimbursable Amount",
        "Rs.100.00",
        "TEST EMPLOYEE ( T001 )",
    ]
    return _make_text_pdf(lines)


def test_merchant_line_is_stored_separately_not_as_category(client):
    pdf_bytes = _make_expense_report_pdf(
        [
            "1.",
            "01/01/2026",
            "Merchant : TCGLS",
            "CAB CHARGES",
            "Cab to office",
            "Rs.546.00",
        ]
    )
    result = _upload(client, "expense-reports", "merchant_line.pdf", pdf_bytes).json()["results"][0]
    report_id = result["document_id"].split(":")[1]
    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    assert expenses[0]["category"] == "CAB CHARGES"
    assert expenses[0]["merchant"] == "TCGLS"


def test_merchant_only_row_falls_back_to_merchant_as_category_when_it_is_a_real_policy_name(client):
    """Some rows in the real document have no separate category line at all -
    the "Merchant : X" line is the only place category-like text appears,
    and only because X (here OWN CAR PETROL) happens to independently name a
    real, active Policy Master category - a self-driven-vehicle fuel expense
    genuinely has no third-party vendor to separately record."""
    pdf_bytes = _make_expense_report_pdf(
        [
            "1.",
            "01/01/2026",
            "Merchant : OWN CAR PETROL",
            "Petrol expense",
            "Rs.100.00",
        ]
    )
    result = _upload(client, "expense-reports", "merchant_only.pdf", pdf_bytes).json()["results"][0]
    report_id = result["document_id"].split(":")[1]
    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    assert expenses[0]["category"] == "OWN CAR PETROL"
    assert expenses[0]["merchant"] == "OWN CAR PETROL"


def test_merchant_text_that_is_not_a_real_category_is_never_promoted():
    """The fallback must never invent a category - unresolvable merchant text
    stays out of the category field entirely."""
    from app.database import SessionLocal
    from app.services.extraction.pipeline import _resolve_category

    db = SessionLocal()
    try:
        assert _resolve_category(db, None, "Random Merchant Pvt Ltd") is None
        assert _resolve_category(db, "Not A Real Category", "Random Merchant Pvt Ltd") == "Not A Real Category"
    finally:
        db.close()


def test_resolved_category_is_stored_as_canonical_policy_master_string():
    """A category line with cosmetic extraction noise ("BATTA [1]",
    "LODGING (LODGING)") must resolve and persist as the exact Policy Master
    name, not the raw noisy text - otherwise Analytics/Excel/Exceptions each
    fragment the same real category into several lookalike buckets."""
    from app.database import SessionLocal
    from app.services.extraction.pipeline import _resolve_category

    db = SessionLocal()
    try:
        assert _resolve_category(db, "BATTA [1]", None) == "BATTA"
        assert _resolve_category(db, "LODGING (LODGING)", None) == "LODGING"
        assert _resolve_category(db, "LOCAL CONVEYANCE", None) == "LOCAL CONVEYANCE"
    finally:
        db.close()


def test_unresolvable_category_is_left_for_review_not_guessed():
    from app.database import SessionLocal
    from app.services.extraction.pipeline import _resolve_category

    db = SessionLocal()
    try:
        assert _resolve_category(db, "Totally Unknown Category", None) == "Totally Unknown Category"
    finally:
        db.close()


# ---------- 9: standalone trip report metadata ----------


def test_standalone_trip_report_extraction(client, sample_trip_pdf_path):
    result = _upload(client, "trip-reports", sample_trip_pdf_path.name, sample_trip_pdf_path.read_bytes()).json()
    doc_id = result["results"][0]["document_id"]
    detail = client.get(f"/api/documents/{doc_id}/extraction").json()

    trip = detail["trip"]
    assert trip["trip_id"] == "TRIP-24935"
    assert trip["employee_name"] == "PRANAY RAJESH PAWAR"
    assert trip["employee_code"] == "T465"
    assert trip["start_date"] == "2026-07-13"
    assert trip["end_date"] == "2026-07-19"
    assert trip["destination"] == "Indore"
    assert trip["approval_status"] == "Approved"
    assert trip["approver"] == "RAVAL KETAN R"
    assert len(detail["legs"]) == 2


def test_all_real_trip_reports_extract_cleanly(client):
    for name in ALL_TRIP_PDFS:
        content = (REPO_ROOT / name).read_bytes()
        result = _upload(client, "trip-reports", name, content).json()["results"][0]
        assert result["success"], name
        detail = client.get(f"/api/documents/{result['document_id']}/extraction").json()
        assert detail["trip"]["trip_id"] is not None, name
        assert detail["extraction_status"] in ("EXTRACTED", "REVIEW_REQUIRED")
        # Every leg date must fall within the trip's own stated travel period - the
        # itinerary-format cross-check that catches a wrong block being parsed as a leg.
        start, end = detail["trip"]["start_date"], detail["trip"]["end_date"]
        if start and end:
            for leg in detail["legs"]:
                if leg["from_date"]:
                    assert start <= leg["from_date"] <= end, f"{name} leg {leg['leg_no']} out of range"


# ---------- 13: missing/uncertain field handling ----------


def test_expense_report_missing_critical_fields_is_review_required(client):
    pdf_bytes = _make_text_pdf(
        [
            "Random Document",
            "Expense Report",
            "EXPENSE SUMMARY",
            "Nothing else useful here.",
        ]
    )
    result = _upload(client, "expense-reports", "incomplete.pdf", pdf_bytes).json()["results"][0]
    assert result["success"] is True  # valid PDF, so upload succeeds; extraction flags it
    doc_id = result["document_id"]
    detail = client.get(f"/api/documents/{doc_id}/extraction").json()
    assert detail["extraction_status"] == "REVIEW_REQUIRED"
    assert detail["extraction_notes"]
    assert detail["header"]["employee_name"] is None  # never guessed


def test_trip_report_missing_trip_id_is_review_required(client):
    pdf_bytes = _make_text_pdf(["SPINCO TECH PVT LTD", "Business Purpose:", "Nothing conclusive."])
    result = _upload(client, "trip-reports", "no_trip_id.pdf", pdf_bytes).json()["results"][0]
    assert result["success"] is True
    detail = client.get(f"/api/documents/{result['document_id']}/extraction").json()
    assert detail["extraction_status"] == "REVIEW_REQUIRED"
    assert detail["trip"]["trip_id"] is None


# ---------- 14: OCR fallback architecture ----------


def test_ocr_fallback_extracts_text_from_image_only_page():
    from app.services.extraction.ocr_engine import TesseractOCREngine
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    ocr = TesseractOCREngine()
    if not ocr.is_available():
        pytest.skip("Tesseract is not installed in this environment.")

    pdf_bytes = _make_image_only_pdf("Expense Report ER-99999")
    tmp_path = Path("ocr_test_tmp.pdf")
    tmp_path.write_bytes(pdf_bytes)
    try:
        extractor = PyMuPDFTextExtractor(ocr_engine=ocr)
        native_pages = extractor.split_pages(tmp_path)
        assert native_pages[0]["has_selectable_text"] is False  # confirms this page truly needs OCR

        content = extractor.content_pages(tmp_path)
        assert content[0]["source"] == "ocr"
        assert "ER-99999" in content[0]["text"] or "99999" in content[0]["text"]
    finally:
        tmp_path.unlink(missing_ok=True)


def test_ocr_fallback_escalates_when_confident_but_amount_and_date_both_absent(monkeypatch):
    """Real corpus case: a handwritten taxi receipt's printed template text
    (vendor name, address) reads with decent average confidence, pulling the
    page's overall score above MIN_OCR_CONFIDENCE, while the handwritten
    amount/date filled into the form's blank lines come out as unrecognizable
    noise - neither field is anywhere in the OCR'd text. The old confidence-
    only gate never escalated this page to the optional NVIDIA fallback; it
    must now, since genuinely critical fields are missing despite a
    deceptively fine-looking confidence average."""
    from app.services.extraction import nvidia_ocr
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    class _FakeOCR:
        def extract_text(self, image_bytes):
            return {
                "text": "TAMANG TAXI SERVICE\nRANGPO, EAST SIKKIM\nSignature",
                "confidence": 0.75,  # comfortably above MIN_OCR_CONFIDENCE
                "words": [],
            }

    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    called = {}

    def _fake_fallback(image_bytes):
        called["fired"] = True
        return {"text": "TAMANG TAXI SERVICE\nDate: 19/08/26\nRs. 4000/-", "confidence": 0.9}

    monkeypatch.setattr(nvidia_ocr, "extract_text_from_image", _fake_fallback)

    pdf_bytes = _make_image_only_pdf("irrelevant - OCR is faked below")
    tmp_path = Path("ocr_escalation_test_tmp.pdf")
    tmp_path.write_bytes(pdf_bytes)
    try:
        extractor = PyMuPDFTextExtractor(ocr_engine=_FakeOCR())
        pages = extractor.content_pages(tmp_path)
        assert called.get("fired") is True
        assert pages[0]["source"] == "nvidia_ocr"
        assert "4000" in pages[0]["text"]
    finally:
        tmp_path.unlink(missing_ok=True)


def test_ocr_fallback_does_not_escalate_when_amount_and_date_present(monkeypatch):
    """Regression guard: a confident OCR result that already has a usable
    amount and date must never trigger the (more expensive) NVIDIA fallback,
    confidence-driven escalation or not."""
    from app.services.extraction import nvidia_ocr
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    class _FakeOCR:
        def extract_text(self, image_bytes):
            return {
                "text": "Cash Rs. 388.26\n8/19/26 5:48 am",
                "confidence": 0.7,
                "words": [],
            }

    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")

    def _fail_if_called(image_bytes):
        raise AssertionError("NVIDIA fallback must not fire when amount and date are both already present")

    monkeypatch.setattr(nvidia_ocr, "extract_text_from_image", _fail_if_called)

    pdf_bytes = _make_image_only_pdf("irrelevant - OCR is faked below")
    tmp_path = Path("ocr_no_escalation_test_tmp.pdf")
    tmp_path.write_bytes(pdf_bytes)
    try:
        extractor = PyMuPDFTextExtractor(ocr_engine=_FakeOCR())
        pages = extractor.content_pages(tmp_path)
        assert pages[0]["source"] == "ocr"
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------- 300 DPI OCR retry on completely empty results ----------
#
# TASK: Implement a Targeted 300 DPI OCR Retry for Empty Results. See
# experiments/node-extraction/benchmarks/dpi-200-vs-300/REPORT.md for the
# evidence this is built on: retrying ONLY a completely empty first OCR
# result at 300 DPI captured the one unambiguous win found in that
# benchmark, while a page that already had some (even low-confidence) text
# at the default DPI must never be retried - that benchmark also found a
# real regression on a page like that.


class _SequencedOCR:
    """Returns one scripted result per call, in order. Also records the
    pixel dimensions of each image it was asked to OCR (via PIL), so a test
    can confirm a retry really was rendered at a higher resolution without
    needing to mock PyMuPDF's own rendering."""

    def __init__(self, results: list[dict]):
        self._results = list(results)
        self.calls = 0
        self.image_sizes: list[tuple[int, int]] = []

    def extract_text(self, image_bytes):
        from PIL import Image

        self.image_sizes.append(Image.open(BytesIO(image_bytes)).size)
        self.calls += 1
        return self._results[self.calls - 1]


def _image_only_pdf_tmp(tmp_name: str, text: str = "Expense Report ER-99999") -> Path:
    pdf_bytes = _make_image_only_pdf(text)
    tmp_path = Path(tmp_name)
    tmp_path.write_bytes(pdf_bytes)
    return tmp_path


def test_empty_200dpi_result_retries_and_succeeds_at_300dpi():
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    ocr = _SequencedOCR(
        [
            {"text": "", "confidence": 0.0, "words": []},
            {"text": "AUTO CARE CENTRE\nAMOUNT: 2500.00 INR", "confidence": 0.71, "words": []},
        ]
    )
    tmp_path = _image_only_pdf_tmp("ocr_retry_success_tmp.pdf")
    try:
        extractor = PyMuPDFTextExtractor(ocr_engine=ocr)
        pages = extractor.content_pages(tmp_path)
        assert ocr.calls == 2
        assert ocr.image_sizes[1][0] > ocr.image_sizes[0][0]  # 300 DPI render is wider than the 200 DPI render
        assert pages[0]["source"] == "ocr"
        assert pages[0]["has_selectable_text"] is True
        assert "2500.00" in pages[0]["text"]
        assert pages[0]["ocr_confidence"] == 0.71
    finally:
        tmp_path.unlink(missing_ok=True)


def test_empty_at_both_resolutions_leaves_page_without_usable_text():
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    ocr = _SequencedOCR(
        [
            {"text": "", "confidence": 0.0, "words": []},
            {"text": "", "confidence": 0.0, "words": []},
        ]
    )
    tmp_path = _image_only_pdf_tmp("ocr_retry_both_empty_tmp.pdf")
    try:
        extractor = PyMuPDFTextExtractor(ocr_engine=ocr)
        pages = extractor.content_pages(tmp_path)
        assert ocr.calls == 2
        assert pages[0]["source"] == "native"  # unchanged - the retry also came back empty
        assert pages[0]["has_selectable_text"] is False
    finally:
        tmp_path.unlink(missing_ok=True)


def test_whitespace_only_first_result_triggers_retry():
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    ocr = _SequencedOCR(
        [
            {"text": "   \n\t  ", "confidence": 0.0, "words": []},
            {"text": "Recovered text", "confidence": 0.6, "words": []},
        ]
    )
    tmp_path = _image_only_pdf_tmp("ocr_retry_whitespace_tmp.pdf")
    try:
        extractor = PyMuPDFTextExtractor(ocr_engine=ocr)
        pages = extractor.content_pages(tmp_path)
        assert ocr.calls == 2
        assert pages[0]["text"] == "Recovered text"
    finally:
        tmp_path.unlink(missing_ok=True)


def test_non_empty_low_confidence_first_result_does_not_retry():
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    ocr = _SequencedOCR([{"text": "some low-confidence text", "confidence": 0.1, "words": []}])
    tmp_path = _image_only_pdf_tmp("ocr_no_retry_low_confidence_tmp.pdf")
    try:
        extractor = PyMuPDFTextExtractor(ocr_engine=ocr)
        pages = extractor.content_pages(tmp_path)
        assert ocr.calls == 1  # never retried - only a completely EMPTY result retries, never low confidence alone
        assert pages[0]["text"] == "some low-confidence text"
    finally:
        tmp_path.unlink(missing_ok=True)


def test_first_ocr_exception_propagates_unchanged():
    """Locks in existing (unchanged) behaviour: an exception from the FIRST,
    default-DPI OCR call is outside this task's scope (only RETRY
    exceptions must degrade safely) - it must still propagate exactly as it
    did before this change."""
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    class _BoomOnFirstCall:
        def extract_text(self, image_bytes):
            raise RuntimeError("boom")

    tmp_path = _image_only_pdf_tmp("ocr_first_call_exception_tmp.pdf")
    try:
        extractor = PyMuPDFTextExtractor(ocr_engine=_BoomOnFirstCall())
        with pytest.raises(RuntimeError, match="boom"):
            extractor.content_pages(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def test_retry_exception_does_not_crash_extraction():
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    class _EmptyThenBoom:
        def __init__(self):
            self.calls = 0

        def extract_text(self, image_bytes):
            self.calls += 1
            if self.calls == 1:
                return {"text": "", "confidence": 0.0, "words": []}
            raise RuntimeError("retry boom")

    ocr = _EmptyThenBoom()
    tmp_path = _image_only_pdf_tmp("ocr_retry_exception_tmp.pdf")
    try:
        extractor = PyMuPDFTextExtractor(ocr_engine=ocr)
        pages = extractor.content_pages(tmp_path)  # must not raise
        assert ocr.calls == 2
        assert pages[0]["source"] == "native"
        assert pages[0]["has_selectable_text"] is False
    finally:
        tmp_path.unlink(missing_ok=True)


def test_exactly_one_retry_per_page():
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    ocr = _SequencedOCR(
        [
            {"text": "", "confidence": 0.0, "words": []},
            {"text": "", "confidence": 0.0, "words": []},
        ]
    )
    tmp_path = _image_only_pdf_tmp("ocr_retry_exactly_once_tmp.pdf")
    try:
        extractor = PyMuPDFTextExtractor(ocr_engine=ocr)
        extractor.content_pages(tmp_path)
        assert ocr.calls == 2  # the default-DPI attempt plus exactly one 300 DPI retry, never more
    finally:
        tmp_path.unlink(missing_ok=True)


def test_retry_preserves_page_number_and_result_structure():
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    ocr = _SequencedOCR(
        [
            {"text": "", "confidence": 0.0, "words": []},
            {"text": "Recovered", "confidence": 0.65, "words": []},
        ]
    )
    tmp_path = _image_only_pdf_tmp("ocr_retry_structure_tmp.pdf")
    try:
        extractor = PyMuPDFTextExtractor(ocr_engine=ocr)
        pages = extractor.content_pages(tmp_path)
        page = pages[0]
        assert page["page_number"] == 1  # single-page test PDF - the retry must target the same page
        assert set(page.keys()) >= {"page_number", "text", "has_selectable_text", "source", "ocr_confidence"}
        assert isinstance(page["text"], str)
    finally:
        tmp_path.unlink(missing_ok=True)


def test_retry_does_not_duplicate_or_concatenate_text():
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    ocr = _SequencedOCR(
        [
            {"text": "", "confidence": 0.0, "words": []},
            {"text": "Only the retry's text", "confidence": 0.65, "words": []},
        ]
    )
    tmp_path = _image_only_pdf_tmp("ocr_retry_no_duplication_tmp.pdf")
    try:
        extractor = PyMuPDFTextExtractor(ocr_engine=ocr)
        pages = extractor.content_pages(tmp_path)
        assert pages[0]["text"] == "Only the retry's text"
        assert pages[0]["text"].count("Only the retry's text") == 1
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------- 15: extraction failure handling ----------


def test_reprocess_handles_missing_stored_file_gracefully(client, sample_pdf_path):
    result = _upload(client, "expense-reports", sample_pdf_path.name, sample_pdf_path.read_bytes()).json()
    doc_id = result["results"][0]["document_id"]
    Path(_storage_path_for(client, doc_id)).unlink(missing_ok=True)

    resp = client.post(f"/api/documents/{doc_id}/reprocess")
    assert resp.status_code == 200  # never a 500, even though the file vanished
    body = resp.json()
    assert body["extraction_status"] == "FAILED"


def _storage_path_for(client, doc_id) -> str:
    from app.database import SessionLocal
    from app.models.expense_report import ExpenseReport

    db = SessionLocal()
    try:
        pk = int(doc_id.split(":")[1])
        row = db.get(ExpenseReport, pk)
        return row.source_path
    finally:
        db.close()


# ---------- 16: reprocessing is idempotent ----------


def test_reprocessing_is_idempotent_no_duplicate_rows(client, sample_pdf_path):
    result = _upload(client, "expense-reports", sample_pdf_path.name, sample_pdf_path.read_bytes()).json()
    doc_id = result["results"][0]["document_id"]

    first = client.get(f"/api/documents/{doc_id}/extraction").json()
    client.post(f"/api/documents/{doc_id}/reprocess")
    client.post(f"/api/documents/{doc_id}/reprocess")
    second = client.get(f"/api/documents/{doc_id}/extraction").json()

    assert len(first["expenses"]) == len(second["expenses"]) == 12
    listing = client.get("/api/expense-reports").json()
    assert len(listing) == 1  # reprocessing must never create a duplicate document


# ---------- 17: bulk extraction isolation ----------


def test_bulk_extraction_one_bad_document_does_not_block_others(client):
    files = [("files", (name, (REPO_ROOT / name).read_bytes(), "application/pdf")) for name in ALL_ER_PDFS]
    files.append(("files", ("bad.pdf", b"not a real pdf", "application/pdf")))
    resp = client.post("/api/uploads/expense-reports", files=files)
    results = resp.json()["results"]
    succeeded = [r for r in results if r["success"]]
    assert len(succeeded) == 4
    for r in succeeded:
        detail = client.get(f"/api/documents/{r['document_id']}/extraction").json()
        assert detail["extraction_status"] in ("EXTRACTED", "REVIEW_REQUIRED")


# ---------- 20/21: Phase 1 & 2 regression ----------


def test_phase1_policy_master_regression(client):
    policies = client.get("/api/policy-master").json()
    assert len(policies) == 29


def test_phase2_upload_and_download_regression(client, sample_pdf_path):
    content = sample_pdf_path.read_bytes()
    result = _upload(client, "expense-reports", sample_pdf_path.name, content).json()["results"][0]
    assert result["success"]
    file_resp = client.get(f"/api/documents/{result['document_id']}/file")
    assert file_resp.content == content
