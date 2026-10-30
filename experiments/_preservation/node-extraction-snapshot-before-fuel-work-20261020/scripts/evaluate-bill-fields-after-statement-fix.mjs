// Re-run of evaluate-bill-fields.mjs AFTER the statement-aware amount fix,
// writing to a NEW report file (never overwrites reports/bill-logic-
// parity.json, the pre-fix baseline) so before/after can be compared
// directly. Adds isMultiTransactionStatement to each mismatch record, so
// an EXPECTED divergence (Node correctly refusing to extract a statement
// balance Python still does) is distinguishable at a glance from an
// unexpected one.
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
    isMultiTransactionStatement: b.isMultiTransactionStatement,
  };
}

const FIELDS = ["vendor", "billNumber", "billDate", "amount", "gstin", "categoryGuess"];
const logicParity = { total: 0, fieldMatches: Object.fromEntries(FIELDS.map((f) => [f, 0])), mismatches: [], statementPagesDetected: [] };

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
    if (js.isMultiTransactionStatement) {
      logicParity.statementPagesDetected.push({ filename: report.filename, page: p.pageNumber, pythonAmount: py.amount, nodeAmount: js.amount });
    }
    for (const f of FIELDS) {
      if (py[f] === js[f]) logicParity.fieldMatches[f]++;
      else {
        logicParity.mismatches.push({
          filename: report.filename, page: p.pageNumber, field: f, python: py[f], js: js[f],
          nodeDetectedStatement: js.isMultiTransactionStatement,
        });
      }
    }
  }
  console.log(`[logic parity, post-fix] ${report.filename}: processed bill pages ${startPage}-${report.pages.length}`);
}

console.log("\n=== Layer (a): business-logic parity (same text both sides) - AFTER statement fix ===");
console.log(`Pages evaluated: ${logicParity.total}`);
for (const f of FIELDS) {
  const pct = logicParity.total ? ((logicParity.fieldMatches[f] / logicParity.total) * 100).toFixed(1) : "n/a";
  console.log(`  ${f}: ${logicParity.fieldMatches[f]}/${logicParity.total} (${pct}%)`);
}
console.log(`\nPages Node detected as multi-transaction statements: ${logicParity.statementPagesDetected.length}`);
for (const s of logicParity.statementPagesDetected) {
  console.log(`  ${s.filename} page ${s.page}: Python amount=${s.pythonAmount}, Node amount=${s.nodeAmount}`);
}
const unexpectedAmountMismatches = logicParity.mismatches.filter((m) => m.field === "amount" && !m.nodeDetectedStatement);
console.log(`\nAmount mismatches NOT explained by statement detection (should be 0, i.e. no new unrelated regressions): ${unexpectedAmountMismatches.length}`);
for (const m of unexpectedAmountMismatches) {
  console.log(`  ${m.filename} page ${m.page}: python=${m.python} js=${m.js}`);
}

await writeFile("reports/bill-logic-parity-after-statement-fix.json", JSON.stringify(logicParity, null, 2));
console.log("\nWrote reports/bill-logic-parity-after-statement-fix.json");
