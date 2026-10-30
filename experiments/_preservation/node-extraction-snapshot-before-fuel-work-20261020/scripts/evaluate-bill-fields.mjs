// Section 4/5: field-level comparison of the existing Node billParser.mjs
// against the Python oracle, for every bill-evidence page found by the
// census. Two layers, like the earlier Expense Report evaluation:
//   (a) business-logic parity - Node's billParser fed the SAME text Python
//       extracted (isolates parser-rule correctness from text-extraction
//       differences; expected near-100% given the 100/100 unit-level
//       parity already established).
//   (b) full end-to-end - Node's OWN PdfJsTextExtractor.billPagesFrom
//       (including Node's own OCR, tesseract.js) feeding Node's OWN
//       billParser. This is what "the existing Node.js extraction"
//       actually means for this task - run only on a representative
//       sample (not every page) because OCR is slow; which pages and why
//       is recorded explicitly, not silently chosen.
import { readFile, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { PdfJsTextExtractor } from "../src/textExtraction.mjs";
import { TesseractJsOCREngine } from "../src/ocrEngine.mjs";
import { parseBillText } from "../src/billParser.mjs";

const census = JSON.parse(await readFile("reports/page-census.json", "utf-8"));

function pyParseBillText(text) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from app.services.extraction import bill_parser
b = bill_parser.parse_bill_text(${JSON.stringify(text)})
print(json.dumps({
    "vendor": b.vendor, "billNumber": b.bill_number,
    "billDate": b.bill_date.isoformat() if b.bill_date else None,
    "amount": b.amount, "gstin": b.gstin, "description": b.description,
    "categoryGuess": b.category_guess, "confidence": b.confidence, "status": b.status,
}))
`;
  return JSON.parse(execFileSync("D:\\Bills\\backend\\.venv\\Scripts\\python.exe", ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 20 * 1024 * 1024 }));
}

function jsParseBillTextNorm(text) {
  const b = parseBillText(text);
  return {
    vendor: b.vendor, billNumber: b.billNumber,
    billDate: b.billDate ? b.billDate.iso : null,
    amount: b.amount, gstin: b.gstin, description: b.description,
    categoryGuess: b.categoryGuess, confidence: b.confidence, status: b.status,
  };
}

const FIELDS = ["vendor", "billNumber", "billDate", "amount", "gstin", "categoryGuess"];

// --- Layer (a): business-logic parity on every bill-evidence page, reusing Python's already-extracted text ---
// The census only stored a 200-char text preview per page; re-run the real
// extraction here to get FULL page text for every bill-evidence page
// (still native/OCR exactly as the census already decided per page - this
// just re-fetches the full text, no new OCR decisions are made).
const logicParity = { total: 0, fieldMatches: Object.fromEntries(FIELDS.map((f) => [f, 0])), mismatches: [] };
function pyFullBillPages(pdfPath, startPage) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from pathlib import Path
from app.services.extraction.text_extraction import PyMuPDFTextExtractor
from app.services.extraction.ocr_engine import TesseractOCREngine
extractor = PyMuPDFTextExtractor(ocr_engine=TesseractOCREngine())
pages = extractor.bill_pages_from(Path(r"${pdfPath}"), ${startPage})
print(json.dumps([{"pageNumber": p["page_number"], "text": p["text"], "source": p["source"]} for p in pages]))
`;
  return JSON.parse(execFileSync("D:\\Bills\\backend\\.venv\\Scripts\\python.exe", ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 80 * 1024 * 1024 }));
}

for (const report of census.reports) {
  const pdfPath = `fixtures/reference-pdfs/${report.filename}`;
  const startPage = report.contentLastPage + 1;
  if (startPage > report.pages.length) continue;
  const fullPages = pyFullBillPages(pdfPath, startPage);
  for (const p of fullPages) {
    const censusEntry = report.pages.find((c) => c.pageNumber === p.pageNumber);
    if (!censusEntry || !censusEntry.hasBillEvidence) continue;
    const py = pyParseBillText(p.text);
    const js = jsParseBillTextNorm(p.text);
    logicParity.total++;
    for (const f of FIELDS) {
      if (py[f] === js[f]) logicParity.fieldMatches[f]++;
      else {
        logicParity.mismatches.push({ filename: report.filename, page: p.pageNumber, field: f, python: py[f], js: js[f] });
      }
    }
  }
  console.log(`[logic parity] ${report.filename}: processed bill pages ${startPage}-${report.pages.length}`);
}

console.log("\n=== Layer (a): business-logic parity (same text both sides) ===");
console.log(`Pages evaluated: ${logicParity.total}`);
for (const f of FIELDS) {
  const pct = logicParity.total ? ((logicParity.fieldMatches[f] / logicParity.total) * 100).toFixed(1) : "n/a";
  console.log(`  ${f}: ${logicParity.fieldMatches[f]}/${logicParity.total} (${pct}%)`);
}

await writeFile("reports/bill-logic-parity.json", JSON.stringify(logicParity, null, 2));
console.log("\nWrote reports/bill-logic-parity.json");
