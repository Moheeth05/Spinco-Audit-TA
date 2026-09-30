"""Optional, opt-in NVIDIA Nemotron OCR v2 fallback for scanned/photographed
PDF pages that local Tesseract OCR (ocr_engine.py) reads with too little, or
too-low-confidence, text to be useful.

Disabled by default - enabled only when SPINCO_NVIDIA_API_KEY is set (and
SPINCO_NVIDIA_ENABLED, if set at all, is not "false"). If disabled, every
function here is a no-op and the application behaves exactly as it did
before this module existed. Never hardcoded, never committed, never logged,
never exposed to the frontend, never stored in audit logs or error messages
(see is_available()/_redact - the key itself never appears in an exception
message, log line, or cached entry).

Pipeline position: this sits at exactly the same place as ocr_engine.py's
TesseractOCREngine (see text_extraction.py:_apply_ocr), one step upstream of
bill_parser.py's deterministic regex/arithmetic field extraction and the
existing nvidia_fallback.py text-structuring module:

    native PDF text -> Tesseract OCR -> [this module, only if Tesseract's
    result is still empty/low-confidence] -> bill_parser.py's deterministic
    field extraction -> [nvidia_fallback.py, only if amount/date are still
    missing] -> normalization -> validation -> matching -> exception engine

Deliberately kept at the TEXT layer (recovering better raw OCR text, not
asking the model to return structured fields itself): that is the only way a
bill with several distinguishable charge lines (e.g. base fare + CGST + SGST,
or ticket fare + catering + convenience fee + insurance) stays intact as
separable text for bill_parser.py's own arithmetic/catering-keyword logic to
still work on. Collapsing straight to one "amount" field the way
nvidia_fallback.py does for its own (structured-field, chat-completions)
purpose would silently discard exactly the per-line detail the catering
exception depends on. This module never decides PASS/MATCHED/ACCOUNT
VERIFIED/EXCEPTION on its own - the text it recovers goes through the exact
same deterministic bill_parser.py/bill_matcher.py/validation pipeline as
locally-OCR'd text.

Endpoint/request/response shape confirmed against NVIDIA's official NIM for
Image OCR API reference (the self-hosted container's documented /v1/infer
contract) and verified with a live call against the real hosted NVIDIA Build
Platform endpoint during implementation: POST https://ai.api.nvidia.com/v1/
cv/nvidia/nemotron-ocr-v2, Bearer auth, JSON body
{"input": [{"type": "image_url", "url": "data:image/png;base64,..."}],
"merge_levels": [...]}, JSON response shaped as
data[].text_detections[].text_prediction.{text, confidence}.
"""

import base64
import hashlib
import os
from typing import Any

import httpx

_DEFAULT_MODEL = "nvidia/nemotron-ocr-v2"
_DEFAULT_ENDPOINT = f"https://ai.api.nvidia.com/v1/cv/{_DEFAULT_MODEL}"

# In-memory cache keyed by the page image's own sha256 hash, so re-extracting
# the same document within one process's lifetime (e.g. a manual reprocess)
# never re-calls the API for a page whose rendered image bytes haven't
# changed - "avoid repeated calls for the same page" without any database
# redesign (a page's image is fully reproducible from the source PDF, so
# there is nothing here that needs to outlive the process).
_cache: dict[str, dict[str, Any] | None] = {}


def _redact(value: str | None) -> str:
    """Never let a real key value reach a log line or exception message."""
    return "<configured>" if value else "<not configured>"


def is_available() -> bool:
    if not os.environ.get("SPINCO_NVIDIA_API_KEY"):
        return False
    enabled = os.environ.get("SPINCO_NVIDIA_ENABLED")
    if enabled is not None and enabled.strip().lower() in ("false", "0", "no"):
        return False
    return True


def clear_cache() -> None:
    """Test-only helper - production code never needs to call this."""
    _cache.clear()


def extract_text_from_image(image_bytes: bytes, *, timeout: float = 30.0) -> dict[str, Any] | None:
    """Returns {"text": str, "confidence": float} (confidence = mean
    per-detection confidence reported by the model, 0.0-1.0) recovered from a
    page image via Nemotron OCR v2, or None if the fallback isn't
    configured/enabled, the image is empty, the response contained no usable
    text, or the call failed for any reason.

    Never raises - a network/API problem here must never break document
    extraction; the caller (text_extraction.py) simply keeps whatever local
    OCR already produced (including nothing at all)."""
    if not is_available() or not image_bytes:
        return None

    cache_key = hashlib.sha256(image_bytes).hexdigest()
    if cache_key in _cache:
        return _cache[cache_key]

    api_key = os.environ.get("SPINCO_NVIDIA_API_KEY")
    endpoint = os.environ.get("SPINCO_NVIDIA_OCR_ENDPOINT")
    if not endpoint:
        model = os.environ.get("SPINCO_NVIDIA_OCR_MODEL", _DEFAULT_MODEL)
        endpoint = f"https://ai.api.nvidia.com/v1/cv/{model}"

    result: dict[str, Any] | None = None
    try:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        response = httpx.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={
                "input": [{"type": "image_url", "url": f"data:image/png;base64,{encoded}"}],
                "merge_levels": ["paragraph"],
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        detections = payload["data"][0]["text_detections"]

        lines: list[str] = []
        confidences: list[float] = []
        for detection in detections:
            prediction = detection.get("text_prediction") if isinstance(detection, dict) else None
            if not isinstance(prediction, dict):
                continue
            text = prediction.get("text")
            confidence = prediction.get("confidence")
            if isinstance(text, str) and text.strip():
                lines.append(text.strip())
                if isinstance(confidence, (int, float)):
                    confidences.append(float(confidence))

        joined = "\n".join(lines).strip()
        if joined:
            avg_confidence = sum(confidences) / len(confidences) if confidences else 0.5
            result = {"text": joined, "confidence": avg_confidence}
    except Exception:  # noqa: BLE001 - a fallback must degrade silently, never break extraction
        result = None

    _cache[cache_key] = result
    return result
