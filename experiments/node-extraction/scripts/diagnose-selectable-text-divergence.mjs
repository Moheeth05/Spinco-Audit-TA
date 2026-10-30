// Step 1: rigorous, corpus-wide, PRE-OCR comparison of the raw
// has_selectable_text decision (native text length >= 20 chars) between
// Node and Python, across every page of every reference PDF - not just
// the one page an earlier report's prose summary named. This isolates the
// TEXT-DETECTION decision itself from OCR-engine behavior (which runs
// only AFTER this decision and is a separate concern already documented
// in reports/ocr-sample-evaluation.json).
import { readFile, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { PdfJsTextExtractor } from "../src/textExtraction.mjs";

const manifest = JSON.parse(await readFile("datasets/manifest.json", "utf-8"));

function pySplitPages(pdfPath) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from pathlib import Path
from app.services.extraction.text_extraction import PyMuPDFTextExtractor, MIN_NATIVE_TEXT_CHARS
extractor = PyMuPDFTextExtractor(ocr_engine=None)
pages = extractor.split_pages(Path(r"${pdfPath}"))
print(json.dumps({
    "minNativeTextChars": MIN_NATIVE_TEXT_CHARS,
    "pages": [{"pageNumber": p["page_number"], "hasSelectableText": p["has_selectable_text"], "textLength": len(p["text"].strip())} for p in pages],
}))
`;
  return JSON.parse(execFileSync("D:\\Bills\\backend\\.venv\\Scripts\\python.exe", ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 50 * 1024 * 1024 }));
}

const divergences = [];
let totalPages = 0;
let pyThreshold = null;

for (const report of manifest.reportsCovered) {
  const pdfPath = `fixtures/reference-pdfs/${report.filename}`;
  const py = pySplitPages(pdfPath);
  pyThreshold = py.minNativeTextChars;
  const extractor = new PdfJsTextExtractor(null);
  const jsPages = await extractor.splitPages(pdfPath);

  for (const pyPage of py.pages) {
    const jsPage = jsPages.find((p) => p.pageNumber === pyPage.pageNumber);
    totalPages++;
    if (!jsPage) continue;
    if (jsPage.hasSelectableText !== pyPage.hasSelectableText) {
      divergences.push({
        filename: report.filename,
        page: pyPage.pageNumber,
        pythonHasSelectableText: pyPage.hasSelectableText,
        pythonTextLength: pyPage.textLength,
        nodeHasSelectableText: jsPage.hasSelectableText,
        nodeTextLength: jsPage.text.trim().length,
      });
    }
  }
  console.log(`${report.filename}: ${py.pages.length} pages checked`);
}

console.log(`\nPython MIN_NATIVE_TEXT_CHARS = ${pyThreshold}`);
console.log(`Node MIN_NATIVE_TEXT_CHARS (hardcoded in src/textExtraction.mjs) = 20`);
console.log(`\nTotal pages checked: ${totalPages}`);
console.log(`hasSelectableText DIVERGENCES (pre-OCR decision only): ${divergences.length}`);
for (const d of divergences) {
  console.log(`  ${d.filename} page ${d.page}: Python=${d.pythonHasSelectableText}(len=${d.pythonTextLength}) Node=${d.nodeHasSelectableText}(len=${d.nodeTextLength})`);
}

await writeFile("reports/selectable-text-divergence-diagnosis.json", JSON.stringify({ pyThreshold, totalPages, divergences }, null, 2));
console.log("\nWrote reports/selectable-text-divergence-diagnosis.json");
