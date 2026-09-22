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

    def _apply_ocr(self, pdf_path: Path, pages: list[dict[str, Any]]) -> None:
        if not self.ocr_engine:
            return
        doc = fitz.open(pdf_path)
        try:
            for page in pages:
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

    def content_pages(self, pdf_path: Path) -> list[dict[str, Any]]:
        """The leading run of pages that carry real document content (header +
        expense/trip table). Stops at the first page with no usable text once
        content has started - later pages are photographed supporting bills,
        handled separately by bill_pages_from()."""
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

        self._apply_ocr(pdf_path, content)
        return content

    def bill_pages_from(self, pdf_path: Path, start_page: int) -> list[dict[str, Any]]:
        """All pages from start_page (1-indexed) through the end of the
        document - the supporting-bill region content_pages() deliberately
        stops before. OCR is applied per page where native text is thin."""
        pages = self.split_pages(pdf_path)
        subset = [p for p in pages if p["page_number"] >= start_page]
        self._apply_ocr(pdf_path, subset)
        return subset

    def detect_document_type(self, pdf_path: Path) -> str:
        pages = self.content_pages(pdf_path)
        combined_text = "\n".join(p["text"] for p in pages)
        return classify_text(combined_text)
