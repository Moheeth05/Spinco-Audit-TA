"""Tesseract-backed OCR fallback for pages with no usable native PDF text.

Only invoked when a page needed for extraction has too little selectable
text (see text_extraction.py) - never used for pages outside that need,
so photographed supporting bills (out of scope until Phase 4) are left
alone rather than OCR'd unnecessarily.
"""

import os
from io import BytesIO
from pathlib import Path
from typing import Any

from app.services.interfaces import OCREngine

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
    _configured = True

    import pytesseract

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


class TesseractOCREngine(OCREngine):
    def __init__(self):
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is None:
            try:
                _configure_tesseract()
                import pytesseract

                pytesseract.get_tesseract_version()
                self._available = True
            except Exception:
                self._available = False
        return self._available

    def extract_text(self, image_bytes: bytes) -> dict[str, Any]:
        if not self.is_available():
            return {"text": "", "confidence": 0.0}

        import pytesseract
        from PIL import Image

        image = Image.open(BytesIO(image_bytes))
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        words = [w for w in data["text"] if w.strip()]
        confidences = [int(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and c != "-1"]
        avg_confidence = (sum(confidences) / len(confidences) / 100) if confidences else 0.0
        return {"text": " ".join(words), "confidence": avg_confidence}
