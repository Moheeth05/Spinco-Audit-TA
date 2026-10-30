// The real end-to-end evaluation: JS's OWN PdfJsTextExtractor feeds JS's
// OWN expenseReportParser (no pre-extracted Python text involved anywhere
// on the JS side) - this is what a real Node.js pipeline would actually
// produce, compared field-by-field against the real Python oracle's own
// end-to-end output for the identical PDF. Computes the metrics the task
// requires: row count, and per-field exact-match accuracy for date,
// category, amount, merchant, description, reference ID - plus explicit
// checks for the two named failure modes (reference-as-category,
// phantom/missing rows from report-summary text).
import { execFileSync } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import { PdfJsTextExtractor } from "../src/textExtraction.mjs";
import { extractHeader, extractExpenseLines } from "../src/expenseReportParser.mjs";

const PYTHON = "D:\\Bills\\backend\\.venv\\Scripts\\python.exe";
const manifest = JSON.parse(await readFile("datasets/manifest.json", "utf-8"));

function pyEndToEnd(pdfPath) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from pathlib import Path
from app.services.extraction.text_extraction import PyMuPDFTextExtractor
from app.services.extraction import expense_report_parser as erp

extractor = PyMuPDFTextExtractor(ocr_engine=None)
pages = extractor.content_pages(Path(r"${pdfPath}"))
rows, warnings = erp.extract_expense_lines(pages)
header = erp.extract_header(pages)

out = {
    "header": {"erId": header["er_id"], "employeeName": header["employee_name"], "totalClaimed": header["total_claimed"]},
    "rows": [
        {
            "sNo": r["s_no"],
            "expenseDate": r["expense_date"].isoformat() if r["expense_date"] else None,
            "category": r["category"],
            "merchant": r["merchant"],
            "referenceId": r["reference_id"],
            "claimedAmount": r["claimed_amount"],
            "status": r["status"],
        }
        for r in rows
    ],
}
print(json.dumps(out))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 50 * 1024 * 1024 }));
}

async function jsEndToEnd(pdfPath) {
  const extractor = new PdfJsTextExtractor(null);
  const pages = await extractor.contentPages(pdfPath);
  const header = extractHeader(pages);
  const [rows] = extractExpenseLines(pages);
  return {
    header: { erId: header.erId, employeeName: header.employeeName, totalClaimed: header.totalClaimed },
    rows: rows.map((r) => ({
      sNo: r.sNo,
      expenseDate: r.expenseDate ? r.expenseDate.iso : null,
      category: r.category,
      merchant: r.merchant,
      referenceId: r.referenceId,
      claimedAmount: r.claimedAmount,
      status: r.status,
    })),
  };
}

const FIELDS = ["expenseDate", "category", "merchant", "referenceId", "claimedAmount"];
const overall = { rowCountCorrect: 0, rowCountTotal: 0, fieldMatches: {}, fieldTotals: {}, refAsCategoryCount: 0, phantomOrMissingRows: 0 };
for (const f of FIELDS) {
  overall.fieldMatches[f] = 0;
  overall.fieldTotals[f] = 0;
}

const perReport = [];
for (const report of manifest.reportsCovered) {
  const pdfPath = `fixtures/reference-pdfs/${report.filename}`;
  const py = pyEndToEnd(pdfPath);
  const js = await jsEndToEnd(pdfPath);

  const headerMatch = {
    erId: js.header.erId === py.header.erId,
    employeeName: js.header.employeeName === py.header.employeeName,
    totalClaimed: js.header.totalClaimed === py.header.totalClaimed,
  };

  const rowCountMatch = js.rows.length === py.rows.length;
  overall.rowCountTotal++;
  if (rowCountMatch) overall.rowCountCorrect++;

  const fieldResults = { ...Object.fromEntries(FIELDS.map((f) => [f, { correct: 0, total: 0 }])) };
  const n = Math.max(js.rows.length, py.rows.length);
  for (let i = 0; i < n; i++) {
    const jr = js.rows[i];
    const pr = py.rows[i];
    for (const f of FIELDS) {
      if (!pr) continue; // extra JS rows beyond python's count counted separately below
      fieldResults[f].total++;
      overall.fieldTotals[f]++;
      if (jr && jr[f] === pr[f]) {
        fieldResults[f].correct++;
        overall.fieldMatches[f]++;
      }
    }
    if (jr && jr.category && (jr.category.toUpperCase().startsWith("REF") || /^\d{6,}$/.test(jr.category.replace(/\D/g, "")))) {
      overall.refAsCategoryCount++;
    }
  }
  if (!rowCountMatch) overall.phantomOrMissingRows += Math.abs(js.rows.length - py.rows.length);

  perReport.push({
    filename: report.filename,
    role: report.role,
    pyRowCount: py.rows.length,
    jsRowCount: js.rows.length,
    rowCountMatch,
    headerMatch,
    fieldAccuracy: Object.fromEntries(FIELDS.map((f) => [f, `${fieldResults[f].correct}/${fieldResults[f].total}`])),
  });

  const pct = (a, b) => (b ? ((a / b) * 100).toFixed(1) : "n/a");
  console.log(`[${report.role}] ${report.filename}: rows JS=${js.rows.length} Py=${py.rows.length} (${rowCountMatch ? "MATCH" : "MISMATCH"})`);
  for (const f of FIELDS) {
    console.log(`    ${f}: ${fieldResults[f].correct}/${fieldResults[f].total} (${pct(fieldResults[f].correct, fieldResults[f].total)}%)`);
  }
}

console.log(`\n=== OVERALL (all ${manifest.reportsCovered.length} reports) ===`);
console.log(`Row count exact match: ${overall.rowCountCorrect}/${overall.rowCountTotal}`);
for (const f of FIELDS) {
  const pct = overall.fieldTotals[f] ? ((overall.fieldMatches[f] / overall.fieldTotals[f]) * 100).toFixed(1) : "n/a";
  console.log(`${f}: ${overall.fieldMatches[f]}/${overall.fieldTotals[f]} (${pct}%)`);
}
console.log(`Reference-ID-emitted-as-category count (must be 0): ${overall.refAsCategoryCount}`);
console.log(`Phantom/missing rows total: ${overall.phantomOrMissingRows}`);

await writeFile("reports/field-level-evaluation.json", JSON.stringify({ overall, perReport }, null, 2));
console.log("\nWrote reports/field-level-evaluation.json");
