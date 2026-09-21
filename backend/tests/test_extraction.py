from io import BytesIO
from pathlib import Path

import fitz
import pytest

from app.services.extraction.classifier import EXPENSE_REPORT, TRIP_REPORT, UNKNOWN, classify_text
from app.services.extraction.dates import parse_date
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
    assert detail["extraction_confidence"] == "HIGH"
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
