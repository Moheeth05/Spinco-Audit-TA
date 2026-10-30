// PDF text extraction via pdfjs-dist, mirroring the two Python extraction
// modes compared earlier this project (see backend's text_extraction.py):
//  - linearText(): items in pdfjs's own content-stream reading order,
//    equivalent to PyMuPDF's page.get_text().
//  - positionedText(): items re-grouped into visual rows by Y-coordinate
//    proximity (item.transform[5] gives each item's Y position), equivalent
//    to PyMuPDF's get_text("dict") + row-clustering reconstruction.
//
// Experimental / reference only - see README.md.

import path from "node:path";
import { fileURLToPath } from "node:url";

const pdfjsLib = await import("pdfjs-dist/legacy/build/pdf.mjs");

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STANDARD_FONT_DATA_URL = path.join(
  __dirname,
  "..",
  "node_modules",
  "pdfjs-dist",
  "standard_fonts"
) + path.sep;

export async function openPdf(pdfPath) {
  const loadingTask = pdfjsLib.getDocument({
    url: pathToFileUrl(pdfPath),
    standardFontDataUrl: pathToFileUrl(STANDARD_FONT_DATA_URL),
    verbosity: 0, // suppress font-loading/console warnings - see README
  });
  return loadingTask.promise;
}

function pathToFileUrl(p) {
  const resolved = path.resolve(p).replace(/\\/g, "/");
  return resolved.startsWith("/") ? `file://${resolved}` : `file:///${resolved}`;
}

export async function getPageCount(pdfPath) {
  const doc = await openPdf(pdfPath);
  const count = doc.numPages;
  await doc.destroy();
  return count;
}

async function getItems(doc, pageNumber) {
  const page = await doc.getPage(pageNumber);
  const content = await page.getTextContent();
  // Real-corpus artifact confirmed during the earlier investigation: pdfjs
  // emits duplicate empty-string "str" items at some real spans' own
  // coordinates (no visible text, nothing to group) - filtered out here,
  // never treated as a layout signal.
  return content.items.filter((it) => it.str && it.str.trim().length > 0);
}

// pdfjs-dist's getTextContent() items carry no inter-item spacing of their
// own - unlike PyMuPDF's get_text(), which reconstructs reading-order
// whitespace internally, pdfjs hands back only each run's own characters
// and position. Naively joining items with "" fuses visually-separate runs
// together with no boundary at all (confirmed real case: a receipt's
// "...26" (year) sits immediately before an unrelated "20:58" (time) with
// no space in the content stream, producing the literal digit run "2620" -
// which a 4-digit-year date pattern then misreads as the year 2620). This
// reconstructs the same minimal spacing PyMuPDF provides for free: a
// newline when the next item drops to a new visual line, otherwise a space
// when there's a real horizontal gap, otherwise nothing (mid-word runs).
export async function linearText(pdfPath, pageNumber) {
  const doc = await openPdf(pdfPath);
  try {
    const items = await getItems(doc, pageNumber);
    let text = "";
    let prevEndX = null;
    let prevY = null;
    for (const it of items) {
      const x0 = it.transform[4];
      const y0 = it.transform[5];
      const height = Math.abs(it.transform[3]) || 10;
      if (prevY !== null && Math.abs(y0 - prevY) > height * 0.5) {
        text += "\n";
      } else if (prevEndX !== null && x0 - prevEndX > height * 0.2) {
        text += " ";
      }
      text += it.str;
      prevEndX = x0 + (it.width || 0);
      prevY = y0;
    }
    return text;
  } finally {
    await doc.destroy();
  }
}

export async function positionedText(pdfPath, pageNumber) {
  const doc = await openPdf(pdfPath);
  try {
    const items = await getItems(doc, pageNumber);
    const spans = items.map((it) => ({
      x0: it.transform[4],
      y0: it.transform[5],
      text: it.str,
    }));
    // PDF Y axis increases upward; sort top-to-bottom then left-to-right.
    spans.sort((a, b) => (b.y0 - a.y0) || (a.x0 - b.x0));

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
    const lines = rows.map((row) =>
      row
        .slice()
        .sort((a, b) => a.x0 - b.x0)
        .map((s) => s.text)
        .join(" ")
    );
    return lines.join("\n");
  } finally {
    await doc.destroy();
  }
}
