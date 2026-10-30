// Port of backend/app/services/extraction/text_extraction.py.
//
// The one piece where pdfjs-dist and PyMuPDF genuinely differ, confirmed
// back in the original Node-extraction experiment: PyMuPDF's plain
// get_text() already does its own internal line reconstruction from text
// spans; pdfjs-dist's getTextContent() returns items in content-stream
// order with no such reconstruction built in. "Linear" extraction here is
// therefore an approximation (position-proximity line-joining over the
// stream-order items), not a true analog of PyMuPDF's get_text() - flagged
// honestly rather than presented as equivalent. The position-aware
// reconstruction path (used as a fallback on both sides) uses the same
// sort-by-(y,x)-then-group-by-y-tolerance technique PyMuPDF's dict-mode
// span data enables, applied to pdfjs-dist's own per-item position data -
// that part IS a faithful, direct algorithmic port.
//
// Experimental / reference only - see README.md. Never wired into the
// production backend or Electron app.

import { createCanvas } from "@napi-rs/canvas";
import { readFile } from "node:fs/promises";
import { getDocument } from "pdfjs-dist/legacy/build/pdf.mjs";

const MIN_NATIVE_TEXT_CHARS = 20;
const MIN_OCR_CONFIDENCE = 0.55;

// ---------- Usable native text (not just "any 20 characters") ----------
//
// Investigated per a task asking to fix a described Node-vs-Python
// has_selectable_text divergence. That specific divergence did not
// reproduce: a corpus-wide, pre-OCR comparison across all 168 real pages
// (scripts/diagnose-selectable-text-divergence.mjs, see
// reports/selectable-text-divergence-diagnosis.json) found 0 disagreements
// - both sides use the identical MIN_NATIVE_TEXT_CHARS=20 and agree on
// every page. The real divergence previously documented on vivek-bhai
// page 6 is OCR-ENGINE behavior downstream of this decision (Python's
// local Tesseract returned empty text for that image; tesseract.js
// returned usable text) - both pipelines correctly agreed OCR was needed
// there (has_selectable_text=false on both sides); that's a separate,
// already-documented OCR-quality concern, not a text-detection threshold
// bug.
//
// The underlying "just count characters" check was still a legitimately
// blunt instrument independent of that - a 20-character run of isolated
// symbols/page-artifacts (no real letters or digits) would pass the old
// check and wrongly skip OCR. This closes that specific gap without
// raising the bar on short-but-genuine text (a short real receipt or a
// single-transaction screenshot, explicitly required to keep working):
// meaningful alphanumeric content and real word-shaped tokens are used
// alongside raw length, not instead of it - a short page can still count
// as usable if it has enough of BOTH, even under the 20-char mark.
const MIN_MEANINGFUL_ALNUM_CHARS = 8;
const MIN_WORD_TOKENS = 2;
const WORD_TOKEN_RE = /[A-Za-z]{2,}|\d+/g;

export function usableNativeTextStats(text) {
  const trimmed = (text || "").trim();
  const alnumChars = (trimmed.match(/[A-Za-z0-9]/g) || []).length;
  const wordTokens = (trimmed.match(WORD_TOKEN_RE) || []).length;
  return { length: trimmed.length, alnumChars, wordTokens };
}

export function hasUsableNativeText(text) {
  const { length, alnumChars, wordTokens } = usableNativeTextStats(text);
  if (length === 0) return false;
  // The original bar (enough raw length), now additionally requiring the
  // text not be mostly symbol/artifact noise - a 20+ character run that's
  // nearly all punctuation/graphics-misread characters no longer counts.
  if (length >= MIN_NATIVE_TEXT_CHARS && alnumChars >= MIN_MEANINGFUL_ALNUM_CHARS) return true;
  // Short but genuinely word-bearing text - several real word/number
  // tokens is a stronger "this is real content" signal than raw character
  // count alone, and is what keeps a short real receipt or a single-
  // transaction screenshot correctly classified as usable even under the
  // 20-character mark.
  if (alnumChars >= MIN_MEANINGFUL_ALNUM_CHARS && wordTokens >= MIN_WORD_TOKENS) return true;
  return false;
}

const AMOUNT_LIKE_RE = /(?:₹|rs\.?|inr)\s*[\d,]+(?:\.\d{1,2})?|\b\d[\d,]*\.\d{2}\b/i;
const VALUE_PROXIMITY_LABELS = ["amount", "total", "fare", "catering", "charge", "payable", "balance"];
const DATE_LIKE_RE = /\b\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b/;
const EXPENSE_ROW_START_RE = /^(\d+)\.$/;
const EXPENSE_ROW_DATE_LOOKAHEAD = 2;
const EXPENSE_ROW_WELL_FORMED_RATIO = 0.8;
const MAX_LABEL_LINE_CHARS = 50;

export function expenseRowsLookWellFormed(text) {
  const lines = text.split("\n");
  const rowIndices = [];
  lines.forEach((line, i) => {
    if (EXPENSE_ROW_START_RE.test(line.trim())) rowIndices.push(i);
  });
  if (!rowIndices.length) return true;
  let wellFormed = 0;
  for (const idx of rowIndices) {
    const window = lines.slice(idx, idx + 1 + EXPENSE_ROW_DATE_LOOKAHEAD).join("\n");
    if (DATE_LIKE_RE.test(window)) wellFormed++;
  }
  return wellFormed / rowIndices.length >= EXPENSE_ROW_WELL_FORMED_RATIO;
}

export function textHasLabelledValues(text) {
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const stripped = lines[i].trim();
    if (!stripped || stripped.length > MAX_LABEL_LINE_CHARS) continue;
    const lower = stripped.toLowerCase();
    if (VALUE_PROXIMITY_LABELS.some((label) => lower.includes(label))) {
      const window = lines[i] + "\n" + (i + 1 < lines.length ? lines[i + 1] : "");
      if (AMOUNT_LIKE_RE.test(window)) return true;
    }
  }
  return false;
}

// Groups raw pdfjs-dist text items (content-stream order, each with its own
// x/y from `transform`) into visual rows by Y-coordinate proximity, each
// row ordered left-to-right, rows ordered top-to-bottom - the direct
// algorithmic equivalent of PyMuPDF dict-mode's span bbox grouping.
function reconstructFromItems(items) {
  const spans = items
    .filter((it) => it.str && it.str.trim())
    .map((it) => ({ x0: it.transform[4], y0: it.transform[5], text: it.str }));
  if (!spans.length) return "";

  // pdfjs-dist's y axis increases upward (PDF user space); PyMuPDF's
  // increases downward. Negating y0 here makes "sort ascending" mean
  // "top to bottom" in both, so the row-grouping/sort logic below is
  // otherwise identical to the Python port.
  for (const s of spans) s.y0 = -s.y0;

  spans.sort((a, b) => (a.y0 - b.y0) || (a.x0 - b.x0));
  const yTolerance = 3.0;
  const rows = [];
  let rowY = null;
  for (const s of spans) {
    if (rows.length && rowY !== null && Math.abs(s.y0 - rowY) <= yTolerance) {
      rows[rows.length - 1].push(s);
    } else {
      rows.push([s]);
      rowY = s.y0;
    }
  }
  return rows.map((row) => row.sort((a, b) => a.x0 - b.x0).map((s) => s.text).join(" ")).join("\n");
}

// Approximates PyMuPDF's plain get_text(). Three independent signals,
// each catching cases the others miss, diagnosed and verified against the
// real ER-40513 document's actual pdfjs-dist item stream - a line break is
// inserted whenever ANY fires:
//
// 1. pdfjs-dist's own `hasEOL` flag - a content-stream-native "a line ends
//    here" marker, including on empty-string marker items that carry no
//    text of their own (discarding those before checking hasEOL throws
//    away real signal). Correctly separates most of the Expense Summary
//    table's per-field lines - but under-fires on a horizontal multi-cell
//    summary strip ("Total Expense" / "( - )" / "Non Reimbursable" packed
//    with no hasEOL between them) where PyMuPDF's get_text() still
//    separates each cell onto its own line.
// 2. A Y-position jump beyond tolerance from the previous item - exactly
//    what PyMuPDF's line-detection is doing for that horizontal strip
//    (each cell sits at a slightly different Y). Recovers case 1's gap,
//    but alone over-merges the table: a short row's S.No/date/category
//    can all sit at the SAME Y with no hasEOL between them either.
// 3. An anomalously wide whitespace-only item. The PDF generator renders
//    a table-column gap as a single " " text item whose `width` is
//    stretched far beyond a real inter-word space (confirmed real values:
//    31.5pt and 140pt, for gaps of a S.No->Details column jump and a
//    Details->Category column jump respectively, vs. a genuine space at
//    typical body-text size never exceeding a few points) - this is what
//    signals 1 and 2 both miss for a short row with no wrapped detail
//    line, where S.No/date/category end up on one content-stream line at
//    one shared Y. The oversized space is replaced with a line break
//    rather than emitted literally, so it never shows up as stray
//    whitespace in the reconstructed text.
//
// No single signal matches PyMuPDF's actual line segmentation for this
// document; their union does (verified against all 3 dev-set PDFs: 100%
// exact-line overlap on every content page - see reports/).
// 5.0 rather than the original 3.0: a policy-violation superscript marker
// (e.g. "LOCAL CONVEYANCE [1]") sits on a slightly raised baseline - a real
// confirmed case measured a 3.48pt Y offset for a small "[1]" annotation
// immediately after its category text with only a 2pt gap, which a 3.0pt
// tolerance wrongly treated as a new line. The genuine column-break cases
// (the horizontal REPORT SUMMARY strip) measure ~6pt Y offsets, comfortably
// still caught at 5.0.
const LINE_Y_TOLERANCE = 5.0;
const WIDE_SPACE_THRESHOLD = 15.0;

function linearTextFromItems(items) {
  let out = "";
  let lastY = null;
  for (const it of items) {
    const y = it.transform[5];
    const yJump = lastY !== null && Math.abs(y - lastY) > LINE_Y_TOLERANCE;
    const isWideSpace = it.str.trim() === "" && it.str.length > 0 && it.width > WIDE_SPACE_THRESHOLD;
    if (yJump && out && !out.endsWith("\n")) out += "\n";
    if (isWideSpace) {
      if (out && !out.endsWith("\n")) out += "\n";
    } else if (it.str) {
      out += it.str;
    }
    if (it.hasEOL && !out.endsWith("\n")) out += "\n";
    lastY = y;
  }
  return out;
}

async function loadDoc(pdfPath) {
  const data = await readFile(pdfPath);
  return getDocument({ data: new Uint8Array(data), useSystemFonts: true }).promise;
}

export class PdfJsTextExtractor {
  constructor(ocrEngine = null) {
    this.ocrEngine = ocrEngine;
  }

  async splitPages(pdfPath) {
    const doc = await loadDoc(pdfPath);
    const pages = [];
    try {
      for (let i = 1; i <= doc.numPages; i++) {
        const page = await doc.getPage(i);
        const content = await page.getTextContent();
        const text = linearTextFromItems(content.items);
        pages.push({
          pageNumber: i,
          text,
          hasSelectableText: hasUsableNativeText(text),
          source: "native",
          ocrConfidence: null,
        });
      }
    } finally {
      await doc.destroy();
    }
    return pages;
  }

  async _renderPageToPng(pdfPath, pageNumber, dpi = 200) {
    const doc = await loadDoc(pdfPath);
    try {
      const page = await doc.getPage(pageNumber);
      const scale = dpi / 72;
      const viewport = page.getViewport({ scale });
      const canvas = createCanvas(Math.ceil(viewport.width), Math.ceil(viewport.height));
      const ctx = canvas.getContext("2d");
      await page.render({ canvasContext: ctx, viewport }).promise;
      return canvas.toBuffer("image/png");
    } finally {
      await doc.destroy();
    }
  }

  async _applyOcr(pdfPath, pages) {
    if (!this.ocrEngine) return;
    for (const page of pages) {
      if (page.hasSelectableText) continue;
      const imageBytes = await this._renderPageToPng(pdfPath, page.pageNumber);
      const result = await this.ocrEngine.extractText(imageBytes);
      if (result.text.trim()) {
        page.text = result.text;
        page.source = "ocr";
        page.ocrConfidence = result.confidence;
        page.hasSelectableText = true;
      }
      // NVIDIA OCR fallback intentionally not ported - opt-in external API,
      // no-op by default in the real app (no key configured).
    }
  }

  async _reconstructPage(pdfPath, pageNumber) {
    const doc = await loadDoc(pdfPath);
    try {
      const page = await doc.getPage(pageNumber);
      const content = await page.getTextContent();
      return reconstructFromItems(content.items);
    } finally {
      await doc.destroy();
    }
  }

  async _applyPositionAwareFallbackToExpensePages(pdfPath, pages) {
    const candidates = pages.filter(
      (p) => p.source === "native" && p.hasSelectableText && !expenseRowsLookWellFormed(p.text)
    );
    for (const page of candidates) {
      const reconstructed = await this._reconstructPage(pdfPath, page.pageNumber);
      if (reconstructed.trim() && expenseRowsLookWellFormed(reconstructed)) {
        page.text = reconstructed;
        page.source = "native_positioned";
      }
    }
  }

  async _applyPositionAwareFallback(pdfPath, pages) {
    const candidates = pages.filter(
      (p) => p.source === "native" && p.hasSelectableText && !textHasLabelledValues(p.text)
    );
    for (const page of candidates) {
      const reconstructed = await this._reconstructPage(pdfPath, page.pageNumber);
      if (reconstructed.trim() && textHasLabelledValues(reconstructed)) {
        page.text = reconstructed;
        page.source = "native_positioned";
      }
    }
  }

  async contentPages(pdfPath) {
    const pages = await this.splitPages(pdfPath);
    const content = [];
    let seenSubstantial = false;
    for (const page of pages) {
      if (page.hasSelectableText) {
        content.push(page);
        seenSubstantial = true;
      } else if (!seenSubstantial) {
        content.push(page);
      } else {
        break;
      }
    }
    await this._applyOcr(pdfPath, content);
    await this._applyPositionAwareFallbackToExpensePages(pdfPath, content);
    return content;
  }

  async billPagesFrom(pdfPath, startPage) {
    const pages = await this.splitPages(pdfPath);
    const subset = pages.filter((p) => p.pageNumber >= startPage);
    await this._applyOcr(pdfPath, subset);
    await this._applyPositionAwareFallback(pdfPath, subset);
    return subset;
  }
}
