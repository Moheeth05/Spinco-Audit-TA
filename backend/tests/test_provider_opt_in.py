"""Hosted AI providers (NVIDIA OCR, NVIDIA text fallback, NVIDIA document
classifier, DeepSeek) are STRICTLY opt-in: disabled unless the API key is
configured AND the provider's enable flag is explicitly true.

Everything here is mocked - no test makes a real network call, and every
"secret" below is an obviously fake placeholder. conftest.py additionally
clears all provider environment variables before each test and replaces
httpx.post with a function that fails the test, so a regression that reaches
the network fails loudly instead of calling out.
"""

import logging
from pathlib import Path

import httpx
import pytest

from app.services.extraction import (
    bill_parser,
    deepseek_fallback,
    document_classifier,
    nvidia_fallback,
    nvidia_ocr,
    provider_gate,
)

FAKE_IMAGE = b"\x89PNG\r\n\x1a\nfake-png-bytes-for-testing"
BILL_TEXT = "Falcon Cabs\nRide fare 520.00\nCGST 13.00\nSGST 13.00\nTotal 546.00"
NO_AMOUNT_TEXT = "Falcon Cabs\nSome ride, no clear total or date printed here."
NV_KEY = "nvapi-FAKE-secret-value-for-tests"
DS_KEY = "ds-FAKE-secret-value-for-tests"

CHAT_OK = {
    "choices": [
        {
            "message": {
                "content": '{"vendor": "Falcon Cabs", "amount": "546.00", "date": "2026-08-07", "invoice_number": null, '
                '"subtotal": null, "tax": null, "total": null, "category_evidence": "Ride fare", "description": null, "confidence": "HIGH"}'
            }
        }
    ]
}
CLASSIFY_OK = {
    "choices": [
        {
            "message": {
                "content": '{"document_type": "BILL", "confidence": 0.9, "vendor": "Falcon Cabs", "invoice_number": null, '
                '"transaction_reference": null, "date": "2026-08-07", "total_amount": "546.00", "catering_amount": null, "currency": "INR"}'
            }
        }
    ]
}
OCR_OK = {"data": [{"text_detections": [{"text_prediction": {"text": "Total 546.00", "confidence": 0.95}}]}]}


class _Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._body


class _NetSpy:
    """Counts outbound calls through every httpx entry point the providers
    could plausibly use - httpx.post and the underlying Client/AsyncClient
    send - so a refactor to a different call style can't slip past."""

    def __init__(self, monkeypatch, body=None):
        self.calls = []
        self.body = body

        def _post(url, **kwargs):
            self.calls.append((url, kwargs))
            return _Response(self.body)

        def _send(*args, **kwargs):
            self.calls.append(("httpx.Client.send", kwargs))
            raise AssertionError("unexpected httpx.Client.send call")

        monkeypatch.setattr(httpx, "post", _post)
        monkeypatch.setattr(httpx.Client, "send", _send)
        monkeypatch.setattr(httpx.AsyncClient, "send", _send)


def _direct_calls():
    """One zero-argument callable per provider entry point, each invoked with
    input that would otherwise be sent to the provider."""
    return {
        "nvidia_ocr": lambda: nvidia_ocr.extract_text_from_image(FAKE_IMAGE),
        "nvidia_fallback": lambda: nvidia_fallback.extract_structured_fields(BILL_TEXT),
        "deepseek_fallback": lambda: deepseek_fallback.extract_structured_fields(BILL_TEXT),
        "document_classifier": lambda: document_classifier.classify_page(BILL_TEXT),
    }


def _availability():
    return {
        "nvidia_ocr": nvidia_ocr.is_available,
        "nvidia_fallback": nvidia_fallback.is_available,
        "deepseek_fallback": deepseek_fallback.is_available,
        "document_classifier": document_classifier.is_available,
    }


@pytest.fixture(autouse=True)
def _fresh_ocr_cache():
    nvidia_ocr.clear_cache()
    yield
    nvidia_ocr.clear_cache()


# ---------- 1. Missing flag means disabled ----------


def test_all_providers_disabled_with_nothing_configured():
    assert not any(fn() for fn in _availability().values())


def test_all_providers_disabled_when_flags_missing_even_with_keys(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    # no *_ENABLED variable anywhere
    assert {name: fn() for name, fn in _availability().items()} == {
        "nvidia_ocr": False,
        "nvidia_fallback": False,
        "deepseek_fallback": False,
        "document_classifier": False,
    }


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", "", "  ", "maybe", "tru", "enabled", "2"])
def test_only_an_explicit_truthy_flag_enables(monkeypatch, value):
    """Falsy AND unrecognised values both mean disabled, even with keys set."""
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", value)
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", value)
    assert not any(fn() for fn in _availability().values())


@pytest.mark.parametrize("value", ["true", "TRUE", " True ", "1", "yes", "on"])
def test_explicit_truthy_flag_with_key_enables(monkeypatch, value):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", value)
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", value)
    assert all(fn() for fn in _availability().values())


def test_flag_without_key_is_still_disabled(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    assert not any(fn() for fn in _availability().values())
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", "   ")  # blank key counts as missing
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", "")
    assert not any(fn() for fn in _availability().values())


# ---------- 2. Explicit false wins over a configured key ----------


def test_false_flags_with_keys_present_stay_disabled(monkeypatch):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "false")
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "false")
    assert not any(fn() for fn in _availability().values())


# ---------- 3/4. Disabled providers never touch the network - including direct calls ----------


@pytest.mark.parametrize("flag_state", ["missing", "false"])
@pytest.mark.parametrize("provider", ["nvidia_ocr", "nvidia_fallback", "deepseek_fallback", "document_classifier"])
def test_direct_provider_call_makes_no_http_request_when_disabled(monkeypatch, provider, flag_state):
    spy = _NetSpy(monkeypatch, body=CHAT_OK)
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    if flag_state == "false":
        monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "false")
        monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "false")

    assert _direct_calls()[provider]() is None
    assert spy.calls == []


def test_direct_call_ignores_a_caller_that_skipped_the_availability_check(monkeypatch):
    """The regression this task fixes: nvidia_fallback/deepseek_fallback used
    to check only for a key inside the network function, so a caller that
    didn't consult is_available() (or a disabled flag) still sent the request."""
    spy = _NetSpy(monkeypatch, body=CHAT_OK)
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "false")
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "false")
    assert nvidia_fallback.extract_structured_fields(BILL_TEXT) is None
    assert deepseek_fallback.extract_structured_fields(BILL_TEXT) is None
    assert spy.calls == []


def test_bill_parser_makes_no_provider_call_when_flags_are_missing(monkeypatch):
    spy = _NetSpy(monkeypatch, body=CHAT_OK)
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    parsed = bill_parser.parse_bill_text(NO_AMOUNT_TEXT)
    assert spy.calls == []
    # deterministic behaviour preserved: nothing supplied, flagged for review
    assert parsed.amount is None
    assert parsed.bill_date is None
    assert parsed.status == "REVIEW_REQUIRED"


# ---------- 5. Explicit enablement permits the provider path ----------


def test_enabled_deepseek_reaches_its_endpoint(monkeypatch):
    spy = _NetSpy(monkeypatch, body=CHAT_OK)
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    result = deepseek_fallback.extract_structured_fields(BILL_TEXT)
    assert result is not None and result.amount.value == 546.0
    assert [c[0] for c in spy.calls] == ["https://api.deepinfra.com/v1/openai/chat/completions"]


def test_enabled_nvidia_text_fallback_reaches_its_endpoint(monkeypatch):
    spy = _NetSpy(monkeypatch, body=CHAT_OK)
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    result = nvidia_fallback.extract_structured_fields(BILL_TEXT)
    assert result is not None and result.amount.value == 546.0
    assert [c[0] for c in spy.calls] == ["https://integrate.api.nvidia.com/v1/chat/completions"]


def test_enabled_nvidia_classifier_reaches_its_endpoint(monkeypatch):
    spy = _NetSpy(monkeypatch, body=CLASSIFY_OK)
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    result = document_classifier.classify_page(BILL_TEXT)
    assert result is not None and result.document_type == "BILL"
    assert [c[0] for c in spy.calls] == ["https://integrate.api.nvidia.com/v1/chat/completions"]


def test_enabled_nvidia_ocr_reaches_its_endpoint(monkeypatch):
    spy = _NetSpy(monkeypatch, body=OCR_OK)
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    result = nvidia_ocr.extract_text_from_image(FAKE_IMAGE)
    assert result is not None and "546.00" in result["text"]
    assert [c[0] for c in spy.calls] == ["https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v2"]


# ---------- 5b. Real HTTP stack, real (loopback-only) socket ----------


class _LoopbackServer:
    """A throwaway HTTP server on 127.0.0.1 that records requests and answers
    with a valid chat-completions body. Loopback only - nothing leaves the
    machine - and it lets the test exercise httpx's real transport instead of
    a monkeypatched httpx.post."""

    def __init__(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append((self.path, self.headers.get("Authorization"), self.rfile.read(length)))
                body = json.dumps(CHAT_OK).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # silence stderr
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def loopback_server():
    server = _LoopbackServer()
    yield server
    server.close()


def test_real_transport_disabled_provider_sends_nothing_to_a_listening_server(monkeypatch, loopback_server):
    # undo conftest's httpx.post guard so a regression would REALLY connect
    monkeypatch.setattr(httpx, "post", _real_post)
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_BASE_URL", loopback_server.url)
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_NVIDIA_BASE_URL", loopback_server.url)
    for state in (None, "false"):
        if state:
            monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", state)
            monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", state)
        assert deepseek_fallback.extract_structured_fields(BILL_TEXT) is None
        assert nvidia_fallback.extract_structured_fields(BILL_TEXT) is None
        assert document_classifier.classify_page(BILL_TEXT) is None
    assert loopback_server.requests == []


def test_real_transport_enabled_provider_reaches_the_configured_server(monkeypatch, loopback_server):
    monkeypatch.setattr(httpx, "post", _real_post)
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    monkeypatch.setenv("SPINCO_DEEPSEEK_BASE_URL", loopback_server.url)
    result = deepseek_fallback.extract_structured_fields(BILL_TEXT)
    assert result is not None and result.amount.value == 546.0
    assert [(p, a) for p, a, _ in loopback_server.requests] == [("/v1/chat/completions", f"Bearer {DS_KEY}")]


_REAL_HTTPX_POST = httpx.post  # captured at import, before conftest swaps it per test


def _real_post(*args, **kwargs):
    return _REAL_HTTPX_POST(*args, **kwargs)


# ---------- 6. One provider never silently enables another ----------


def test_enabling_deepseek_does_not_enable_nvidia(monkeypatch):
    spy = _NetSpy(monkeypatch, body=CHAT_OK)
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)  # key present, flag absent
    assert deepseek_fallback.is_available() is True
    assert not any(
        fn() for name, fn in _availability().items() if name != "deepseek_fallback"
    )
    assert nvidia_fallback.extract_structured_fields(BILL_TEXT) is None
    assert nvidia_ocr.extract_text_from_image(FAKE_IMAGE) is None
    assert document_classifier.classify_page(BILL_TEXT) is None
    assert spy.calls == []


def test_enabling_nvidia_does_not_enable_deepseek(monkeypatch):
    spy = _NetSpy(monkeypatch, body=CHAT_OK)
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)  # key present, flag absent
    assert deepseek_fallback.is_available() is False
    assert deepseek_fallback.extract_structured_fields(BILL_TEXT) is None
    assert spy.calls == []


def test_disabled_deepseek_falls_through_to_nvidia_only_if_nvidia_is_explicitly_enabled(monkeypatch):
    spy = _NetSpy(monkeypatch, body=CHAT_OK)
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "false")
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    # NVIDIA configured but NOT enabled: DeepSeek being off must not wake it up.
    bill_parser.parse_bill_text(NO_AMOUNT_TEXT)
    assert spy.calls == []

    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    parsed = bill_parser.parse_bill_text(NO_AMOUNT_TEXT)
    assert [c[0] for c in spy.calls] == ["https://integrate.api.nvidia.com/v1/chat/completions"]
    assert any("NVIDIA" in w for w in parsed.warnings)


# ---------- 7. Invalid configuration / failures fail safely, without leaking secrets ----------


@pytest.mark.parametrize(
    "url",
    ["http://api.example.com/v1", "ftp://api.example.com", "api.example.com/v1", "", "https://", "file:///etc/passwd"],
)
def test_unsafe_or_malformed_endpoint_is_rejected_before_any_request(monkeypatch, url):
    spy = _NetSpy(monkeypatch, body=CHAT_OK)
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")
    monkeypatch.setenv("SPINCO_NVIDIA_BASE_URL", url)
    monkeypatch.setenv("SPINCO_DEEPSEEK_BASE_URL", url)
    monkeypatch.setenv("SPINCO_NVIDIA_OCR_ENDPOINT", url or "x")
    # empty SPINCO_NVIDIA_BASE_URL/ DEEPSEEK_BASE_URL are present-but-blank -> invalid, not "use default"
    assert nvidia_fallback.extract_structured_fields(BILL_TEXT) is None
    assert deepseek_fallback.extract_structured_fields(BILL_TEXT) is None
    assert document_classifier.classify_page(BILL_TEXT) is None
    assert nvidia_ocr.extract_text_from_image(FAKE_IMAGE) is None
    assert spy.calls == []


def test_loopback_http_endpoint_is_allowed_for_local_mock_servers():
    assert provider_gate.is_safe_endpoint("http://127.0.0.1:9999/v1")
    assert provider_gate.is_safe_endpoint("http://localhost:9999/v1")
    assert provider_gate.is_safe_endpoint("https://api.deepinfra.com/v1/openai")
    assert not provider_gate.is_safe_endpoint("http://api.deepinfra.com/v1/openai")
    assert not provider_gate.is_safe_endpoint(None)


@pytest.mark.parametrize("provider", ["nvidia_ocr", "nvidia_fallback", "deepseek_fallback", "document_classifier"])
@pytest.mark.parametrize("failure", ["timeout", "connect", "http_500", "http_401", "garbage_body", "key_in_message"])
def test_provider_failures_return_none_and_never_leak_the_secret(monkeypatch, caplog, provider, failure):
    monkeypatch.setenv("SPINCO_NVIDIA_API_KEY", NV_KEY)
    monkeypatch.setenv("SPINCO_NVIDIA_ENABLED", "true")
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")

    def _post(url, headers=None, **kwargs):
        # The exception text deliberately contains BOTH secrets and the auth
        # header, as a real HTTP library error message could.
        leaky = f"failure talking to {url} with {headers}"
        if failure == "timeout":
            raise httpx.TimeoutException(leaky)
        if failure == "connect":
            raise httpx.ConnectError(leaky)
        if failure == "key_in_message":
            raise RuntimeError(f"{NV_KEY} {DS_KEY} {leaky}")
        if failure == "http_500":
            return _Response({}, 500)
        if failure == "http_401":
            return _Response({}, 401)
        return _Response({"unexpected": "shape"})

    monkeypatch.setattr(httpx, "post", _post)
    with caplog.at_level(logging.DEBUG):
        assert _direct_calls()[provider]() is None

    logged = caplog.text
    assert NV_KEY not in logged and DS_KEY not in logged
    assert "Bearer" not in logged
    for record in caplog.records:
        assert NV_KEY not in record.getMessage() and DS_KEY not in record.getMessage()
        if record.exc_info:  # nothing may attach the leaky exception to a log record
            pytest.fail("provider failure logged with exc_info, which would carry the exception message")


def test_failure_log_names_only_the_exception_type(monkeypatch, caplog):
    monkeypatch.setenv("SPINCO_DEEPSEEK_API_KEY", DS_KEY)
    monkeypatch.setenv("SPINCO_DEEPSEEK_ENABLED", "true")

    def _post(*args, **kwargs):
        raise httpx.ConnectError("secret-bearing detail")

    monkeypatch.setattr(httpx, "post", _post)
    with caplog.at_level(logging.WARNING):
        assert deepseek_fallback.extract_structured_fields(BILL_TEXT) is None
    assert "ConnectError" in caplog.text
    assert "secret-bearing detail" not in caplog.text


# ---------- 8. Configuration template and repo hygiene ----------

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _env_example_assignments() -> dict[str, str]:
    values = {}
    for raw in (BACKEND_DIR / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


def test_env_example_documents_every_provider_disabled_and_holds_no_secret():
    values = _env_example_assignments()
    assert values["SPINCO_NVIDIA_ENABLED"] == "false"
    assert values["SPINCO_DEEPSEEK_ENABLED"] == "false"
    assert values["SPINCO_NVIDIA_API_KEY"] == ""
    assert values["SPINCO_DEEPSEEK_API_KEY"] == ""


REPO_ROOT = BACKEND_DIR.parent


def test_packaged_electron_forces_both_provider_flags_off_after_inheriting_env():
    """electron/main.js builds the backend's environment from `...process.env`;
    the override must come AFTER that spread so a flag exported in a
    developer's shell can never reach the packaged backend."""
    source = (REPO_ROOT / "electron" / "main.js").read_text(encoding="utf-8")
    spread = source.index("...process.env")
    for flag in ("SPINCO_NVIDIA_ENABLED", "SPINCO_DEEPSEEK_ENABLED"):
        marker = f'env.{flag} = "false";'
        assert marker in source, f"main.js must force {flag} off"
        assert source.index(marker) > spread
        assert f'{flag}: "true"' not in source


def test_backend_never_loads_dotenv_files_at_runtime():
    offenders = []
    for path in (BACKEND_DIR / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "load_dotenv" in text or "env_file" in text or "import dotenv" in text:
            offenders.append(str(path.relative_to(BACKEND_DIR)))
    assert offenders == []


def test_packaging_config_cannot_include_env_files():
    import json

    build = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))["build"]
    assert build["files"] and all(not f.lstrip("!").startswith(("backend", ".env")) for f in build["files"] if not f.startswith("!"))
    sources = [r["from"] for r in build["extraResources"]]
    assert sources == [
        "frontend/dist",
        "backend/dist_backend/spinco-backend",
        "resources/tesseract",
        "electron/generated/build-info.json",
    ]


def test_env_files_are_gitignored():
    ignore = (BACKEND_DIR.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in [line.strip() for line in ignore]
    assert ".env.local" in [line.strip() for line in ignore]
