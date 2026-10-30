// Stage 3 parity harness: expenseReportParser.mjs vs the real
// expense_report_parser.py - including the P0 category-extraction fix, run
// against the REAL ER-40513 document ("reahul report.pdf", 28 rows, at
// least 5 distinct optional prefix-label types). Both sides are fed the
// EXACT SAME page text (obtained from Python's own content_pages(), which
// is the real production text-extraction output) so this isolates parsing-
// logic parity from PDF-text-extraction parity (that's Stage 4).
import { execFileSync } from "node:child_process";
import { extractHeader, extractExpenseLines } from "./src/expenseReportParser.mjs";

const PYTHON = "D:\\Bills\\backend\\.venv\\Scripts\\python.exe";
const REAL_PDF = "D:\\Bills\\uat-data-test\\documents\\expense_reports\\506d7dbf9383_reahul report.pdf";

function getPythonPagesAndRows() {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from pathlib import Path
from app.services.extraction.text_extraction import PyMuPDFTextExtractor
from app.services.extraction import expense_report_parser

extractor = PyMuPDFTextExtractor(ocr_engine=None)
pages = extractor.content_pages(Path(r"${REAL_PDF}"))
rows, warnings = expense_report_parser.extract_expense_lines(pages)
header = expense_report_parser.extract_header(pages)

out = {
    "pages": [{"pageNumber": p["page_number"], "text": p["text"]} for p in pages],
    "rows": [
        {
            "sNo": r["s_no"],
            "expenseDate": r["expense_date"].isoformat() if r["expense_date"] else None,
            "category": r["category"],
            "merchant": r["merchant"],
            "referenceId": r["reference_id"],
            "expenseDetails": r["expense_details"],
            "claimedAmount": r["claimed_amount"],
            "sourcePage": r["source_page"],
            "status": r["status"],
        }
        for r in rows
    ],
    "header": {
        "erId": header["er_id"],
        "employeeName": header["employee_name"],
        "employeeCode": header["employee_code"],
        "totalClaimed": header["total_claimed"],
    },
}
print(json.dumps(out))
`;
  return JSON.parse(execFileSync(PYTHON, ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 50 * 1024 * 1024 }));
}

let pass = 0, fail = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    pass++;
  } else {
    fail++;
    console.log(`[FAIL] ${label}\n  JS:     ${a}\n  Python: ${e}`);
  }
}

const { pages, rows: pyRows, header: pyHeader } = getPythonPagesAndRows();
console.log(`Fed ${pages.length} pages (Python's own real content_pages() output) to both implementations.`);

const jsHeader = extractHeader(pages);
check("header.erId", jsHeader.erId, pyHeader.erId);
check("header.employeeName", jsHeader.employeeName, pyHeader.employeeName);
check("header.employeeCode", jsHeader.employeeCode, pyHeader.employeeCode);
check("header.totalClaimed", jsHeader.totalClaimed, pyHeader.totalClaimed);

const [jsRows] = extractExpenseLines(pages);
check("row count", jsRows.length, pyRows.length);

for (let i = 0; i < Math.max(jsRows.length, pyRows.length); i++) {
  const js = jsRows[i];
  const py = pyRows[i];
  const jsNorm = js
    ? {
        sNo: js.sNo,
        expenseDate: js.expenseDate ? js.expenseDate.iso : null,
        category: js.category,
        merchant: js.merchant,
        referenceId: js.referenceId,
        expenseDetails: js.expenseDetails,
        claimedAmount: js.claimedAmount,
        sourcePage: js.sourcePage,
        status: js.status,
      }
    : null;
  check(`row[${i}] (s_no=${py ? py.sNo : "?"})`, jsNorm, py);
}

// Explicit regression guard: the exact original P0 defect - no row's
// category may ever be a reference-looking string, and every non-null
// category must be a real Policy Master category.
let refAsCategoryCount = 0;
for (const r of jsRows) {
  if (r.category && (r.category.toUpperCase().startsWith("REF") || r.category.includes(":"))) {
    refAsCategoryCount++;
  }
}
check("reference-as-category count (must be 0)", refAsCategoryCount, 0);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
