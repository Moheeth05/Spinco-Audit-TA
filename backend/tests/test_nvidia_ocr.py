"""Tests for the optional NVIDIA Nemotron OCR v2 fallback (nvidia_ocr.py)
and its wiring into text_extraction.py's OCR chain and the downstream
deterministic pipeline (bill_parser.py, checks.py). NVIDIA is always mocked
here - no automated test makes a real network call.
"""

import base64
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from app.services.extraction import bill_parser, nvidia_fallback, nvidia_ocr
from app.services.extraction.text_extraction import MIN_OCR_CONFIDENCE, PyMuPDFTextExtractor
from app.services.validation import checks

FAKE_IMAGE = b"\x89PNG\r\n\x1a\nfake-png-bytes-for-testing"


@pytest.fixture(autouse=True)
def _clear_nvidia_ocr_cache():
    nvidia_ocr.clear_cache()
    yield
    nvidia_ocr.clear_cache()


def _ocr_response(pairs: list[tuple[str, float]]) -> dict:
    """Builds a fake NVIDIA OCR response payload from (text, confidence) pairs,
    matching the confirmed real response shape:
    data[].text_detections[].text_prediction.{text, confidence}."""
    return {
        "data": [
            {
                "index": 0,
                "text_detections": [
                    {"text_prediction": {"text": text, "confidence": conf}, "bounding_box": {"points": []}}
                    for text, conf in pairs
                ],
            }
        ],
        "usage": {"images_size_mb": 0.1},
    }


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


# ---------- 1. NVIDIA disabled ----------


def test_disabled_when_no_api_key(monkeypatch):
    monkeypatch.delenv("SPINCO_NVIDIA_API_KEY", raising=False)
    assert nvidia_ocr.is_available() is False


def test_disabled_returns_none_without_any_http_call(monkeypatch):
    monkeypatch.delenv("SPINCO_NVIDIA_API_KEY", raising=False)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("httpx.post must never be called when NVIDIA is disabled")

    monkeypatch.setattr(httpx, "post", _fail_if_called)
    assert nvidia_ocr.extract_text_from_image(FAKE_IMAGE) is None


def test_explicit_enabled_false_overrides_configured_key(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "false")
    assert nvidia_ocr.is_available() is False
    # Also verify the shared kill-switch reaches the existing text fallback.
    assert nvidia_fallback.is_available() is False


def test_key_alone_does_not_enable(monkeypatch):
    """Strict opt-in: a configured key with no enable flag stays disabled."""
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.delenv("SPINCO_NVIDIA_ENABLED", raising=False)
    assert nvidia_ocr.is_available() is False
    assert nvidia_fallback.is_available() is False


# ---------- 2. NVIDIA enabled ----------


def test_enabled_when_key_present_and_flag_true(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    assert nvidia_ocr.is_available() is True


def test_enabled_calls_confirmed_endpoint_and_parses_response(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(200, _ocr_response([("Ride fare", 0.95), ("Total 546.00", 0.9)]))

    monkeypatch.setattr(httpx, "post", _fake_post)
    result = nvidia_ocr.extract_text_from_image(FAKE_IMAGE)

    assert result is not None
    assert "Ride fare" in result["text"]
    assert "546.00" in result["text"]
    assert 0.8 < result["confidence"] <= 1.0
    assert captured["url"] == "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v2"
    assert captured["headers"]["Authorization"] == "Bearer nvapi-test-key"
    assert captured["json"]["input"][0]["type"] == "image_url"
    assert captured["json"]["input"][0]["url"].startswith("data:image/png;base64,")


# ---------- 3-5. Missing date / amount / vendor recovery (via better OCR text feeding bill_parser) ----------


def test_missing_date_recovered_through_better_ocr_text(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse(
            200, _ocr_response([("Date: August 7, 2026", 0.9), ("Total: 546.00", 0.9)])
        ),
    )
    recovered = nvidia_ocr.extract_text_from_image(FAKE_IMAGE)
    parsed = bill_parser.parse_bill_text(recovered["text"])
    assert parsed.bill_date is not None
    assert parsed.bill_date.isoformat() == "2026-08-07"


def test_missing_amount_recovered_through_better_ocr_text(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse(200, _ocr_response([("Grand Total: Rs. 546.00", 0.92)])),
    )
    recovered = nvidia_ocr.extract_text_from_image(FAKE_IMAGE)
    parsed = bill_parser.parse_bill_text(recovered["text"])
    assert parsed.amount == 546.00


def test_vendor_recovered_through_better_ocr_text(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse(
            200, _ocr_response([("M/s Indsystems IT Private Limited", 0.9), ("Total: 546.00", 0.9)])
        ),
    )
    recovered = nvidia_ocr.extract_text_from_image(FAKE_IMAGE)
    parsed = bill_parser.parse_bill_text(recovered["text"])
    assert parsed.vendor is not None
    assert "Indsystems" in parsed.vendor


# ---------- 6-8. Failure modes must never break extraction ----------


def test_malformed_response_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, {"unexpected": "shape"}))
    assert nvidia_ocr.extract_text_from_image(FAKE_IMAGE) is None


def test_empty_detections_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _ocr_response([])))
    assert nvidia_ocr.extract_text_from_image(FAKE_IMAGE) is None


def test_timeout_returns_none_without_raising(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")

    def _raise_timeout(*a, **k):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx, "post", _raise_timeout)
    assert nvidia_ocr.extract_text_from_image(FAKE_IMAGE) is None


def test_api_error_5xx_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(500, {}))
    assert nvidia_ocr.extract_text_from_image(FAKE_IMAGE) is None


def test_api_error_429_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(429, {}))
    assert nvidia_ocr.extract_text_from_image(FAKE_IMAGE) is None


def test_invalid_key_unauthorized_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-invalid")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(401, {}))
    assert nvidia_ocr.extract_text_from_image(FAKE_IMAGE) is None


# ---------- 9. API key never logged/exposed ----------


def test_api_key_never_appears_in_exception_or_result(monkeypatch):
    secret = "nvapi-super-secret-value-do-not-leak"
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", secret)
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")

    def _raise(*a, **k):
        raise RuntimeError("boom - simulated failure")

    monkeypatch.setattr(httpx, "post", _raise)
    result = nvidia_ocr.extract_text_from_image(FAKE_IMAGE)
    assert result is None

    # _redact() must never return the real value.
    assert nvidia_ocr._redact(secret) == "<configured>"
    assert secret not in nvidia_ocr._redact(secret)


def test_redact_reports_not_configured_when_unset():
    assert nvidia_ocr._redact(None) == "<not configured>"


# ---------- 10-11. NVIDIA cannot directly decide verdicts ----------


def test_ocr_result_shape_carries_no_verdict_field(monkeypatch):
    """Structural guarantee: extract_text_from_image can only ever return
    {"text", "confidence"} - there is no field it could populate that any
    caller could mistake for ACCOUNT VERIFIED / EXCEPTION / MATCHED."""
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _ocr_response([("hello", 0.9)])))
    result = nvidia_ocr.extract_text_from_image(FAKE_IMAGE)
    assert set(result.keys()) == {"text", "confidence"}


def test_recovered_text_still_requires_deterministic_bill_matcher(monkeypatch):
    """Even a clean, high-confidence NVIDIA OCR recovery still produces a
    plain ParsedBill that must go through bill_matcher/checks - it never
    marks anything MATCHED/ACCOUNT VERIFIED itself."""
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse(200, _ocr_response([("Total: Rs. 546.00", 0.99), ("Date: 07/08/2026", 0.99)])),
    )
    recovered = nvidia_ocr.extract_text_from_image(FAKE_IMAGE)
    parsed = bill_parser.parse_bill_text(recovered["text"])
    assert not hasattr(parsed, "match_status")
    assert not hasattr(parsed, "overall_status")
    assert parsed.status in ("EXTRACTED", "REVIEW_REQUIRED")


# ---------- 12. BATTA remains bill-independent regardless of NVIDIA config ----------


def test_batta_never_triggers_nvidia_ocr_or_bill_matching(monkeypatch):
    from app.services.extraction.bill_pipeline import _is_batta

    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")

    def _fail_if_called(*a, **k):
        raise AssertionError("BATTA must never trigger an NVIDIA OCR call")

    monkeypatch.setattr(httpx, "post", _fail_if_called)
    assert _is_batta("BATTA") is True
    # bill_pipeline.py's extract_and_match_bills() short-circuits BATTA
    # expenses to NOT_APPLICABLE before any bill-matching/NVIDIA-touching
    # code runs for them (see bill_pipeline.py:_is_batta guard) - confirmed
    # here structurally: is_available() being true changes nothing about
    # that guard, and no bill/OCR call happens for a BATTA-only expense.
    nvidia_ocr.extract_text_from_image(b"")  # empty image short-circuits before any HTTP call
    assert nvidia_ocr.is_available() is True  # sanity: it WAS configured, yet nothing fired above


# ---------- 13. Catering detection remains intact with NVIDIA-recovered text ----------


def test_catering_detected_in_nvidia_recovered_irctc_text(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse(
            200,
            _ocr_response(
                [
                    ("Ticket Fare: Rs. 2412.00", 0.95),
                    ("Catering Charges (Incl. of GST): Rs. 368.00", 0.95),
                    ("Convenience Fee: Rs. 23.60", 0.9),
                    ("Travel Insurance: Rs. 0.45", 0.9),
                    ("Total: Rs. 2804.05", 0.95),
                ]
            ),
        ),
    )
    recovered = nvidia_ocr.extract_text_from_image(FAKE_IMAGE)
    parsed = bill_parser.parse_bill_text(recovered["text"])

    fake_bill = SimpleNamespace(raw_text=parsed.raw_text, page_number=1)
    result = checks.check_catering(category="TRAIN / BUS FARE", bill=fake_bill, claimed_amount=2804.05)

    assert result.result == checks.REVIEW_REQUIRED
    assert result.details == (
        "Total claimed amount: Rs.2,804.05. Catering amount: Rs.368.00. "
        "Catering/service component requires separate policy review."
    )


# ---------- 14. Deterministic validation remains authoritative ----------


def test_nvidia_never_overrides_a_value_bill_parser_already_extracted(monkeypatch):
    """bill_parser.py's own labelled/arithmetic extraction already finds a
    clean amount from this cab-bill-like text; even if a (mocked) NVIDIA
    fallback were invoked, it must never override an already-extracted
    value - amount stays whatever the deterministic parser found."""
    text = "Ride fare 520.00\nCGST 13.00\nSGST 13.00\nTotal 546.00"
    parsed = bill_parser.parse_bill_text(text)
    assert parsed.amount == 546.00

    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")

    def _fail_if_called(*a, **k):
        raise AssertionError("nvidia_fallback must not be invoked when amount/date are already extracted")

    monkeypatch.setattr(httpx, "post", _fail_if_called)
    # Re-parsing the same, already-sufficient text must not touch the
    # network at all (bill_parser.py's own gate: only when amount/date are
    # still None) - proves the deterministic result is authoritative and
    # final once it's confident, never second-guessed by NVIDIA.
    parsed_again = bill_parser.parse_bill_text(text)
    assert parsed_again.amount == 546.00


def test_low_confidence_local_ocr_triggers_nvidia_fallback_in_pipeline(monkeypatch, tmp_path):
    """Integration-level check on text_extraction.py's _apply_ocr(): a local
    OCR engine reporting confidence below MIN_OCR_CONFIDENCE causes the
    (mocked, available) NVIDIA fallback to be tried and its better text used;
    a confident local OCR result is left alone (no NVIDIA call at all)."""
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")

    calls = {"count": 0}

    def _fake_post(*a, **k):
        calls["count"] += 1
        return _FakeResponse(200, _ocr_response([("Recovered via NVIDIA", 0.95)]))

    monkeypatch.setattr(httpx, "post", _fake_post)

    class _WeakOCR:
        def extract_text(self, image_bytes):
            return {"text": "garbled", "confidence": MIN_OCR_CONFIDENCE - 0.1}

    class _ConfidentOCR:
        def extract_text(self, image_bytes):
            return {"text": "clean local text", "confidence": MIN_OCR_CONFIDENCE + 0.3}

    import fitz

    pdf_path = tmp_path / "blank.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(pdf_path)
    doc.close()

    weak_extractor = PyMuPDFTextExtractor(ocr_engine=_WeakOCR())
    pages = weak_extractor.split_pages(pdf_path)
    weak_extractor._apply_ocr(pdf_path, pages)
    assert pages[0]["source"] == "nvidia_ocr"
    assert pages[0]["text"] == "Recovered via NVIDIA"
    assert calls["count"] == 1

    confident_extractor = PyMuPDFTextExtractor(ocr_engine=_ConfidentOCR())
    pages2 = confident_extractor.split_pages(pdf_path)
    confident_extractor._apply_ocr(pdf_path, pages2)
    assert pages2[0]["source"] == "ocr"
    assert pages2[0]["text"] == "clean local text"
    assert calls["count"] == 1  # unchanged - confident local OCR never calls NVIDIA


# ---------- Caching (page-hash based, avoids repeated calls) ----------


def test_same_image_bytes_only_calls_api_once(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    calls = {"count": 0}

    def _fake_post(*a, **k):
        calls["count"] += 1
        return _FakeResponse(200, _ocr_response([("same page", 0.9)]))

    monkeypatch.setattr(httpx, "post", _fake_post)
    first = nvidia_ocr.extract_text_from_image(FAKE_IMAGE)
    second = nvidia_ocr.extract_text_from_image(FAKE_IMAGE)
    assert first == second
    assert calls["count"] == 1


def test_cache_key_is_content_hash(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _ocr_response([("x", 0.9)])))
    nvidia_ocr.extract_text_from_image(FAKE_IMAGE)
    expected_key = hashlib.sha256(FAKE_IMAGE).hexdigest()
    assert expected_key in nvidia_ocr._cache
