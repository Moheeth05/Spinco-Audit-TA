// Real-document comparison: does pdfjs-dist's "linear" extraction actually
// match PyMuPDF's get_text() for the real ER-40513 document? Honest
// comparison - report exactly what matches and what doesn't, not just a
// pass/fail count.
import { execFileSync } from "node:child_process";
import { PdfJsTextExtractor } from "./src/textExtraction.mjs";

const REAL_PDF = "D:\\Bills\\uat-data-test\\documents\\expense_reports\\506d7dbf9383_reahul report.pdf";
const PYTHON = "D:\\Bills\\backend\\.venv\\Scripts\\python.exe";

function pyContentPages() {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from pathlib import Path
from app.services.extraction.text_extraction import PyMuPDFTextExtractor
extractor = PyMuPDFTextExtractor(ocr_engine=None)
pages = extractor.content_pages(Path(r"${REAL_PDF}"))
print(json.dumps([{"pageNumber": p["page_number"], "text": p["text"], "source": p["source"]} for p in pages]))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 20 * 1024 * 1024 }));
}

const extractor = new PdfJsTextExtractor(null);
const jsPages = await extractor.contentPages(REAL_PDF);
const pyPages = pyContentPages();

console.log(`JS pages: ${jsPages.length}, Python pages: ${pyPages.length}\n`);

for (let i = 0; i < Math.max(jsPages.length, pyPages.length); i++) {
  const js = jsPages[i];
  const py = pyPages[i];
  console.log(`=== Page ${i + 1} === JS source=${js?.source} Python source=${py?.source}`);
  const jsLines = (js?.text || "").split("\n").map((l) => l.trim()).filter(Boolean);
  const pyLines = (py?.text || "").split("\n").map((l) => l.trim()).filter(Boolean);
  console.log(`  JS lines: ${jsLines.length}, Python lines: ${pyLines.length}`);
  const jsSet = new Set(jsLines);
  const pySet = new Set(pyLines);
  const matching = pyLines.filter((l) => jsSet.has(l)).length;
  console.log(`  Exact-line overlap: ${matching}/${pyLines.length} Python lines also appear verbatim in JS output`);
  if (matching < pyLines.length) {
    const missing = pyLines.filter((l) => !jsSet.has(l));
    console.log(`  Lines in Python but not JS (first 5): ${JSON.stringify(missing.slice(0, 5))}`);
  }
}
