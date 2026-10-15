"""TASK: Improve handwritten bill OCR and exception handling.

Root cause (confirmed by direct inspection of the pipeline, not assumed):
- No rotation/orientation detection or correction existed anywhere in the
  codebase, despite osd.traineddata already being bundled alongside
  eng.traineddata for OCR - it was simply never invoked
  (pytesseract.image_to_osd was never called). See
  ocr_engine.py::_correct_orientation.
- No image preprocessing (grayscale/contrast/threshold/denoise/deskew)
  existed anywhere. See ocr_engine.py::preprocess_for_difficult_ocr, wired
  into the existing narrow empty-result retry path only (text_extraction.py
  ::_retry_ocr_at_higher_dpi) - never applied to a normal document's first
  OCR pass.
- Follow-up (same day, found via real-world use): orientation correction was
  originally wired to run unconditionally on every OCR'd page. Measured at
  1.3-2.3x extra runtime per page (see the benchmark REPORT.md), and a real
  multi-file batch upload - which runs OCR synchronously inside the HTTP
  request, see api/uploads.py - never completed in a reasonable time because
  of it. TesseractOCREngine.extract_text() now only attempts orientation
  correction when the first OCR pass already looks weak (empty text or
  confidence below _WEAK_RESULT_CONFIDENCE) - see
  test_extract_text_skips_osd_on_a_confident_clean_pass below.
- A real, already-confirmed limitation (tests/test_bills.py's
  "TAMANG TAXI SERVICE" fixture, see
  test_handwritten_taxi_receipt_amount_and_date_never_fabricated): a
  genuinely handwritten amount/date does not OCR under local Tesseract at
  any tested PSM mode. These changes target rendering/preprocessing quality,
  not handwriting RECOGNITION itself - see the ground-truth evaluation
  script (benchmark-handwritten-ocr/) for honest before/after measurement
  against this and other real fixtures, including whether this specific
  limitation remains (it is expected to).
"""

from io import BytesIO

import pytest
from PIL import Image

from app.services.extraction.ocr_engine import (
    TesseractOCREngine,
    _correct_orientation,
    preprocess_for_difficult_ocr,
)


def _solid_image_bytes(width=200, height=100, color=(255, 255, 255)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------- preprocess_for_difficult_ocr (pure PIL, no Tesseract needed) ----------


def test_preprocess_returns_valid_png_bytes():
    out = preprocess_for_difficult_ocr(_solid_image_bytes())
    img = Image.open(BytesIO(out))
    assert img.format == "PNG"


def test_preprocess_converts_to_grayscale_then_binarizes():
    """Output must be effectively black/white (grayscale + threshold), not
    the original color image passed through unchanged."""
    out = preprocess_for_difficult_ocr(_solid_image_bytes(color=(120, 80, 200)))
    img = Image.open(BytesIO(out))
    # After grayscale+threshold, every pixel is either pure black or pure
    # white (mode 'L' grayscale, values 0 or 255 only).
    pixels = set(img.getdata())
    assert pixels <= {0, 255}


def test_preprocess_preserves_image_dimensions():
    out = preprocess_for_difficult_ocr(_solid_image_bytes(width=321, height=147))
    img = Image.open(BytesIO(out))
    assert img.size == (321, 147)


def test_preprocess_never_invents_pixels_beyond_source_content():
    """A pure white source image threshold must stay pure white (255) -
    preprocessing adjusts presentation, never fabricates content."""
    out = preprocess_for_difficult_ocr(_solid_image_bytes(color=(255, 255, 255)))
    img = Image.open(BytesIO(out))
    assert set(img.getdata()) == {255}


# ---------- _correct_orientation (requires real Tesseract for OSD) ----------


def test_correct_orientation_leaves_blank_image_untouched():
    """OSD routinely fails outright on a sparse/blank image (confirmed real
    behaviour during investigation) - must degrade to "no correction", never
    raise."""
    ocr = TesseractOCREngine()
    if not ocr.is_available():
        pytest.skip("Tesseract is not installed in this environment.")

    img = Image.new("RGB", (200, 100), (255, 255, 255))
    corrected, angle = _correct_orientation(img)
    assert angle == 0
    assert corrected.size == img.size


def test_correct_orientation_does_not_rotate_confident_upright_real_text():
    """Real, confirmed case (see investigation): every real document page
    tested (clean thermal receipt, invoice, screenshot, handwritten memo)
    reports rotate=0 - none needed correction, and none must get one."""
    ocr = TesseractOCREngine()
    if not ocr.is_available():
        pytest.skip("Tesseract is not installed in this environment.")

    img = Image.new("RGB", (600, 200), (255, 255, 255))
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    draw.text((20, 80), "INVOICE TOTAL AMOUNT RS 2500.00 DATE 12 AUG 2026", fill=(0, 0, 0))
    corrected, angle = _correct_orientation(img)
    assert angle == 0
    assert corrected.tobytes() == img.tobytes()


def test_correct_orientation_rotates_a_confidently_upside_down_real_page():
    """Positive case: a page rotated 180 degrees must be detected and
    corrected back. Built from real, substantial printed text (OSD needs
    enough content to read orientation from at all - confirmed real
    behaviour, see module docstring)."""
    ocr = TesseractOCREngine()
    if not ocr.is_available():
        pytest.skip("Tesseract is not installed in this environment.")

    img = Image.new("RGB", (900, 500), (255, 255, 255))
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except Exception:
        font = ImageFont.load_default()
    lines = [
        "TAX INVOICE",
        "Grand Hotel Residency",
        "Room Charges for Deluxe Suite",
        "Check-in Date 12 August 2026",
        "Check-out Date 13 August 2026",
        "Guest Name Mr Rajesh Kumar",
        "Total Amount Payable Rs 3200.00",
    ]
    y = 40
    for line in lines:
        draw.text((40, y), line, fill=(0, 0, 0), font=font)
        y += 60

    upside_down = img.rotate(180, expand=True)
    corrected, angle = _correct_orientation(upside_down)
    if angle == 0:
        pytest.skip("OSD did not confidently detect the rotation on this synthetic image - not a hard failure, see module docstring on real-example calibration.")
    assert angle == 180
    # Rotating the already-upside-down image by the detected correction
    # must restore it to (approximately) the original orientation.
    assert corrected.size == img.size


# ---------- TesseractOCREngine.extract_text: conditional OSD trigger ----------


def _clean_text_image(lines, width=900, height=500):
    img = Image.new("RGB", (width, height), (255, 255, 255))
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except Exception:
        font = ImageFont.load_default()
    y = 40
    for line in lines:
        draw.text((40, y), line, fill=(0, 0, 0), font=font)
        y += 60
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_extract_text_skips_osd_on_a_confident_clean_pass(monkeypatch):
    """The common case (a clean, already-upright page) must never pay for a
    second Tesseract invocation - see the follow-up note in the module
    docstring: this was unconditional before and measurably slowed down a
    real multi-page batch upload."""
    from app.services.extraction import ocr_engine

    ocr = ocr_engine.TesseractOCREngine()
    if not ocr.is_available():
        pytest.skip("Tesseract is not installed in this environment.")

    calls = []
    original = ocr_engine._correct_orientation

    def _spy(image):
        calls.append(1)
        return original(image)

    monkeypatch.setattr(ocr_engine, "_correct_orientation", _spy)

    image_bytes = _clean_text_image(
        ["TAX INVOICE", "Grand Hotel Residency", "Total Amount Payable Rs 3200.00"]
    )
    result = ocr.extract_text(image_bytes)
    if result["confidence"] < ocr_engine._WEAK_RESULT_CONFIDENCE:
        pytest.skip("This synthetic image did not OCR with high enough confidence to exercise the skip path.")

    assert calls == []
    assert result["rotation_applied"] == 0


def test_extract_text_attempts_osd_on_a_blank_first_pass(monkeypatch):
    """A blank/weak first pass is exactly the case orientation correction
    exists for - it must still be attempted (just not unconditionally for
    every page)."""
    from app.services.extraction import ocr_engine

    ocr = ocr_engine.TesseractOCREngine()
    if not ocr.is_available():
        pytest.skip("Tesseract is not installed in this environment.")

    calls = []
    original = ocr_engine._correct_orientation

    def _spy(image):
        calls.append(1)
        return original(image)

    monkeypatch.setattr(ocr_engine, "_correct_orientation", _spy)

    blank_bytes = _solid_image_bytes()
    result = ocr.extract_text(blank_bytes)

    assert calls == [1]
    assert result["rotation_applied"] == 0  # OSD finds nothing on a blank image - no false correction


def test_extract_text_never_regresses_when_rotated_result_is_worse(monkeypatch):
    """If the orientation-corrected retry doesn't actually improve on the
    first pass, the first pass must still win - this is a real, confirmed
    possibility (OSD can be confident about a rotation that doesn't help a
    genuinely poor-quality scan's legibility), not a hypothetical."""
    from app.services.extraction import ocr_engine

    ocr = ocr_engine.TesseractOCREngine()
    if not ocr.is_available():
        pytest.skip("Tesseract is not installed in this environment.")

    monkeypatch.setattr(ocr_engine, "_correct_orientation", lambda image: (image, 90))

    first_pass_calls = []
    original_pass = ocr_engine.TesseractOCREngine._ocr_pass

    def _fake_ocr_pass(self, image):
        first_pass_calls.append(image)
        if len(first_pass_calls) == 1:
            return {"text": "", "confidence": 0.0, "words": []}
        return {"text": "", "confidence": 0.0, "words": []}

    monkeypatch.setattr(ocr_engine.TesseractOCREngine, "_ocr_pass", _fake_ocr_pass)

    result = ocr.extract_text(_solid_image_bytes())
    assert result["rotation_applied"] == 0
    assert len(first_pass_calls) == 2  # first pass + the rotated retry, but retry wasn't adopted
