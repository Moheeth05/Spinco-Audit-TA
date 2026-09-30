"""Optional, opt-in supporting-document-type classification for a bill-region
page bill_parser.py's own deterministic rules found NO recognizable evidence
on at all (no amount, no GSTIN, no bill number, no category-guess keyword
match - see bill_parser.parsed_has_evidence()). A last-resort fallback, not a
replacement: bill_parser.py's deterministic extraction always runs first and
is always preferred; this module only gets a chance at a page that would
otherwise be silently dropped with zero evidence.

Adapted from the same proven pattern this project's own nvidia_fallback.py
already uses (strict single-JSON-object prompt, temperature 0, defensive
parsing that re-validates every field through the same parse_amount/
parse_date the rest of the pipeline uses, never trusts a raw model value
directly, never raises). Uses the SAME already-configured NVIDIA credentials
(SPINCO_NVIDIA_API_KEY/SPINCO_NVIDIA_ENABLED) - no new provider, no new
config surface. Disabled by default; every function here is a no-op unless
NVIDIA is configured, exactly like nvidia_fallback.py.

This is supporting-EVIDENCE classification only. It never decides PASS/
MATCHED/ACCOUNT VERIFIED/EXCEPTION on its own - a bill created from its
output always starts REVIEW_REQUIRED (see bill_pipeline.py) and goes through
the exact same deterministic bill_matcher.py/validation/checks.py pipeline
as every other bill. An invalid or out-of-schema model response is treated
as "no evidence found" (returns None), never as a confident classification.
"""

import json
import os
from dataclasses import dataclass
from datetime import date as date_cls
from typing import Any

import httpx

from app.services.extraction.money import parse_amount

_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
_DEFAULT_MODEL = "meta/llama-3.1-70b-instruct"

DOCUMENT_TYPES = ("BILL", "RECEIPT", "TICKET", "TRANSACTION_SCREENSHOT", "INVOICE", "OTHER", "NOT_APPLICABLE")

_PROMPT = """You are classifying a single supporting-evidence page from a travel expense audit system. The page's automated text extraction found no bill-like evidence (no clear amount, GSTIN, or bill number), so a human-level read is needed. Return ONLY a JSON object with exactly these keys: document_type, confidence, vendor, invoice_number, transaction_reference, date, total_amount, catering_amount, currency.

Rules:
- "document_type" MUST be exactly one of: "BILL", "RECEIPT", "TICKET", "TRANSACTION_SCREENSHOT", "INVOICE", "OTHER", "NOT_APPLICABLE". Use "NOT_APPLICABLE" if this page is not useful supporting evidence at all (e.g. blank, a signature page, unrelated content, or part of the Expense Report form itself).
- "confidence" is a number from 0.0 to 1.0 - your own honest assessment of how certain you are.
- "total_amount" is the final payable total, only if clearly and unambiguously present.
- "catering_amount" is a separately itemized catering/food/service charge, only if clearly present as its own line item. Do not compute or infer one.
- "transaction_reference" is a payment/transaction/UTR/reference number, if present (common on payment/transaction screenshots).
- "date" must be in YYYY-MM-DD format, or null.
- Any field you cannot determine with confidence MUST be null. Never guess or fabricate a value.
- Return ONLY the JSON object, no other text, no markdown code fence.

Page text:
---
{text}
---"""


@dataclass
class ClassificationField:
    value: Any
    confidence: str  # MEDIUM | LOW - see _validate_response, mirrors nvidia_fallback.py's own field confidence rule


@dataclass
class ClassificationResult:
    document_type: str
    type_confidence: float  # 0.0-1.0, clamped/validated model estimate
    vendor: ClassificationField | None = None
    invoice_number: ClassificationField | None = None
    transaction_reference: ClassificationField | None = None
    date: ClassificationField | None = None
    total_amount: ClassificationField | None = None
    catering_amount: ClassificationField | None = None


def is_available() -> bool:
    if not os.environ.get("SPINCO_NVIDIA_API_KEY"):
        return False
    enabled = os.environ.get("SPINCO_NVIDIA_ENABLED")
    if enabled is not None and enabled.strip().lower() in ("false", "0", "no"):
        return False
    return True


def _validate_response(data: dict) -> ClassificationResult | None:
    """Returns None (== "no usable classification") for anything that
    doesn't cleanly fit the schema - an out-of-enum document_type, a
    non-numeric confidence, or a completely empty result are all treated as
    "the model could not confidently classify this page", never coerced into
    a guess. Every field is independently re-validated through the same
    deterministic parsers the rest of the pipeline uses, exactly like
    nvidia_fallback.py's own _validate_response."""
    doc_type = data.get("document_type")
    if not isinstance(doc_type, str) or doc_type.strip().upper() not in DOCUMENT_TYPES:
        return None
    doc_type = doc_type.strip().upper()

    confidence_raw = data.get("confidence")
    try:
        type_confidence = float(confidence_raw)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= type_confidence <= 1.0):
        return None

    if doc_type == "NOT_APPLICABLE":
        return ClassificationResult(document_type=doc_type, type_confidence=type_confidence)

    # A model-sourced field is never trusted at HIGH confidence, same rule as
    # nvidia_fallback.py - only bill_parser.py's own labelled/arithmetic-
    # validated extraction earns HIGH.
    field_confidence = "MEDIUM" if type_confidence >= 0.5 else "LOW"
    result = ClassificationResult(document_type=doc_type, type_confidence=type_confidence)

    vendor = data.get("vendor")
    if isinstance(vendor, str) and vendor.strip():
        result.vendor = ClassificationField(vendor.strip(), field_confidence)

    invoice_number = data.get("invoice_number")
    if isinstance(invoice_number, str) and invoice_number.strip():
        result.invoice_number = ClassificationField(invoice_number.strip(), field_confidence)

    transaction_reference = data.get("transaction_reference")
    if isinstance(transaction_reference, str) and transaction_reference.strip():
        result.transaction_reference = ClassificationField(transaction_reference.strip(), field_confidence)

    date_value = data.get("date")
    if isinstance(date_value, str) and date_value.strip():
        # The prompt requires ISO YYYY-MM-DD specifically (unlike the rest of
        # this codebase's OCR-text parse_date, which targets DD/MM/YYYY) -
        # parsed directly and strictly here rather than guessed at.
        try:
            parsed_date = date_cls.fromisoformat(date_value.strip())
        except ValueError:
            parsed_date = None
        if parsed_date is not None:
            result.date = ClassificationField(parsed_date, field_confidence)

    total_amount = data.get("total_amount")
    if total_amount is not None:
        parsed_total = parse_amount(str(total_amount))
        if parsed_total is not None and parsed_total > 0:
            result.total_amount = ClassificationField(parsed_total, field_confidence)

    catering_amount = data.get("catering_amount")
    if catering_amount is not None:
        parsed_catering = parse_amount(str(catering_amount))
        if parsed_catering is not None and parsed_catering > 0:
            result.catering_amount = ClassificationField(parsed_catering, field_confidence)

    return result


def classify_page(page_text: str, *, timeout: float = 20.0) -> ClassificationResult | None:
    """Returns a ClassificationResult, or None if the classifier isn't
    configured/enabled, the text is empty, the model call failed, or the
    response didn't validate. Never raises - a network/API problem here must
    never break document extraction; the caller (bill_pipeline.py) simply
    treats the page as having no recognizable evidence, exactly as it did
    before this module existed."""
    if not is_available():
        return None
    if not page_text or not page_text.strip():
        return None

    api_key = os.environ.get("SPINCO_NVIDIA_API_KEY")
    base_url = os.environ.get("SPINCO_NVIDIA_BASE_URL", _DEFAULT_BASE_URL)
    model = os.environ.get("SPINCO_NVIDIA_MODEL", _DEFAULT_MODEL)

    try:
        response = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": _PROMPT.format(text=page_text[:4000])}],
                "temperature": 0.0,
                "max_tokens": 500,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        # Models sometimes wrap JSON in a code fence despite instructions -
        # strip that defensively rather than failing the whole call (same
        # defensive parsing as nvidia_fallback.py).
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.lower().startswith("json"):
                content = content[4:]
        # Defensive against any remaining surrounding prose - recover the
        # first {...} block rather than requiring an exact-JSON-only response.
        if not content.startswith("{"):
            start = content.find("{")
            end = content.rfind("}")
            if start == -1 or end == -1 or end < start:
                return None
            content = content[start : end + 1]
        data = json.loads(content)
        if not isinstance(data, dict):
            return None
        return _validate_response(data)
    except Exception:  # noqa: BLE001 - a fallback must degrade silently, never break extraction
        return None
