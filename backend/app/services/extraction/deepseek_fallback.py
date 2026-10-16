"""Optional, opt-in structured-extraction fallback for bill pages the local
deterministic pipeline (native PDF text -> OCR -> bill_parser.py's labelled/
arithmetic amount and date rules) couldn't confidently extract from - the
same role nvidia_fallback.py already serves, offered through DeepSeek
(hosted on DeepInfra's OpenAI-compatible API) as an alternative provider.

Disabled by default. Enabled only when SPINCO_DEEPSEEK_API_KEY is set AND
SPINCO_DEEPSEEK_ENABLED is explicitly true (a missing or unrecognised flag
means disabled - see provider_gate.py). extract_structured_fields re-checks
the gate itself, so a direct call is safe too - never hardcoded, never committed, never logged,
never exposed to the frontend, never stored in audit logs or error messages
(see is_available()/_redact below). If it isn't set, every function in this
module is a no-op and the application behaves exactly as it does today.

Pipeline position: identical to nvidia_fallback.py (see its module
docstring) - this only ever SUPPLEMENTS bill_parser.py's own extraction,
fills in fields left as None, never overrides a field already extracted
deterministically, and never decides PASS/MATCHED/ACCOUNT VERIFIED on its
own. bill_parser.py tries at most ONE of the two text-interpretation
providers per bill (never both for the same document - see its own call
site), preferring DeepSeek when both are configured, since this project's
intended provider split is "NVIDIA for OCR, DeepSeek for structured-field
interpretation" (not a hard requirement of either module individually -
each works standalone and is independently toggleable).

Reuses nvidia_fallback.py's FallbackField/FallbackResult/_validate_response
instead of duplicating them - both providers return the same OpenAI-style
chat-completions JSON shape for this prompt, and bill_parser.py must be able
to treat either provider's result identically.

Endpoint shape confirmed against DeepInfra's own documented OpenAI-compatible
inference API (https://deepinfra.com/docs) - POST https://api.deepinfra.com/
v1/openai/chat/completions, Bearer auth, standard OpenAI chat-completions
request/response body. DeepInfra also accepts an optional "service_tier"
field (e.g. "flex" - lower cost, routed to spare capacity) - passed through
only when SPINCO_DEEPSEEK_SERVICE_TIER is explicitly set, never invented.
"""

import json
import logging
import os

import httpx

from app.services.extraction import provider_gate
from app.services.extraction.nvidia_fallback import FallbackResult, _validate_response

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.deepinfra.com/v1/openai"
_DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3"

_PROMPT = """You are extracting structured fields from a single supporting bill/receipt's OCR text for an expense audit system. Return ONLY a JSON object with exactly these keys: vendor, invoice_number, date, amount, subtotal, tax, total, category_evidence, description, confidence.

Rules:
- "amount" is the final payable total on the bill (prefer an explicit "total"/"grand total"/"amount payable" line; otherwise the largest clearly-labelled figure).
- "date" must be in YYYY-MM-DD format, or null if not confidently present.
- "category_evidence" is a short phrase from the bill's own text that indicates what kind of expense this is (e.g. "Ride fare", "Hotel room charge", "Train ticket") - never invent one.
- "confidence" is one of "HIGH", "MEDIUM", "LOW" - your own honest assessment of how reliable this extraction is.
- Any field you cannot determine from the text MUST be null. Never guess or fabricate a value.
- Return ONLY the JSON object, no other text.

Bill text:
---
{text}
---"""


def _redact(value: str | None) -> str:
    """Never let a real key value reach a log line or exception message."""
    return "<configured>" if value else "<not configured>"


def is_available() -> bool:
    # Independent of every NVIDIA setting: enabling or disabling one provider
    # never changes the other.
    return provider_gate.provider_enabled("SPINCO_DEEPSEEK_API_KEY", "SPINCO_DEEPSEEK_ENABLED")


def extract_structured_fields(bill_text: str, *, timeout: float = 20.0) -> FallbackResult | None:
    """Returns a FallbackResult with whatever fields DeepSeek could
    determine (see nvidia_fallback.FallbackResult/_validate_response - the
    same validation discipline applies: nothing here is trusted without
    being re-parsed through the same deterministic parsers everything else
    uses), or None if the fallback isn't configured or the call failed for
    any reason. Never raises - a network/API problem here must never break
    document extraction."""
    # Enforced here, not just by the caller: a direct call must never reach
    # the network while the provider is disabled.
    if not is_available():
        return None
    api_key = os.environ.get("SPINCO_DEEPSEEK_API_KEY")
    if not bill_text or not bill_text.strip():
        return None

    base_url = os.environ.get("SPINCO_DEEPSEEK_BASE_URL", _DEFAULT_BASE_URL)
    model = os.environ.get("SPINCO_DEEPSEEK_MODEL", _DEFAULT_MODEL)
    if not provider_gate.is_safe_endpoint(base_url):
        logger.warning("DeepSeek fallback skipped: configured base URL is not a valid https URL.")
        return None
    service_tier = os.environ.get("SPINCO_DEEPSEEK_SERVICE_TIER")

    body = {
        "model": model,
        "messages": [{"role": "user", "content": _PROMPT.format(text=bill_text[:4000])}],
        "temperature": 0.0,
        "max_tokens": 500,
    }
    if service_tier:
        body["service_tier"] = service_tier

    try:
        response = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        # Models sometimes wrap JSON in a code fence despite instructions -
        # strip that defensively rather than failing the whole call.
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.lower().startswith("json"):
                content = content[4:]
        data = json.loads(content)
        if not isinstance(data, dict):
            return None
        return _validate_response(data)
    except Exception as exc:  # noqa: BLE001 - a fallback must degrade silently, never break extraction
        logger.warning("DeepSeek fallback call failed (%s); using local extraction only.", type(exc).__name__)
        return None
