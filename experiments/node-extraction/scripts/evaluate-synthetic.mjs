import { execFileSync } from "node:child_process";
import { readdir } from "node:fs/promises";
import { PdfJsTextExtractor } from "../src/textExtraction.mjs";
import { extractHeader, extractExpenseLines } from "../src/expenseReportParser.mjs";

const PYTHON = "D:\\Bills\\backend\\.venv\\Scripts\\python.exe";
const DIR = "fixtures/synthetic";

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
print(json.dumps([{"sNo": r["s_no"], "category": r["category"], "merchant": r["merchant"], "referenceId": r["reference_id"], "claimedAmount": r["claimed_amount"]} for r in rows]))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 20 * 1024 * 1024 }));
}

async function jsEndToEnd(pdfPath) {
  const extractor = new PdfJsTextExtractor(null);
  const pages = await extractor.contentPages(pdfPath);
  const [rows] = extractExpenseLines(pages);
  return rows.map((r) => ({
    sNo: r.sNo,
    category: r.category,
    merchant: r.merchant,
    referenceId: r.referenceId,
    claimedAmount: r.claimedAmount,
  }));
}

const files = (await readdir(DIR)).filter((f) => f.endsWith(".pdf"));
console.log("[SYNTHETIC-ONLY - not proof of unseen real-world layout handling]\n");
for (const file of files) {
  const pdfPath = `${DIR}/${file}`;
  const py = pyEndToEnd(pdfPath);
  const js = await jsEndToEnd(pdfPath);
  console.log(`${file}:`);
  console.log(`  Python: ${JSON.stringify(py)}`);
  console.log(`  JS:     ${JSON.stringify(js)}`);
  console.log(`  Match:  ${JSON.stringify(py) === JSON.stringify(js) ? "YES" : "NO - DIFFERS"}\n`);
}
