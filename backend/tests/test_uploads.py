from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter

from app.database import SessionLocal, engine
from app.models.expense_report import ExpenseReport

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

ALL_ER_PDFS = [
    "ER-39832.pdf",
    "ER-40223.pdf",
    "ER-40706 (1).pdf",
    "ER-40782.pdf",
]

ALL_TRIP_PDFS = [
    "TRIP-24935_Trip_To_Indore_Pithampur.pdf",
    "TRIP-25167_Raipur,_Billai,_Ahmedabad-_27thJuly_-30th_July_2026.pdf",
    "TRIP-25845_Chandigarh_tour30.06.2026_to_12.07.2026.pdf",
    "TRIP-25921_Indore.pdf",
]


def _upload(client, endpoint, filename, content):
    return client.post(f"/api/uploads/{endpoint}", files=[("files", (filename, content, "application/pdf"))])


def _encrypted_pdf_bytes(password="secret") -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(password)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


# 1. Single Expense Report upload
def test_single_expense_report_upload(client, sample_pdf_path):
    resp = _upload(client, "expense-reports", sample_pdf_path.name, sample_pdf_path.read_bytes())
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["success"] is True
    assert result["processing_status"] == "PROCESSED"
    assert result["document_number"] == "ER-39832"
    assert result["page_count"] and result["page_count"] > 0
    assert result["document_id"] == "expense:1" or result["document_id"].startswith("expense:")


# 2. Single Trip Report upload
def test_single_trip_report_upload(client, sample_trip_pdf_path):
    resp = _upload(client, "trip-reports", sample_trip_pdf_path.name, sample_trip_pdf_path.read_bytes())
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["success"] is True
    assert result["document_number"] == "TRIP-24935"
    assert result["document_id"].startswith("trip:")


# 3. Multiple PDF upload (all 4 real ER PDFs at once)
def test_multiple_pdf_upload(client):
    files = [
        ("files", (name, (REPO_ROOT / name).read_bytes(), "application/pdf")) for name in ALL_ER_PDFS
    ]
    resp = client.post("/api/uploads/expense-reports", files=files)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 4
    assert all(r["success"] for r in results)
    numbers = {r["document_number"] for r in results}
    assert numbers == {"ER-39832", "ER-40223", "ER-40706", "ER-40782"}


# 4. Invalid file rejection
def test_invalid_file_rejected(client):
    resp = _upload(client, "expense-reports", "not-a-pdf.pdf", b"this is just plain text, not a pdf")
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["success"] is False
    assert result["error_message"]
    assert "list_expense_reports" not in result["error_message"]  # sanity: no leaked internals

    # confirm no phantom DB row was created
    listing = client.get("/api/expense-reports").json()
    assert listing == []


def test_wrong_extension_rejected(client):
    resp = _upload(client, "expense-reports", "report.docx", b"%PDF-1.4 fake content")
    result = resp.json()["results"][0]
    assert result["success"] is False
    assert "PDF" in result["error_message"]


# 5. Duplicate file detection
def test_duplicate_file_detected(client, sample_pdf_path):
    content = sample_pdf_path.read_bytes()
    first = _upload(client, "expense-reports", sample_pdf_path.name, content).json()["results"][0]
    second = _upload(client, "expense-reports", sample_pdf_path.name, content).json()["results"][0]

    assert first["success"] is True
    assert second["success"] is False
    assert second["error_message"] == "File already exists."

    listing = client.get("/api/expense-reports").json()
    assert len(listing) == 1


# 6. Corrupted PDF handling
def test_corrupted_pdf_handling(client):
    resp = _upload(client, "expense-reports", "corrupted.pdf", b"%PDF-1.4\ngarbage garbage garbage not a real pdf")
    assert resp.status_code == 200  # never a 500 - handled gracefully
    result = resp.json()["results"][0]
    assert result["success"] is False
    assert "corrupted" in result["error_message"].lower() or "could not be read" in result["error_message"].lower()


def test_zero_byte_file(client):
    resp = _upload(client, "expense-reports", "empty.pdf", b"")
    result = resp.json()["results"][0]
    assert result["success"] is False
    assert "empty" in result["error_message"].lower()


def test_password_protected_pdf(client):
    resp = _upload(client, "expense-reports", "protected.pdf", _encrypted_pdf_bytes())
    result = resp.json()["results"][0]
    assert result["success"] is False
    assert "password" in result["error_message"].lower()


# 7. Database persistence
def test_database_persistence(client, sample_pdf_path):
    _upload(client, "expense-reports", sample_pdf_path.name, sample_pdf_path.read_bytes())
    listing = client.get("/api/expense-reports").json()
    assert len(listing) == 1
    row = listing[0]
    assert row["er_id"] == "ER-39832"
    assert row["processing_status"] == "PROCESSED"
    assert row["file_size"] > 0
    # Phase 3 auto-extracts immediately after upload, so this is now real extracted
    # data, not a guess - see tests/test_extraction.py for the extraction-specific coverage.
    assert row["employee_name"] == "MANEESH KUMAR P. N."
    # Phase 5 (policy validation) and Phase 7 (exception engine) both now run
    # automatically as part of upload/extraction, so overall_status is real,
    # populated data - not a guess. ER-39832 genuinely has confirmed
    # exceptions elsewhere in the document (missing/duplicate bills, one
    # over-limit line - see tests/test_exception_engine.py), so EXCEPTION is
    # the correct, honest verdict here, never ACCOUNT VERIFIED.
    assert row["overall_status"] == "EXCEPTION"


# 8 & 9. Storage persistence + PDF retrieval
def test_storage_persistence_and_retrieval(client, sample_pdf_path):
    original_bytes = sample_pdf_path.read_bytes()
    upload_result = _upload(client, "expense-reports", sample_pdf_path.name, original_bytes).json()["results"][0]
    doc_id = upload_result["document_id"]

    file_resp = client.get(f"/api/documents/{doc_id}/file")
    assert file_resp.status_code == 200
    assert file_resp.headers["content-type"] == "application/pdf"
    assert file_resp.content == original_bytes


# 11. Download button (content-disposition attachment)
def test_download_sets_attachment_header(client, sample_pdf_path):
    upload_result = _upload(client, "expense-reports", sample_pdf_path.name, sample_pdf_path.read_bytes()).json()[
        "results"
    ][0]
    doc_id = upload_result["document_id"]

    resp = client.get(f"/api/documents/{doc_id}/file", params={"download": "true"})
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]

    inline_resp = client.get(f"/api/documents/{doc_id}/file")
    assert "inline" in inline_resp.headers["content-disposition"]


# 12. One failed file among multiple
def test_one_failed_file_among_multiple(client):
    good_names = ALL_ER_PDFS[:3]
    files = [("files", (name, (REPO_ROOT / name).read_bytes(), "application/pdf")) for name in good_names]
    files.append(("files", ("bad.pdf", b"not a real pdf at all", "application/pdf")))

    resp = client.post("/api/uploads/expense-reports", files=files)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 4
    succeeded = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    assert len(succeeded) == 3
    assert len(failed) == 1

    listing = client.get("/api/expense-reports").json()
    assert len(listing) == 3  # the failed file left no DB row


# Document detail + delete
def test_document_detail_and_delete(client, sample_pdf_path):
    upload_result = _upload(client, "expense-reports", sample_pdf_path.name, sample_pdf_path.read_bytes()).json()[
        "results"
    ][0]
    doc_id = upload_result["document_id"]

    detail = client.get(f"/api/documents/{doc_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["document_number"] == "ER-39832"
    assert body["file_hash"] is not None
    assert body["person_name"] == "MANEESH KUMAR P. N."  # Phase 3: real extracted data, not a guess

    delete_resp = client.delete(f"/api/documents/{doc_id}")
    assert delete_resp.status_code == 204

    assert client.get(f"/api/documents/{doc_id}").status_code == 404
    assert client.get("/api/expense-reports").json() == []


def test_document_not_found(client):
    assert client.get("/api/documents/expense:999999").status_code == 404
    assert client.get("/api/documents/not-a-real-id").status_code == 400


# 13. Application restart (simulated: dispose the connection pool, re-query)
def test_application_restart_persistence(client, sample_pdf_path):
    _upload(client, "expense-reports", sample_pdf_path.name, sample_pdf_path.read_bytes())

    engine.dispose()  # forces brand-new DB connections, proving data is on disk, not just cached

    db = SessionLocal()
    try:
        rows = db.query(ExpenseReport).all()
        assert len(rows) == 1
        assert rows[0].er_id == "ER-39832"
    finally:
        db.close()


# 14 & 15. Existing Phase 1 endpoints/regression
def test_phase1_policy_api_regression(client):
    resp = client.get("/api/policy-master")
    assert resp.status_code == 200
    policies = resp.json()
    assert len(policies) == 29
    by_category = {p["category"]: p for p in policies}
    assert by_category["OWN CAR PETROL"]["limit_amount"] == 8500
    assert by_category["BATTA"]["reimbursement_basis"] == "ACTUAL"
    assert by_category["BATTA"]["document_required"] == "DNA"


def test_phase1_health_regression(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_phase1_users_regression(client):
    me = client.get("/api/users/me").json()
    assert me["email"] == "intern@spincotech.com"
    assert me["role"] == "ADMIN"


def test_phase1_dashboard_kpis_regression(client):
    kpis = client.get("/api/dashboard/kpis").json()
    # Phase 9 legitimately extended this response with review_required_reports
    # and total_expenses (both explicit Home KPI requirements) - this test
    # predates that extension.
    assert kpis == {
        "total_expense_reports": 0,
        "total_trip_reports": 0,
        "total_claimed_amount": 0.0,
        "account_verified_reports": 0,
        "exception_reports": 0,
        "review_required_reports": 0,
        "total_expenses": 0,
    }


def test_all_sample_trip_pdfs_upload_cleanly(client):
    files = [
        ("files", (name, (REPO_ROOT / name).read_bytes(), "application/pdf"))
        for name in ALL_TRIP_PDFS
    ]
    resp = client.post("/api/uploads/trip-reports", files=files)
    results = resp.json()["results"]
    assert all(r["success"] for r in results), results
