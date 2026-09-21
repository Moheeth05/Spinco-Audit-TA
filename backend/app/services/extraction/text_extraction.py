"""Page-by-page PDF text extraction with an OCR fallback for pages that turn
out to have no usable native text. See services/interfaces.py:PDFProcessor.
"""

from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from app.services.extraction.classifier import classify_text
from app.services.interfaces import OCREngine, PDFProcessor

MIN_NATIVE_TEXT_CHARS = 20


class PyMuPDFTextExtractor(PDFProcessor):
    def __init__(self, ocr_engine: OCREngine | None = None):
        self.ocr_engine = ocr_engine

    def split_pages(self, pdf_path: Path) -> list[dict[str, Any]]:
        doc = fitz.open(pdf_path)
        try:
            pages = []
            for i in range(doc.page_count):
                text = doc[i].get_text()
                pages.append(
                    {
                        "page_number": i + 1,
                        "text": text,
                        "has_selectable_text": len(text.strip()) >= MIN_NATIVE_TEXT_CHARS,
                        "source": "native",
                        "ocr_confidence": None,
                    }
                )
            return pages
        finally:
            doc.close()

    def content_pages(self, pdf_path: Path) -> list[dict[str, Any]]:
        """The leading run of pages that carry real document content (header +
        expense/trip table). Stops at the first page with no usable text once
        content has started - later pages are photographed supporting bills,
        out of scope until the bill-extraction phase."""
        pages = self.split_pages(pdf_path)
        content: list[dict[str, Any]] = []
        seen_substantial = False
        for page in pages:
            if page["has_selectable_text"]:
                content.append(page)
                seen_substantial = True
            elif not seen_substantial:
                content.append(page)
            else:
                break

        if self.ocr_engine:
            doc = fitz.open(pdf_path)
            try:
                for page in content:
                    if not page["has_selectable_text"]:
                        pix = doc[page["page_number"] - 1].get_pixmap(dpi=200)
                        result = self.ocr_engine.extract_text(pix.tobytes("png"))
                        if result["text"].strip():
                            page["text"] = result["text"]
                            page["source"] = "ocr"
                            page["ocr_confidence"] = result["confidence"]
                            page["has_selectable_text"] = True
            finally:
                doc.close()
        return content

    def detect_document_type(self, pdf_path: Path) -> str:
        pages = self.content_pages(pdf_path)
        combined_text = "\n".join(p["text"] for p in pages)
        return classify_text(combined_text)
