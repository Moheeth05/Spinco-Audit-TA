// Re-run of the full real-corpus business-logic parity AFTER BOTH the
// statement-aware amount fix AND the vendor-extraction fix, writing to a
// NEW file (never overwrites the statement-fix-only baseline or the
// original pre-fix baseline).
import { readFile, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
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
    "amount": b.amount, "gstin": b.gstin,
    "categoryGuess": b.category_guess,
}))
`;
  return JSON.parse(execFileSync("D:\\Bills\\backend\\.venv\\Scripts\\python.exe", ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 20 * 1024 * 1024 }));
}

function jsParseBillTextNorm(text) {
  const b = parseBillText(text);
  return {
    vendor: b.vendor, billNumber: b.billNumber,
    billDate: b.billDate ? b.billDate.iso : null,
    amount: b.amount, gstin: b.gstin,
    categoryGuess: b.categoryGuess,
    isMultiTransactionStatement: b.isMultiTransactionStatement,
  };
}

const FIELDS = ["vendor", "billNumber", "billDate", "amount", "gstin", "categoryGuess"];
const result = { total: 0, fieldMatches: Object.fromEntries(FIELDS.map((f) => [f, 0])), vendorMismatches: [] };

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
    result.total++;
    for (const f of FIELDS) {
      if (py[f] === js[f]) result.fieldMatches[f]++;
      else if (f === "vendor") {
        result.vendorMismatches.push({ filename: report.filename, page: p.pageNumber, python: py.vendor, js: js.vendor, nodeDetectedStatement: js.isMultiTransactionStatement });
      }
    }
  }
  console.log(`[logic parity, post-vendor-fix] ${report.filename}: processed bill pages ${startPage}-${report.pages.length}`);
}

console.log("\n=== Business-logic parity - AFTER statement fix + vendor fix ===");
console.log(`Pages evaluated: ${result.total}`);
for (const f of FIELDS) {
  const pct = result.total ? ((result.fieldMatches[f] / result.total) * 100).toFixed(1) : "n/a";
  console.log(`  ${f}: ${result.fieldMatches[f]}/${result.total} (${pct}%)`);
}
console.log(`\nVendor mismatches (Node now intentionally diverges from Python on these): ${result.vendorMismatches.length}`);
for (const m of result.vendorMismatches) {
  console.log(`  ${m.filename} page ${m.page}: python=${JSON.stringify(m.python)} js=${JSON.stringify(m.js)} statementDetected=${m.nodeDetectedStatement}`);
}

await writeFile("reports/bill-logic-parity-after-vendor-fix.json", JSON.stringify(result, null, 2));
console.log("\nWrote reports/bill-logic-parity-after-vendor-fix.json");
