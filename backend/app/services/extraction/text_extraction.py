"""Page-by-page PDF text extraction with an OCR fallback for pages that turn
out to have no usable native text. See services/interfaces.py:PDFProcessor.
"""

import re
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from app.services.extraction import nvidia_ocr
from app.services.extraction.classifier import classify_text
from app.services.interfaces import OCREngine, PDFProcessor

MIN_NATIVE_TEXT_CHARS = 20
# Below this, Tesseract's own result is treated as "cannot confidently
# provide required evidence" and the (opt-in) NVIDIA Nemotron OCR v2
# fallback is tried for that page - see nvidia_ocr.py's module docstring for
# why this stays at the text layer rather than asking the model for
# structured fields directly.
MIN_OCR_CONFIDENCE = 0.55

# Generalized, document-agnostic signal for "does this page's linear text
# already pair a recognizable field label with a nearby value?" - used only
# to decide whether the (more expensive) position-aware re-extraction below
# is worth attempting for a given native-text page, never as a field
# extraction decision itself. Deliberately a small, generic set of English
# financial-document terms, not tied to any single vendor/layout/document.
_AMOUNT_LIKE_RE = re.compile(r"(?:₹|rs\.?|inr)\s*[\d,]+(?:\.\d{1,2})?|\b\d[\d,]*\.\d{2}\b", re.IGNORECASE)
_VALUE_PROXIMITY_LABELS = ("amount", "total", "fare", "catering", "charge", "payable", "balance")


_MAX_LABEL_LINE_CHARS = 50  # a form-field label line is short; a boilerplate/
# terms-and-conditions sentence merely containing one of these common words
# (e.g. "A clerkage charge of Rs.60 per passenger..." genuinely contains
# "charge" next to a currency-shaped "Rs.60") is not a label - it's prose,
# reliably distinguished by length alone without needing document-specific
# exclusion lists.


def _text_has_labelled_values(text: str) -> bool:
    lines = text.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > _MAX_LABEL_LINE_CHARS:
            continue
        lower = stripped.lower()
        if any(label in lower for label in _VALUE_PROXIMITY_LABELS):
            window = line + "\n" + (lines[i + 1] if i + 1 < len(lines) else "")
            if _AMOUNT_LIKE_RE.search(window):
                return True
    return False


def _reconstruct_native_text(page: "fitz.Page") -> str:
    """Position-aware text reconstruction via PyMuPDF's structured
    get_text("dict") mode: groups text spans into visual rows by Y-coordinate
    proximity, each row ordered left-to-right, rows ordered top-to-bottom.

    PyMuPDF's own linear get_text() follows the PDF's internal content-stream
    text-run order, which for some table/form layouts interleaves or
    separates a label from its value even though they share a visual row
    (confirmed on a real IRCTC ticket: "Catering Charges (Incl. of GST)" and
    its "₹ 174.00" value are emitted as entirely separate, non-adjacent runs
    in linear order). This never adds, removes, or guesses any text - it is
    a strict re-grouping of exactly the same spans PyMuPDF already found,
    using their own bounding-box coordinates instead of stream order.
    """
    d = page.get_text("dict")
    spans: list[dict[str, Any]] = []
    for block in d["blocks"]:
        if "lines" not in block:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                text = span["text"]
                if text.strip():
                    x0, y0, _, _ = span["bbox"]
                    spans.append({"x0": x0, "y0": y0, "text": text})
    if not spans:
        return ""

    spans.sort(key=lambda s: (s["y0"], s["x0"]))
    y_tolerance = 3.0
    rows: list[list[dict[str, Any]]] = []
    row_y: float | None = None
    for s in spans:
        if rows and row_y is not None and abs(s["y0"] - row_y) <= y_tolerance:
            rows[-1].append(s)
        else:
            rows.append([s])
            row_y = s["y0"]

    lines = [" ".join(s["text"] for s in sorted(row, key=lambda s: s["x0"])) for row in rows]
    return "\n".join(lines)


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
                    image_bytes = doc[page["page_number"] - 1].get_pixmap(dpi=200).tobytes("png")
                    result = self.ocr_engine.extract_text(image_bytes)
                    if result["text"].strip():
                        page["text"] = result["text"]
                        page["source"] = "ocr"
                        page["ocr_confidence"] = result["confidence"]
                        page["has_selectable_text"] = True

                    # Local OCR came back empty, or confident enough that it
                    # isn't "insufficient" (MIN_OCR_CONFIDENCE) - only then
                    # is the optional NVIDIA fallback worth its network call.
                    text_is_weak = not result["text"].strip() or result["confidence"] < MIN_OCR_CONFIDENCE
                    if text_is_weak and nvidia_ocr.is_available():
                        fallback = nvidia_ocr.extract_text_from_image(image_bytes)
                        if fallback is not None and fallback["text"].strip():
                            page["text"] = fallback["text"]
                            page["source"] = "nvidia_ocr"
                            page["ocr_confidence"] = fallback["confidence"]
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
        stops before. OCR is applied per page where native text is thin;
        position-aware re-extraction is applied per native page where the
        fast linear extraction didn't already pair a label with a value."""
        pages = self.split_pages(pdf_path)
        subset = [p for p in pages if p["page_number"] >= start_page]
        self._apply_ocr(pdf_path, subset)
        self._apply_position_aware_fallback(pdf_path, subset)
        return subset

    def _apply_position_aware_fallback(self, pdf_path: Path, pages: list[dict[str, Any]]) -> None:
        candidates = [
            p for p in pages if p["source"] == "native" and p["has_selectable_text"] and not _text_has_labelled_values(p["text"])
        ]
        if not candidates:
            return
        doc = fitz.open(pdf_path)
        try:
            for page in candidates:
                reconstructed = _reconstruct_native_text(doc[page["page_number"] - 1])
                # Only adopt the reconstruction if it actually resolves the
                # thing the fast path couldn't - never swap in a worse or
                # merely-different result for its own sake.
                if reconstructed.strip() and _text_has_labelled_values(reconstructed):
                    page["text"] = reconstructed
                    page["source"] = "native_positioned"
        finally:
            doc.close()

    def detect_document_type(self, pdf_path: Path) -> str:
        pages = self.content_pages(pdf_path)
        combined_text = "\n".join(p["text"] for p in pages)
        return classify_text(combined_text)
