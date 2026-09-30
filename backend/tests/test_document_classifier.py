"""Tests for the optional NVIDIA-backed supporting-document classifier
(document_classifier.py) and its wiring into bill_pipeline.py as a
last-resort fallback for pages bill_parser.py's own deterministic rules
found no recognizable evidence on at all. NVIDIA is always mocked here - no
automated test makes a real network call.

Sections:
  1-2:   disabled/no-op behavior
  3-9:   valid classification, one per document_type, full field extraction
  10-15: defensive parsing of malformed/ambiguous/out-of-schema responses
  16-17: failure modes never raise
  18-21: bill_pipeline.py integration - fallback trigger point, source page,
         always-REVIEW_REQUIRED, and that a confidently-extracted page never
         even calls the classifier
  22:    NOT_APPLICABLE produces no Bill row
  23:    a classifier-derived bill still goes through ordinary bill_matcher
         scoring - ONE signal among several, never an automatic match
  24:    check_catering falls back to bill.catering_amount when the
         deterministic regex finds nothing
"""

import json
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import fitz
import httpx
import pytest

from app.database import SessionLocal
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.services.extraction import bill_pipeline, document_classifier
from app.services.extraction.text_extraction import PyMuPDFTextExtractor
from app.services.validation import checks


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_body: dict | None = None):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.text = str(json_body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json_body


def _chat_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _full_payload(**overrides) -> dict:
    payload = {
        "document_type": "BILL",
        "confidence": 0.8,
        "vendor": "Acme Traders",
        "invoice_number": "INV-9911",
        "transaction_reference": "TXN123456",
        "date": "2026-08-07",
        "total_amount": "546.00",
        "catering_amount": "120.00",
        "currency": "INR",
    }
    payload.update(overrides)
    return payload


# ---------- 1-2. Disabled / no-op ----------


def test_disabled_when_no_api_key(monkeypatch):
    monkeypatch.delenv("SPINCO_NVIDIA_API_KEY", raising=False)
    assert document_classifier.is_available() is False


def test_disabled_returns_none_without_any_http_call(monkeypatch):
    monkeypatch.delenv("SPINCO_NVIDIA_API_KEY", raising=False)

    def _fail_if_called(*a, **k):
        raise AssertionError("httpx.post must never be called when NVIDIA is disabled")

    monkeypatch.setattr(httpx, "post", _fail_if_called)
    assert document_classifier.classify_page("some ambiguous page text") is None


def test_empty_text_returns_none_without_any_http_call(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")

    def _fail_if_called(*a, **k):
        raise AssertionError("httpx.post must never be called for empty text")

    monkeypatch.setattr(httpx, "post", _fail_if_called)
    assert document_classifier.classify_page("   ") is None
    assert document_classifier.classify_page("") is None


# ---------- 3-9. Valid classification per document_type, full field set ----------


@pytest.mark.parametrize(
    "doc_type",
    ["BILL", "RECEIPT", "TICKET", "TRANSACTION_SCREENSHOT", "INVOICE", "OTHER"],
)
def test_valid_classification_each_document_type(monkeypatch, doc_type):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(json.dumps(_full_payload(document_type=doc_type))))
    )
    result = document_classifier.classify_page("some page text")
    assert result is not None
    assert result.document_type == doc_type
    assert result.type_confidence == 0.8
    assert result.vendor.value == "Acme Traders"
    assert result.invoice_number.value == "INV-9911"
    assert result.transaction_reference.value == "TXN123456"
    assert result.date.value == date(2026, 8, 7)
    assert result.total_amount.value == 546.00
    assert result.catering_amount.value == 120.00
    assert result.vendor.confidence == "MEDIUM"  # confidence 0.8 >= 0.5


def test_not_applicable_carries_no_evidence_fields(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    payload = {"document_type": "NOT_APPLICABLE", "confidence": 0.9}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(json.dumps(payload))))
    result = document_classifier.classify_page("blank page / signature only")
    assert result is not None
    assert result.document_type == "NOT_APPLICABLE"
    assert result.vendor is None
    assert result.total_amount is None


def test_low_confidence_yields_low_field_confidence(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    payload = _full_payload(confidence=0.3)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(json.dumps(payload))))
    result = document_classifier.classify_page("some page text")
    assert result is not None
    assert result.vendor.confidence == "LOW"


def test_string_amount_is_reparsed_through_parse_amount(monkeypatch):
    """The model's own amount string must survive the same deterministic
    parser as everything else - not be trusted verbatim."""
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    payload = _full_payload(total_amount="Rs. 1,234.50", catering_amount=None)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(json.dumps(payload))))
    result = document_classifier.classify_page("some page text")
    assert result.total_amount.value == 1234.50
    assert result.catering_amount is None


def test_non_iso_date_is_dropped_not_guessed(monkeypatch):
    """The prompt requires ISO YYYY-MM-DD; a model that ignores the
    instruction and returns a different format must not be guessed at - the
    date field is simply left absent, everything else still validates."""
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    payload = _full_payload(date="07/08/2026")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(json.dumps(payload))))
    result = document_classifier.classify_page("some page text")
    assert result is not None
    assert result.date is None
    assert result.vendor.value == "Acme Traders"  # rest of the result is unaffected


# ---------- 10-15. Defensive parsing of malformed/out-of-schema responses ----------


def test_invalid_document_type_enum_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    payload = _full_payload(document_type="RANDOM_MADE_UP_TYPE")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(json.dumps(payload))))
    assert document_classifier.classify_page("some page text") is None


def test_missing_document_type_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    payload = {"confidence": 0.8}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(json.dumps(payload))))
    assert document_classifier.classify_page("some page text") is None


def test_non_numeric_confidence_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    payload = _full_payload(confidence="very sure")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(json.dumps(payload))))
    assert document_classifier.classify_page("some page text") is None


def test_out_of_range_confidence_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    payload = _full_payload(confidence=1.5)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(json.dumps(payload))))
    assert document_classifier.classify_page("some page text") is None


def test_unparseable_json_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response("not json at all {{{")))
    assert document_classifier.classify_page("some page text") is None


def test_code_fenced_json_is_recovered(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    payload_str = json.dumps(_full_payload(document_type="RECEIPT"))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(f"```json\n{payload_str}\n```")))
    result = document_classifier.classify_page("some page text")
    assert result is not None
    assert result.document_type == "RECEIPT"


def test_prose_wrapped_json_is_recovered(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    payload_str = json.dumps(_full_payload(document_type="TICKET"))
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(f"Here is the classification:\n{payload_str}\nHope that helps!"))
    )
    result = document_classifier.classify_page("some page text")
    assert result is not None
    assert result.document_type == "TICKET"


# ---------- 16-17. Failure modes must never raise ----------


def test_timeout_returns_none_without_raising(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.TimeoutException("timed out")))
    assert document_classifier.classify_page("some page text") is None


def test_api_error_5xx_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(500, {}))
    assert document_classifier.classify_page("some page text") is None


# ---------- Shared explicit kill-switch honored (same as nvidia_ocr/nvidia_fallback) ----------


def test_explicit_enabled_false_overrides_configured_key(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "false")
    assert document_classifier.is_available() is False


# ---------- 18-21. bill_pipeline.py integration ----------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AMBIGUOUS_TEXT = "Session reference 88213 recorded. Please retain this note for your records. " * 2
CLEAR_BILL_TEXT = "Ride fare 520.00\nCGST 13.00\nSGST 13.00\nTotal 546.00"


def _make_report(db, source_path: Path, **kwargs) -> ExpenseReport:
    defaults = dict(
        filename="test.pdf",
        original_filename="test.pdf",
        file_hash=f"hash-{uuid.uuid4()}",
        file_size=100,
        source_path=str(source_path),
        extraction_status="EXTRACTED",
        total_claimed=0.0,
    )
    defaults.update(kwargs)
    row = ExpenseReport(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _one_page_pdf(tmp_path: Path, text: str) -> Path:
    pdf_path = tmp_path / f"{uuid.uuid4()}.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(pdf_path)
    doc.close()
    return pdf_path


def test_ambiguous_page_triggers_classifier_fallback_and_creates_review_required_bill(client, monkeypatch, tmp_path):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    payload = _full_payload(document_type="TRANSACTION_SCREENSHOT", total_amount="899.00", catering_amount=None)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(json.dumps(payload))))

    pdf_path = _one_page_pdf(tmp_path, AMBIGUOUS_TEXT)
    db = SessionLocal()
    try:
        report = _make_report(db, pdf_path)
        extractor = PyMuPDFTextExtractor()
        bill_pipeline.extract_and_match_bills(db, report, extractor, content_last_page=0)

        bills = db.query(Bill).filter(Bill.expense_report_id == report.id).all()
        assert len(bills) == 1
        bill = bills[0]
        assert bill.document_type == "TRANSACTION_SCREENSHOT"
        assert bill.extraction_status == "REVIEW_REQUIRED"  # never a confident result on its own
        assert bill.page_number == 1  # source page preserved
        assert float(bill.amount) == 899.00
        assert bill.extraction_notes is not None and "classified" in bill.extraction_notes.lower()
    finally:
        db.close()


def test_not_applicable_classification_creates_no_bill(client, monkeypatch, tmp_path):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    payload = {"document_type": "NOT_APPLICABLE", "confidence": 0.95}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(json.dumps(payload))))

    pdf_path = _one_page_pdf(tmp_path, AMBIGUOUS_TEXT)
    db = SessionLocal()
    try:
        report = _make_report(db, pdf_path)
        extractor = PyMuPDFTextExtractor()
        bill_pipeline.extract_and_match_bills(db, report, extractor, content_last_page=0)
        bills = db.query(Bill).filter(Bill.expense_report_id == report.id).all()
        assert bills == []
    finally:
        db.close()


def test_classifier_disabled_drops_ambiguous_page_exactly_as_before(client, monkeypatch, tmp_path):
    monkeypatch.delenv("SPINCO_NVIDIA_API_KEY", raising=False)

    def _fail_if_called(*a, **k):
        raise AssertionError("classifier must not call NVIDIA when disabled")

    monkeypatch.setattr(httpx, "post", _fail_if_called)

    pdf_path = _one_page_pdf(tmp_path, AMBIGUOUS_TEXT)
    db = SessionLocal()
    try:
        report = _make_report(db, pdf_path)
        extractor = PyMuPDFTextExtractor()
        bill_pipeline.extract_and_match_bills(db, report, extractor, content_last_page=0)
        bills = db.query(Bill).filter(Bill.expense_report_id == report.id).all()
        assert bills == []  # unchanged from pre-feature behavior
    finally:
        db.close()


def test_deterministically_extracted_page_never_calls_classifier(client, monkeypatch, tmp_path):
    """Performance guarantee: the classifier is a LAST-RESORT fallback, never
    invoked for a page bill_parser.py already extracted evidence from."""
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")

    def _fail_if_called(*a, **k):
        raise AssertionError("classifier must not be called for a page with deterministic evidence")

    monkeypatch.setattr(httpx, "post", _fail_if_called)

    pdf_path = _one_page_pdf(tmp_path, CLEAR_BILL_TEXT)
    db = SessionLocal()
    try:
        report = _make_report(db, pdf_path)
        extractor = PyMuPDFTextExtractor()
        bill_pipeline.extract_and_match_bills(db, report, extractor, content_last_page=0)
        bills = db.query(Bill).filter(Bill.expense_report_id == report.id).all()
        assert len(bills) == 1
        assert bills[0].amount == 546.00
        assert bills[0].document_type is None  # ordinary deterministic bill, not classifier-derived
    finally:
        db.close()


# ---------- 23. Classifier-derived bill is ONE signal among several, never an automatic match ----------


def test_classifier_derived_bill_flows_through_ordinary_matcher(client, monkeypatch, tmp_path):
    """A classifier-derived Bill row must go through the exact same
    bill_matcher.match_expense() scoring as any other bill - amount+date
    exact match still scores HIGH and matches; a plausible-but-wrong amount
    does not match just because document_type happens to be BILL."""
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    payload = _full_payload(document_type="BILL", total_amount="899.00", date="2026-08-07", catering_amount=None)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(json.dumps(payload))))

    pdf_path = _one_page_pdf(tmp_path, AMBIGUOUS_TEXT)
    db = SessionLocal()
    try:
        report = _make_report(db, pdf_path)
        expense = Expense(
            expense_report_id=report.id,
            s_no=1,
            expense_date=date(2026, 8, 7),
            category="CAB CHARGES",
            claimed_amount=899.00,
            bill_match_status="NOT_PROCESSED",
            trip_match_status="NOT_PROCESSED",
        )
        db.add(expense)
        db.commit()

        extractor = PyMuPDFTextExtractor()
        bill_pipeline.extract_and_match_bills(db, report, extractor, content_last_page=0)

        db.refresh(expense)
        # Exact amount (+40) + exact date (+30) = 70 -> MATCHED, HIGH - the
        # SAME deterministic scoring path every other bill goes through, not
        # an automatic match just because document_type == BILL.
        assert expense.bill_match_status == "MATCHED"
        assert expense.bill_match_confidence == "HIGH"
    finally:
        db.close()


# ---------- 24. check_catering falls back to bill.catering_amount ----------


def test_check_catering_falls_back_to_classifier_catering_amount():
    """No regex-detectable catering line on this bill's raw_text (it was
    recovered via the classifier fallback, which doesn't run bill_parser's
    label-pattern regex) - check_catering must still surface it using the
    classifier's own catering_amount field."""
    fake_bill = SimpleNamespace(
        raw_text="Transaction confirmed. Reference TXN123456. Total Rs. 2804.05.",
        page_number=3,
        catering_amount=368.00,
    )
    result = checks.check_catering(category="TRAIN / BUS FARE", bill=fake_bill, claimed_amount=2804.05)
    assert result.result == checks.REVIEW_REQUIRED
    assert "368.00" in result.details


def test_check_catering_deterministic_regex_still_takes_priority():
    """When both the regex AND bill.catering_amount could answer, the
    regex-detected figure (from the bill's own text) wins - the classifier
    field is a fallback only, never overrides bill_parser's own extraction."""
    fake_bill = SimpleNamespace(
        raw_text="Ticket Fare: Rs. 2412.00\nCatering Charges (Incl. of GST): Rs. 368.00\nTotal: Rs. 2804.05",
        page_number=1,
        catering_amount=999.00,  # deliberately different - must be ignored
    )
    result = checks.check_catering(category="TRAIN / BUS FARE", bill=fake_bill, claimed_amount=2804.05)
    assert result.result == checks.REVIEW_REQUIRED
    assert "368.00" in result.details
    assert "999.00" not in result.details


def test_check_catering_no_evidence_at_all_still_passes():
    fake_bill = SimpleNamespace(raw_text="Ride fare 520.00\nTotal 546.00", page_number=1, catering_amount=None)
    result = checks.check_catering(category="TRAIN / BUS FARE", bill=fake_bill, claimed_amount=546.00)
    assert result.result == checks.PASS
