"""Tesseract-backed OCR fallback for pages with no usable native PDF text.

Only invoked when a page needed for extraction has too little selectable
text (see text_extraction.py) - never used for pages outside that need,
so photographed supporting bills (out of scope until Phase 4) are left
alone rather than OCR'd unnecessarily.
"""

from io import BytesIO
from typing import Any

from app.services.interfaces import OCREngine


class TesseractOCREngine(OCREngine):
    def __init__(self):
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is None:
            try:
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
