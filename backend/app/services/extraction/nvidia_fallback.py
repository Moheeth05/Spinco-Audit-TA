"""Optional, opt-in structured-extraction fallback for bill pages the local
deterministic pipeline (native PDF text -> OCR -> bill_parser.py's labelled/
arithmetic amount and date rules) couldn't confidently extract from.

Disabled by default. Enabled only by setting the SPINCO_NVIDIA_API_KEY
environment variable - never hardcoded, never committed, never logged, never
exposed to the frontend, never stored in audit logs or error messages (see
is_available()/_redact below). If it isn't set, every function in this
module is a no-op and the application behaves exactly as it does today.

Pipeline position (per the post-UAT correction spec's Part 8):

    native PDF text -> OCR -> [optional NVIDIA structured extraction] ->
    normalization -> deterministic validation -> matching -> exception engine

This module only ever SUPPLEMENTS bill_parser.py's own extraction - it fills
in fields bill_parser.py left as None, at LOW confidence, flagged with its
own source ("nvidia_fallback") in ParsedBill.warnings so the evidence trail
stays honest about where each field actually came from. It never overrides a
field bill_parser.py already extracted, and it never decides PASS/MATCHED/
ACCOUNT VERIFIED on its own - every value it returns still goes through the
exact same deterministic bill_matcher.py/validation/checks.py pipeline as
everything else. A field the model can't determine must come back null, not
guessed - see _validate_response().
"""

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

from app.services.extraction.dates import parse_date
from app.services.extraction.money import parse_amount

# NVIDIA's NIM catalog exposes an OpenAI-compatible chat completions API -
# this is the standard public endpoint shape, not a project-specific one.
# Override via env var if the configured model/deployment differs.
_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
_DEFAULT_MODEL = "meta/llama-3.1-70b-instruct"

_FIELDS = ("vendor", "invoice_number", "date", "amount", "subtotal", "tax", "total", "category_evidence", "description")

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
    return bool(os.environ.get("SPINCO_NVIDIA_API_KEY"))


@dataclass
class FallbackField:
    value: Any
    confidence: str  # HIGH | MEDIUM | LOW


@dataclass
class FallbackResult:
    vendor: FallbackField | None = None
    invoice_number: FallbackField | None = None
    date: FallbackField | None = None
    amount: FallbackField | None = None
    category_evidence: FallbackField | None = None
    description: FallbackField | None = None
    raw_model_confidence: str | None = None


def _validate_response(data: dict) -> FallbackResult:
    """Never trusts the model's own values without re-parsing them through
    the same deterministic parsers (parse_amount/parse_date) the rest of the
    pipeline uses - a model that writes "546" as text still has to survive
    the same numeric/date parsing everything else does, and a field it
    couldn't determine (None/null/empty) never becomes a fabricated value."""
    confidence = data.get("confidence") if isinstance(data.get("confidence"), str) else "LOW"
    confidence = confidence.upper() if confidence.upper() in ("HIGH", "MEDIUM", "LOW") else "LOW"
    # A model-sourced field is never trusted at HIGH confidence, regardless
    # of what the model itself claims - only bill_parser.py's own labelled/
    # arithmetic-validated extraction earns HIGH (see module docstring).
    field_confidence = "MEDIUM" if confidence in ("HIGH", "MEDIUM") else "LOW"

    result = FallbackResult(raw_model_confidence=confidence)

    vendor = data.get("vendor")
    if isinstance(vendor, str) and vendor.strip():
        result.vendor = FallbackField(vendor.strip(), field_confidence)

    invoice_number = data.get("invoice_number")
    if isinstance(invoice_number, str) and invoice_number.strip():
        result.invoice_number = FallbackField(invoice_number.strip(), field_confidence)

    date_value = data.get("date")
    if isinstance(date_value, str) and date_value.strip():
        parsed = parse_date(date_value.strip())
        if parsed is not None:
            result.date = FallbackField(parsed, field_confidence)

    amount_value = data.get("amount") if data.get("amount") is not None else data.get("total")
    if amount_value is not None:
        parsed = parse_amount(str(amount_value))
        if parsed is not None and parsed > 0:
            result.amount = FallbackField(parsed, field_confidence)

    category_evidence = data.get("category_evidence")
    if isinstance(category_evidence, str) and category_evidence.strip():
        result.category_evidence = FallbackField(category_evidence.strip(), field_confidence)

    description = data.get("description")
    if isinstance(description, str) and description.strip():
        result.description = FallbackField(description.strip(), field_confidence)

    return result


def extract_structured_fields(bill_text: str, *, timeout: float = 20.0) -> FallbackResult | None:
    """Returns a FallbackResult with whatever fields the model could
    determine (each field independently null-or-populated - see
    _validate_response), or None if the fallback isn't configured or the
    call failed for any reason. Never raises - a network/API problem here
    must never break document extraction, it just means no supplementary
    fields are offered and bill_parser.py's own result stands as-is."""
    api_key = os.environ.get("SPINCO_NVIDIA_API_KEY")
    if not api_key:
        return None
    if not bill_text or not bill_text.strip():
        return None

    base_url = os.environ.get("SPINCO_NVIDIA_BASE_URL", _DEFAULT_BASE_URL)
    model = os.environ.get("SPINCO_NVIDIA_MODEL", _DEFAULT_MODEL)

    try:
        response = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": _PROMPT.format(text=bill_text[:4000])}],
                "temperature": 0.0,
                "max_tokens": 500,
            },
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
    except Exception:  # noqa: BLE001 - a fallback must degrade silently, never break extraction
        return None
