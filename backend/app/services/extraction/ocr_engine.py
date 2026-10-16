"""Tesseract-backed OCR fallback for pages with no usable native PDF text.

Only invoked when a page needed for extraction has too little selectable
text (see text_extraction.py) - never used for pages outside that need,
so photographed supporting bills (out of scope until Phase 4) are left
alone rather than OCR'd unnecessarily.
"""

import logging
import os
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from app.services.interfaces import OCREngine

logger = logging.getLogger(__name__)

_configured = False


def _configure_tesseract() -> None:
    """Points pytesseract at a Tesseract binary without hardcoding a
    developer-specific path (e.g. C:\\Program Files\\Tesseract-OCR).

    Resolution order:
    1. SPINCO_TESSERACT_CMD - an explicit override, full path to tesseract.exe.
    2. SPINCO_TESSERACT_DIR - the packaged desktop app sets this to its bundled
       resources/tesseract directory (see electron/scripts/collect-tesseract.js
       and electron/main.js); TESSDATA_PREFIX is set alongside it so the
       bundled eng/osd traineddata is found instead of a system install.
    3. Neither set (plain `uvicorn app.main:app` dev workflow) - pytesseract's
       own default, i.e. whatever `tesseract` resolves to on PATH, unchanged
       from before this function existed.
    Runs once per process; safe to call multiple times.
    """
    global _configured
    if _configured:
        return

    import pytesseract

    # Marked done only once pytesseract actually imported: if the import
    # fails, a later retry (see TesseractOCREngine.is_available) must still
    # run the configuration below, not skip it and then probe the wrong
    # (default PATH) Tesseract forever.
    _configured = True

    explicit_cmd = os.environ.get("SPINCO_TESSERACT_CMD")
    if explicit_cmd:
        pytesseract.pytesseract.tesseract_cmd = explicit_cmd
        return

    bundled_dir = os.environ.get("SPINCO_TESSERACT_DIR")
    if bundled_dir:
        bundled_exe = Path(bundled_dir) / "tesseract.exe"
        if bundled_exe.exists():
            pytesseract.pytesseract.tesseract_cmd = str(bundled_exe)
            tessdata_dir = Path(bundled_dir) / "tessdata"
            if tessdata_dir.exists():
                os.environ["TESSDATA_PREFIX"] = str(tessdata_dir)


# Tesseract's own OSD (orientation & script detection) confidence is an
# unbounded score, not a 0-1 probability - empirically, a page this app's
# own real corpus actually needed orientation for was never found (every
# real document on hand is already upright), so this threshold is a
# conservative, documented judgment call rather than one calibrated against
# a confirmed real example: low enough to catch a genuinely rotated page,
# high enough that a borderline/noisy OSD read (observed on legitimate
# upright pages at confidence as low as 0.65) is never acted on. Revisit
# with a real rotated example if one is ever found.
_MIN_OSD_CONFIDENCE = 1.0

# Running OSD (a second, full Tesseract invocation) on every OCR'd page measured
# as a real 1.3-2.3x runtime cost per page (see experiments/node-extraction/
# benchmarks/handwritten-ocr-20261014/REPORT.md) - acceptable for a single
# diagnostic page, but compounds badly across a real multi-page, multi-bill
# batch upload (confirmed: a 2-file real batch left the synchronous upload
# request running long enough that it never completed before the app was
# closed). Only pay that cost on a page whose first-pass result already looks
# weak enough that a wrong orientation is a plausible explanation - a clean
# upright page (the common case, confirmed: every real document page tested
# so far reports rotate=0) never triggers OSD at all.
_WEAK_RESULT_CONFIDENCE = 0.6


def _correct_orientation(image: Any) -> tuple[Any, int]:
    """Uses Tesseract's own OSD model (osd.traineddata - already bundled
    with this app for OCR, but never previously invoked) to detect a
    confident 90/180/270-degree rotation and correct it before the real OCR
    pass. Returns (possibly-rotated image, degrees corrected - 0 if none).

    Never guesses: OSD routinely fails outright on a sparse/low-content page
    (confirmed real behaviour - raises TesseractError, caught and treated as
    "no correction" here) or reports a genuinely-zero rotation on an
    already-upright page; both leave the image untouched."""
    import pytesseract

    try:
        osd = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT)
    except Exception:  # noqa: BLE001 - OSD is a best-effort hint, never fatal to OCR itself
        return image, 0

    angle = osd.get("rotate") or 0
    confidence = osd.get("orientation_conf") or 0
    if not angle or confidence < _MIN_OSD_CONFIDENCE:
        return image, 0

    # Tesseract's own "rotate" value is how many degrees to rotate the
    # image CLOCKWISE to correct it; PIL's Image.rotate() is
    # counterclockwise, hence the negation.
    return image.rotate(-angle, expand=True), int(angle)


def preprocess_for_difficult_ocr(image_bytes: bytes) -> bytes:
    """A measured, targeted preprocessing pass - deliberately used ONLY by
    the existing narrow retry path (text_extraction.py's
    _retry_ocr_at_higher_dpi, reached only when the first OCR pass returned
    no usable text at all), never applied to a normal page's first pass, per
    this task's explicit "avoid expensive multi-pass OCR for every normal
    printed document" requirement.

    Grayscale -> contrast boost -> a fixed global threshold (binarization).
    Standard, well-understood techniques for improving legibility of a
    faint/low-contrast photographed receipt; no new dependency (PIL only,
    already used throughout this module) and no adaptive/per-image-tuned
    algorithm that would make behaviour hard to predict - this only adjusts
    how the SAME pixels are presented to Tesseract, never invents content.
    """
    from PIL import Image, ImageEnhance, ImageOps

    image = Image.open(BytesIO(image_bytes))
    gray = ImageOps.grayscale(image)
    contrasted = ImageEnhance.Contrast(gray).enhance(1.6)
    thresholded = contrasted.point(lambda p: 0 if p < 140 else 255)
    out = BytesIO()
    thresholded.save(out, format="PNG")
    return out.getvalue()


# A failed Tesseract availability check is a TEMPORARY condition (e.g. a
# process spawn failing under load), not a permanent verdict: it used to be
# cached as False for the life of the process, which silently disabled OCR for
# every later page - and in the long-lived packaged backend, until restart.
# Only a success is cached. A failed check is retried a couple of times
# straight away (a transient glitch usually clears within a moment), and if it
# still fails the next check is held off for a cooldown so a persistently
# missing Tesseract costs at most one bounded round of checks per cooldown, no
# matter how many pages or threads ask.
_AVAILABILITY_PROBE_ATTEMPTS = 3
_AVAILABILITY_PROBE_RETRY_DELAY_SECONDS = 0.25
_AVAILABILITY_RETRY_COOLDOWN_SECONDS = 5.0

# Indirection so tests can drive time without real waiting.
_monotonic = time.monotonic
_sleep = time.sleep


class TesseractOCREngine(OCREngine):
    def __init__(self):
        # True once Tesseract has been confirmed usable, and then never
        # re-checked. Never set to False: failure is not cached (see above).
        self._available: bool | None = None
        self._probe_lock = threading.Lock()
        self._next_probe_at: float | None = None

    def is_available(self) -> bool:
        if self._available:
            return True

        # One check at a time: concurrent OCR worker threads that arrive while
        # a check is in flight wait for its result instead of each starting
        # their own, and a failed round's cooldown applies to all of them.
        with self._probe_lock:
            if self._available:
                return True
            if self._next_probe_at is not None and _monotonic() < self._next_probe_at:
                return False

            last_error: Exception | None = None
            for attempt in range(_AVAILABILITY_PROBE_ATTEMPTS):
                try:
                    _configure_tesseract()
                    import pytesseract

                    pytesseract.get_tesseract_version()
                except Exception as exc:  # noqa: BLE001 - any failure to run Tesseract means "not available right now"
                    last_error = exc
                    if attempt + 1 < _AVAILABILITY_PROBE_ATTEMPTS:
                        _sleep(_AVAILABILITY_PROBE_RETRY_DELAY_SECONDS)
                    continue

                recovered = self._next_probe_at is not None
                self._available = True
                self._next_probe_at = None
                if recovered:
                    logger.info("Tesseract is available again; local OCR has resumed.")
                return True

            self._next_probe_at = _monotonic() + _AVAILABILITY_RETRY_COOLDOWN_SECONDS
            logger.warning(
                "Tesseract availability check failed (%s: %s); local OCR is unavailable and will be re-checked in %.0fs.",
                type(last_error).__name__,
                last_error,
                _AVAILABILITY_RETRY_COOLDOWN_SECONDS,
            )
            return False

    def _ocr_pass(self, image: Any) -> dict[str, Any]:
        """One Tesseract image_to_data call, parsed into this engine's
        {text, confidence, words} shape. No orientation handling here -
        that's the caller's decision (extract_text), so this can be reused
        for both the first pass and an optional orientation-corrected retry
        without paying for OSD twice."""
        import pytesseract

        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)

        words: list[dict[str, Any]] = []
        confidences: list[int] = []
        for i, text in enumerate(data["text"]):
            if not text.strip():
                continue
            conf = data["conf"][i]
            words.append(
                {
                    "text": text,
                    "left": data["left"][i],
                    "top": data["top"][i],
                    "width": data["width"][i],
                    "height": data["height"][i],
                    "conf": conf,
                    "block_num": data["block_num"][i],
                    "par_num": data["par_num"][i],
                    "line_num": data["line_num"][i],
                }
            )
            if conf != "-1":
                confidences.append(int(conf))
        avg_confidence = (sum(confidences) / len(confidences) / 100) if confidences else 0.0

        lines: list[list[str]] = []
        current_key = None
        for w in words:
            key = (w["block_num"], w["par_num"], w["line_num"])
            if key != current_key:
                lines.append([])
                current_key = key
            lines[-1].append(w["text"])
        text_out = "\n".join(" ".join(line_words) for line_words in lines)

        return {"text": text_out, "confidence": avg_confidence, "words": words}

    def extract_text(self, image_bytes: bytes) -> dict[str, Any]:
        """Returns {"text": str, "confidence": float, "words": list[dict],
        "rotation_applied": int}.

        `text` preserves Tesseract's own logical line structure (grouped by
        its (block_num, par_num, line_num) layout analysis, the same fields
        `image_to_data()` already computes) instead of flattening every word
        on the page into one space-joined string. A thermal-receipt label
        and its value (e.g. "Amount(Rs)" and "03908.04") routinely end up as
        separate words on the SAME Tesseract line - collapsing all lines
        together (the previous behaviour) destroyed that structure and
        broke every downstream line-based label/value search in
        bill_parser.py, even when both words were individually read
        correctly. `words` additionally exposes each word's own bounding box
        (left/top/width/height) and layout indices, for callers that need
        true spatial (not just line-order) reasoning.

        Orientation correction (_correct_orientation) only runs when this
        first pass already looks weak (empty text or confidence below
        _WEAK_RESULT_CONFIDENCE) - see that constant's comment for why a
        clean, already-upright page (the common case) must never pay for a
        second Tesseract invocation. `rotation_applied` is 0 unless a
        confident correction was made AND it was adopted.
        """
        if not self.is_available():
            return {"text": "", "confidence": 0.0, "words": [], "rotation_applied": 0}

        from PIL import Image

        image = Image.open(BytesIO(image_bytes))
        result = self._ocr_pass(image)

        if result["text"].strip() and result["confidence"] >= _WEAK_RESULT_CONFIDENCE:
            return {**result, "rotation_applied": 0}

        corrected, rotation_applied = _correct_orientation(image)
        if not rotation_applied:
            return {**result, "rotation_applied": 0}

        rotated_result = self._ocr_pass(corrected)
        # Only adopt the rotated result if it's actually better - OSD can be
        # confident about an orientation that doesn't help this particular
        # page's legibility (e.g. a genuinely poor-quality scan), and this
        # must never regress a result the first pass already produced.
        rotated_is_better = (
            bool(rotated_result["text"].strip())
            and len(rotated_result["text"]) >= len(result["text"])
            and rotated_result["confidence"] >= result["confidence"]
        )
        if rotated_is_better:
            return {**rotated_result, "rotation_applied": rotation_applied}
        return {**result, "rotation_applied": 0}
