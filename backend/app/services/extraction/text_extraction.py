"""Page-by-page PDF text extraction with an OCR fallback for pages that turn
out to have no usable native text. See services/interfaces.py:PDFProcessor.
"""

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from app.services.extraction import nvidia_ocr
from app.services.extraction.classifier import classify_text
from app.services.extraction.ocr_engine import preprocess_for_difficult_ocr
from app.services.interfaces import OCREngine, PDFProcessor

logger = logging.getLogger(__name__)

MIN_NATIVE_TEXT_CHARS = 20
# Below this, Tesseract's own result is treated as "cannot confidently
# provide required evidence" and the (opt-in) NVIDIA Nemotron OCR v2
# fallback is tried for that page - see nvidia_ocr.py's module docstring for
# why this stays at the text layer rather than asking the model for
# structured fields directly.
MIN_OCR_CONFIDENCE = 0.55

# Render DPI used for the one-shot retry when the default-DPI OCR pass (see
# _apply_ocr below) comes back completely empty. Deliberately triggered only
# by a totally empty/whitespace-only result, never by low confidence alone -
# see experiments/node-extraction/benchmarks/dpi-200-vs-300/REPORT.md, which
# found that re-rendering sharper reliably helped pages that returned
# nothing at the default DPI, but that retrying pages which already had some
# (even low-confidence) text risked changing - not improving - the result on
# at least one real document in that benchmark.
OCR_RETRY_DPI = 300


def _ocr_worker_count() -> int:
    """How many pages are OCR'd concurrently. Each Tesseract call is its own
    subprocess, so threads here genuinely overlap (measured on a real 57-page
    expense report: 21s -> 11s for 24 pages with 3 workers, identical text
    output). Capped at 3 and left at least one core free so the rest of the
    app (and the UI) stays responsive during a batch. SPINCO_OCR_WORKERS
    overrides it (1 restores strictly sequential behaviour)."""
    override = os.environ.get("SPINCO_OCR_WORKERS")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    return max(1, min(3, (os.cpu_count() or 2) - 1))

# Generalized, document-agnostic signal for "does this page's linear text
# already pair a recognizable field label with a nearby value?" - used only
# to decide whether the (more expensive) position-aware re-extraction below
# is worth attempting for a given native-text page, never as a field
# extraction decision itself. Deliberately a small, generic set of English
# financial-document terms, not tied to any single vendor/layout/document.
_AMOUNT_LIKE_RE = re.compile(r"(?:₹|rs\.?|inr)\s*[\d,]+(?:\.\d{1,2})?|\b\d[\d,]*\.\d{2}\b", re.IGNORECASE)
_VALUE_PROXIMITY_LABELS = ("amount", "total", "fare", "catering", "charge", "payable", "balance")

# Loose, structural "this looks like a date" shape (DD/MM/YY(YY) or
# YYYY-MM-DD) - deliberately NOT dates.py's own validated patterns; this
# only has to answer "is there anything date-shaped on the page at all",
# not parse a specific date, so it stays cheap and self-contained here.
_DATE_LIKE_RE = re.compile(r"\b\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b")

# Mirrors expense_report_parser.ROW_START_RE - duplicated rather than
# imported so this lower-layer module (every extraction module sits
# downstream of it) never depends on the parser that consumes its output.
# Used only by _expense_rows_look_well_formed below, as a cheap structural
# probe, never to parse an actual row.
_EXPENSE_ROW_START_RE = re.compile(r"^(\d+)\.$")
_EXPENSE_ROW_DATE_LOOKAHEAD = 2
_EXPENSE_ROW_WELL_FORMED_RATIO = 0.8


def _expense_rows_look_well_formed(text: str) -> bool:
    """Stage-2 generalization (architecture-audit gap #1): content_pages()'s
    own version of _text_has_labelled_values - "did this page's plain linear
    text extraction preserve the Expense Summary table's structure, or did
    PyMuPDF's content-stream order diverge from visual reading order?" A
    genuinely multi-column table layout can interleave a row's own date/
    category/amount with a DIFFERENT row's fields; every real Expense Report
    seen so far happens to linearize cleanly (one field per line, in visual
    order), but nothing in this codebase previously detected or recovered
    from a layout that didn't.

    Cheap, generic probe: every numbered-row marker found in the text should
    have a date-shaped token within the next couple of lines - true for
    every real document in this corpus. A page with no row markers at all
    (the header page before row 1, a REPORT SUMMARY page) is vacuously
    well-formed - nothing to check, never triggers reconstruction."""
    lines = text.split("\n")
    row_indices = [i for i, line in enumerate(lines) if _EXPENSE_ROW_START_RE.match(line.strip())]
    if not row_indices:
        return True
    well_formed = 0
    for idx in row_indices:
        window = "\n".join(lines[idx : idx + 1 + _EXPENSE_ROW_DATE_LOOKAHEAD])
        if _DATE_LIKE_RE.search(window):
            well_formed += 1
    return (well_formed / len(row_indices)) >= _EXPENSE_ROW_WELL_FORMED_RATIO


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

    def _retry_ocr_at_higher_dpi(
        self, doc: "fitz.Document", page_number: int, doc_lock: Any = None
    ) -> tuple[bytes, dict[str, Any]] | None:
        """Targeted fallback for a page whose default-DPI OCR pass returned
        no usable text at all. Two narrow, successive attempts - never
        applied to a normal document, since both require the default pass
        to have already come back completely empty:

        1. Re-render the SAME page at OCR_RETRY_DPI and re-run the SAME OCR
           engine/config, nothing else changed (the original, proven
           retry).
        2. If THAT also comes back empty, one more attempt: the SAME
           OCR_RETRY_DPI render, this time with a measured preprocessing
           pass (grayscale/contrast/threshold - see
           ocr_engine.preprocess_for_difficult_ocr) before OCR'ing it -
           for a faint/low-contrast photographed receipt plain re-rendering
           alone doesn't help.

        Returns None (never raises) if every attempt fails or every attempt
        still comes back empty - the caller then keeps the original empty
        result, exactly as if this retry had never been attempted."""
        try:
            with doc_lock or nullcontext():
                retry_image_bytes = doc[page_number - 1].get_pixmap(dpi=OCR_RETRY_DPI).tobytes("png")
            retry_result = self.ocr_engine.extract_text(retry_image_bytes)
        except Exception:  # noqa: BLE001 - a retry must degrade silently, never break extraction
            logger.exception("OCR retry at %d DPI raised for page %d", OCR_RETRY_DPI, page_number)
            return None

        if retry_result["text"].strip():
            logger.info("OCR retry at %d DPI recovered text for page %d (default DPI returned none)", OCR_RETRY_DPI, page_number)
            return retry_image_bytes, retry_result

        try:
            preprocessed_bytes = preprocess_for_difficult_ocr(retry_image_bytes)
            preprocessed_result = self.ocr_engine.extract_text(preprocessed_bytes)
        except Exception:  # noqa: BLE001 - same degrade-silently contract as the DPI retry above
            logger.exception("OCR preprocessing retry raised for page %d", page_number)
            return None

        if preprocessed_result["text"].strip():
            logger.info(
                "OCR preprocessing retry recovered text for page %d (default DPI and plain %d DPI retry both returned none)",
                page_number,
                OCR_RETRY_DPI,
            )
            return preprocessed_bytes, preprocessed_result

        logger.info("OCR retry at %d DPI (plain and preprocessed) still returned no text for page %d", OCR_RETRY_DPI, page_number)
        return None

    def _ocr_one_page(self, doc: "fitz.Document", doc_lock: threading.Lock, page: dict[str, Any]) -> None:
        """OCR a single page in place. All PyMuPDF access goes through
        doc_lock (PyMuPDF is not thread-safe); the Tesseract/NVIDIA calls,
        which dominate the runtime, run outside it."""
        with doc_lock:
            image_bytes = doc[page["page_number"] - 1].get_pixmap(dpi=200).tobytes("png")
        result = self.ocr_engine.extract_text(image_bytes)

        if not result["text"].strip():
            retried = self._retry_ocr_at_higher_dpi(doc, page["page_number"], doc_lock)
            if retried is not None:
                image_bytes, result = retried

        if result["text"].strip():
            page["text"] = result["text"]
            page["source"] = "ocr"
            page["ocr_confidence"] = result["confidence"]
            page["has_selectable_text"] = True

        # Local OCR came back empty, confident enough that it
        # isn't "insufficient" (MIN_OCR_CONFIDENCE), OR - a
        # genuinely real, confirmed case - its own average
        # word-confidence looks fine (clean printed template
        # text pulls the average up) while the two fields that
        # actually matter are nowhere on the page at all (a
        # handwritten amount/date filled into a printed form's
        # blank lines came out as unrecognizable noise). Only
        # then is the optional NVIDIA fallback worth its network
        # call - this never fires on a page that already has a
        # usable amount or date, confident or not.
        text_is_weak = (
            not result["text"].strip()
            or result["confidence"] < MIN_OCR_CONFIDENCE
            or (
                len(result["text"].strip()) >= MIN_NATIVE_TEXT_CHARS
                and not _AMOUNT_LIKE_RE.search(result["text"])
                and not _DATE_LIKE_RE.search(result["text"])
            )
        )
        if text_is_weak and nvidia_ocr.is_available():
            fallback = nvidia_ocr.extract_text_from_image(image_bytes)
            if fallback is not None and fallback["text"].strip():
                page["text"] = fallback["text"]
                page["source"] = "nvidia_ocr"
                page["ocr_confidence"] = fallback["confidence"]
                page["has_selectable_text"] = True

    def _apply_ocr(self, pdf_path: Path, pages: list[dict[str, Any]]) -> None:
        if not self.ocr_engine:
            return
        todo = [p for p in pages if not p["has_selectable_text"]]
        if not todo:
            return
        doc = fitz.open(pdf_path)
        doc_lock = threading.Lock()
        try:
            workers = min(_ocr_worker_count(), len(todo))
            if workers <= 1:
                for page in todo:
                    self._ocr_one_page(doc, doc_lock, page)
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    # list() re-raises the first worker exception, same as the
                    # previous sequential loop would have.
                    list(pool.map(lambda page: self._ocr_one_page(doc, doc_lock, page), todo))
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
        self._apply_position_aware_fallback_to_expense_pages(pdf_path, content)
        return content

    def _apply_position_aware_fallback_to_expense_pages(self, pdf_path: Path, pages: list[dict[str, Any]]) -> None:
        """Architecture-audit gap #1 fix: content_pages()'s own version of
        bill_pages_from()'s _apply_position_aware_fallback - same technique
        (visual-row reconstruction via _reconstruct_native_text), same
        "only adopt if it actually resolves the thing the fast path
        couldn't" discipline, applied to the Expense Summary table instead
        of a supporting-bill page. A no-op for every document whose linear
        extraction already looks well-formed (the entire real corpus, by
        construction) - this exists for the as-yet-unseen multi-column
        layout where it wouldn't."""
        candidates = [
            p for p in pages if p["source"] == "native" and p["has_selectable_text"] and not _expense_rows_look_well_formed(p["text"])
        ]
        if not candidates:
            return
        doc = fitz.open(pdf_path)
        try:
            for page in candidates:
                reconstructed = _reconstruct_native_text(doc[page["page_number"] - 1])
                if reconstructed.strip() and _expense_rows_look_well_formed(reconstructed):
                    page["text"] = reconstructed
                    page["source"] = "native_positioned"
        finally:
            doc.close()

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
