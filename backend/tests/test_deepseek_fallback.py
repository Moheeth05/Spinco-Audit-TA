"""Tests for the optional DeepSeek (via DeepInfra) structured-extraction
fallback (deepseek_fallback.py) and its wiring into bill_parser.py's
provider-selection logic alongside the existing nvidia_fallback.py. Both
providers are always mocked here - no automated test makes a real network
call, and no automated test requires a real API key.
"""

import httpx
import pytest

from app.services.extraction import bill_parser, deepseek_fallback, nvidia_fallback

FAKE_BILL_TEXT = "Falcon Cabs\nRide fare 520.00\nCGST 13.00\nSGST 13.00\nTotal 546.00"


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


# ---------- 1. Disabled by default ----------


def test_disabled_when_no_api_key(monkeypatch):
    monkeypatch.delenv("SPINCO_DEEPSEEK_API_KEY", raising=False)
    assert deepseek_fallback.is_available() is False


def test_disabled_returns_none_without_any_http_call(monkeypatch):
    monkeypatch.delenv("SPINCO_DEEPSEEK_API_KEY", raising=False)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("httpx.post must never be called when DeepSeek is disabled")

    monkeypatch.setattr(httpx, "post", _fail_if_called)
    assert deepseek_fallback.extract_structured_fields(FAKE_BILL_TEXT) is None


def test_explicit_enabled_false_overrides_configured_key(monkeypatch):
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "false")
    assert deepseek_fallback.is_available() is False


def test_key_alone_does_not_enable(monkeypatch):
    """Strict opt-in: a configured key with no enable flag stays disabled."""
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.delenv("SPINCO_DEEPSEEK_ENABLED", raising=False)
    assert deepseek_fallback.is_available() is False


# ---------- 2. Enabled, correct endpoint/payload ----------


def test_enabled_when_key_present_and_flag_true(monkeypatch):
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    assert deepseek_fallback.is_available() is True


def test_enabled_calls_documented_deepinfra_endpoint_and_default_model(monkeypatch):
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    monkeypatch.delenv("SPINCO_DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("SPINCO_DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("SPINCO_DEEPSEEK_SERVICE_TIER", raising=False)
    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(200, _chat_response('{"vendor": "Falcon Cabs", "amount": 546.00, "date": null, "invoice_number": null, "subtotal": null, "tax": null, "total": 546.00, "category_evidence": "Ride fare", "description": null, "confidence": "HIGH"}'))

    monkeypatch.setattr(httpx, "post", _fake_post)
    result = deepseek_fallback.extract_structured_fields(FAKE_BILL_TEXT)

    assert result is not None
    assert captured["url"] == "https://api.deepinfra.com/v1/openai/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer ds-test-key"
    assert captured["json"]["model"] == "deepseek-ai/DeepSeek-V3"
    assert "service_tier" not in captured["json"]  # never sent unless explicitly configured


def test_configured_model_and_service_tier_are_used(monkeypatch):
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    monkeypatch.setenv("SPINCO_DEEPSEEK_MODEL", "deepseek-ai/DeepSeek-V4.1-Flash")
    monkeypatch.setenv("SPINCO_DEEPSEEK_SERVICE_TIER", "flex")
    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(200, _chat_response('{"amount": null, "date": null, "vendor": null, "invoice_number": null, "subtotal": null, "tax": null, "total": null, "category_evidence": null, "description": null, "confidence": "LOW"}'))

    monkeypatch.setattr(httpx, "post", _fake_post)
    deepseek_fallback.extract_structured_fields(FAKE_BILL_TEXT)
    assert captured["json"]["model"] == "deepseek-ai/DeepSeek-V4.1-Flash"
    assert captured["json"]["service_tier"] == "flex"


# ---------- 3. Structured field recovery (parsed through the same deterministic parsers) ----------


def test_amount_and_date_recovered_and_reparsed_deterministically(monkeypatch):
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: _FakeResponse(200, _chat_response(
            '{"vendor": "Falcon Cabs", "amount": "546.00", "date": "2026-08-07", "invoice_number": null, '
            '"subtotal": null, "tax": null, "total": null, "category_evidence": "Ride fare", "description": null, "confidence": "HIGH"}'
        )),
    )
    result = deepseek_fallback.extract_structured_fields(FAKE_BILL_TEXT)
    assert result.amount.value == 546.0
    assert result.date.value.isoformat() == "2026-08-07"
    assert result.vendor.value == "Falcon Cabs"
    # A model-sourced field is never trusted at HIGH, regardless of what the model claims.
    assert result.amount.confidence == "MEDIUM"


def test_model_cannot_fabricate_a_field_it_marked_null():
    """_validate_response (shared with nvidia_fallback.py) must leave a null
    field as None, never guess - exercised directly since this logic is
    shared, not duplicated."""
    from app.services.extraction.nvidia_fallback import _validate_response

    result = _validate_response({"amount": None, "date": None, "vendor": None, "confidence": "HIGH"})
    assert result.amount is None
    assert result.date is None
    assert result.vendor is None


# ---------- 4. Failure modes must never break extraction ----------


def test_malformed_response_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, {"unexpected": "shape"}))
    assert deepseek_fallback.extract_structured_fields(FAKE_BILL_TEXT) is None


def test_non_json_content_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response("not json at all")))
    assert deepseek_fallback.extract_structured_fields(FAKE_BILL_TEXT) is None


def test_code_fenced_json_is_still_parsed(monkeypatch):
    """Models sometimes wrap JSON in a ```json fence despite instructions -
    must be stripped defensively, not treated as a failure."""
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    fenced = '```json\n{"amount": "100.00", "date": null, "vendor": null, "invoice_number": null, "subtotal": null, "tax": null, "total": null, "category_evidence": null, "description": null, "confidence": "LOW"}\n```'
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, _chat_response(fenced)))
    result = deepseek_fallback.extract_structured_fields(FAKE_BILL_TEXT)
    assert result is not None
    assert result.amount.value == 100.0


def test_timeout_returns_none_without_raising(monkeypatch):
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")

    def _raise_timeout(*a, **k):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx, "post", _raise_timeout)
    assert deepseek_fallback.extract_structured_fields(FAKE_BILL_TEXT) is None


def test_api_error_5xx_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(500, {}))
    assert deepseek_fallback.extract_structured_fields(FAKE_BILL_TEXT) is None


def test_invalid_key_unauthorized_returns_none(monkeypatch):
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-invalid")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(401, {}))
    assert deepseek_fallback.extract_structured_fields(FAKE_BILL_TEXT) is None


def test_empty_bill_text_returns_none_without_http_call(monkeypatch):
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")

    def _fail_if_called(*a, **k):
        raise AssertionError("httpx.post must never be called for empty text")

    monkeypatch.setattr(httpx, "post", _fail_if_called)
    assert deepseek_fallback.extract_structured_fields("") is None
    assert deepseek_fallback.extract_structured_fields("   ") is None


# ---------- 5. API key never logged/exposed ----------


def test_redact_never_returns_the_real_value():
    secret = "ds-super-secret-value-do-not-leak"
    assert deepseek_fallback._redact(secret) == "<configured>"
    assert secret not in deepseek_fallback._redact(secret)


def test_redact_reports_not_configured_when_unset():
    assert deepseek_fallback._redact(None) == "<not configured>"


# ---------- 6. bill_parser.py provider selection: never both, DeepSeek preferred ----------


def test_bill_parser_prefers_deepseek_when_both_configured(monkeypatch):
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    calls = {"deepseek": 0, "nvidia": 0}

    def _fake_post(url, headers=None, json=None, timeout=None):
        if "deepinfra" in url:
            calls["deepseek"] += 1
        else:
            calls["nvidia"] += 1
        return _FakeResponse(200, _chat_response(
            '{"amount": "546.00", "date": "2026-08-07", "vendor": "Falcon Cabs", "invoice_number": null, '
            '"subtotal": null, "tax": null, "total": null, "category_evidence": "Ride fare", "description": null, "confidence": "HIGH"}'
        ))

    monkeypatch.setattr(httpx, "post", _fake_post)
    # Text with no deterministically-extractable amount/date at all, forcing the fallback gate open.
    parsed = bill_parser.parse_bill_text("Falcon Cabs\nSome ride, no clear total or date printed here.")
    assert calls["deepseek"] == 1
    assert calls["nvidia"] == 0
    assert any("DeepSeek" in w for w in parsed.warnings)


def test_bill_parser_falls_back_to_nvidia_when_deepseek_not_configured(monkeypatch):
    monkeypatch.delenv("SPINCO_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    calls = {"nvidia": 0}

    def _fake_post(url, headers=None, json=None, timeout=None):
        calls["nvidia"] += 1
        return _FakeResponse(200, _chat_response(
            '{"amount": "546.00", "date": "2026-08-07", "vendor": "Falcon Cabs", "invoice_number": null, '
            '"subtotal": null, "tax": null, "total": null, "category_evidence": "Ride fare", "description": null, "confidence": "HIGH"}'
        ))

    monkeypatch.setattr(httpx, "post", _fake_post)
    parsed = bill_parser.parse_bill_text("Falcon Cabs\nSome ride, no clear total or date printed here.")
    assert calls["nvidia"] == 1
    assert any("NVIDIA" in w for w in parsed.warnings)


def test_bill_parser_calls_neither_provider_when_neither_configured(monkeypatch):
    monkeypatch.delenv("SPINCO_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("SPINCO_NVIDIA_API_KEY", raising=False)

    def _fail_if_called(*a, **k):
        raise AssertionError("no provider is configured - httpx.post must never be called")

    monkeypatch.setattr(httpx, "post", _fail_if_called)
    parsed = bill_parser.parse_bill_text("Falcon Cabs\nSome ride, no clear total or date printed here.")
    assert parsed.amount is None
    assert parsed.bill_date is None


def test_bill_parser_never_calls_fallback_when_amount_and_date_already_extracted(monkeypatch):
    """Regression guard (existing behaviour, unaffected by adding DeepSeek):
    a deterministically-complete bill never triggers EITHER provider."""
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")

    def _fail_if_called(*a, **k):
        raise AssertionError("neither fallback should be invoked when amount/date are already extracted")

    monkeypatch.setattr(httpx, "post", _fail_if_called)
    parsed = bill_parser.parse_bill_text(FAKE_BILL_TEXT)
    assert parsed.amount == 546.0


def test_deepseek_fallback_never_overrides_an_already_extracted_amount(monkeypatch):
    """Even when DeepSeek IS invoked (date missing, amount already found),
    it must only fill the missing field, never override the one already
    extracted deterministically."""
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: _FakeResponse(200, _chat_response(
            '{"amount": "9999.00", "date": "2026-08-07", "vendor": null, "invoice_number": null, '
            '"subtotal": null, "tax": null, "total": null, "category_evidence": null, "description": null, "confidence": "HIGH"}'
        )),
    )
    # Deterministically has an amount (546.00) but no parseable date line.
    parsed = bill_parser.parse_bill_text("Falcon Cabs\nTotal: Rs. 546.00")
    assert parsed.amount == 546.0  # untouched by DeepSeek's (different) 9999.00
    assert parsed.bill_date is not None and parsed.bill_date.isoformat() == "2026-08-07"
