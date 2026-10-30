// Runs the Node/pdfjs-dist extraction pipeline against every bill page of
// the 5 real reference Expense Report PDFs already used throughout this
// project as test fixtures, and compares the result field-by-field against
// the already-validated Python/production output (recorded earlier this
// session in review_required_data_AFTER_REVERT.json).
//
// This is a read-only comparison harness - it never writes to the
// production database, never modifies the PDFs, and nothing here is wired
// into the backend or Electron app. See README.md.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { getPageCount, linearText, positionedText } from "./src/pdfExtract.mjs";
import { parseBillText } from "./src/billParser.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const REPO_ROOT = "D:\\Bills";
const PDF_PATHS = {
  "ER-39832.pdf": path.join(REPO_ROOT, "ER-39832.pdf"),
  "ER-40223.pdf": path.join(REPO_ROOT, "ER-40223.pdf"),
  "ER-40706 (1).pdf": path.join(REPO_ROOT, "ER-40706 (1).pdf"),
  "ER-40782.pdf": path.join(REPO_ROOT, "ER-40782.pdf"),
  "c967267ef0f4_ER-41115.pdf": path.join(
    REPO_ROOT,
    "backups",
    "dipak-cleanup-20260924-170758",
    "documents",
    "documents",
    "expense_reports",
    "c967267ef0f4_ER-41115.pdf"
  ),
};

const PYTHON_RESULTS_PATH = path.join(
  "C:\\Users\\RAJA\\AppData\\Local\\Temp\\claude\\d--Bills\\da4adec8-0df2-494a-8fa4-fa096ab578da\\scratchpad",
  "review_required_data_AFTER_REVERT.json"
);

function fmtDate(d) {
  return d ? d.iso : null;
}

async function main() {
  const pythonData = JSON.parse(fs.readFileSync(PYTHON_RESULTS_PATH, "utf-8"));
  const rows = [];
  let nativeTextPages = 0;
  let scannedOrEmptyPages = 0;
  let amountAgree = 0, amountDisagree = 0, amountBothNull = 0;
  let dateAgree = 0, dateDisagree = 0, dateBothNull = 0;
  let categoryAgree = 0, categoryDisagree = 0, categoryBothNull = 0;

  for (const report of pythonData.reports) {
    const pdfPath = PDF_PATHS[report.file];
    if (!pdfPath || !fs.existsSync(pdfPath)) {
      console.log(`SKIP ${report.file}: PDF not found at expected path`);
      continue;
    }

    for (const bill of report.bills) {
      const pageNum = bill.page_number;
      let linear, positioned;
      try {
        linear = await linearText(pdfPath, pageNum);
        positioned = await positionedText(pdfPath, pageNum);
      } catch (err) {
        rows.push({ file: report.file, page: pageNum, error: String(err) });
        continue;
      }

      const hasNativeText = linear.replace(/\s/g, "").length >= 20;
      if (hasNativeText) nativeTextPages++;
      else scannedOrEmptyPages++;

      // Prefer the positioned reconstruction when the plain linear text
      // doesn't already look field-bearing - same two-tier strategy as the
      // production Python path (see text_extraction.py's
      // _apply_position_aware_fallback), re-implemented independently here
      // rather than imported, since this harness is deliberately standalone.
      const linearParsed = parseBillText(linear);
      const positionedParsed = parseBillText(positioned);
      const nodeAmount = linearParsed.amount !== null ? linearParsed.amount : positionedParsed.amount;
      const nodeDate = linearParsed.billDate !== null ? linearParsed.billDate : positionedParsed.billDate;
      const nodeCategory = linearParsed.category !== null ? linearParsed.category : positionedParsed.category;

      const pyAmount = bill.amount;
      const pyDate = bill.bill_date;
      const pyCategory = bill.category_guess;

      const nodeDateIso = fmtDate(nodeDate);

      let amountVerdict;
      if (pyAmount === null && nodeAmount === null) { amountVerdict = "both-null"; amountBothNull++; }
      else if (pyAmount !== null && nodeAmount !== null && Math.abs(pyAmount - nodeAmount) < 0.01) { amountVerdict = "agree"; amountAgree++; }
      else { amountVerdict = "DISAGREE"; amountDisagree++; }

      let dateVerdict;
      if (!pyDate && !nodeDateIso) { dateVerdict = "both-null"; dateBothNull++; }
      else if (pyDate === nodeDateIso) { dateVerdict = "agree"; dateAgree++; }
      else { dateVerdict = "DISAGREE"; dateDisagree++; }

      let categoryVerdict;
      if (!pyCategory && !nodeCategory) { categoryVerdict = "both-null"; categoryBothNull++; }
      else if (pyCategory === nodeCategory) { categoryVerdict = "agree"; categoryAgree++; }
      else { categoryVerdict = "DISAGREE"; categoryVerdict; categoryDisagree++; }

      rows.push({
        file: report.file,
        page: pageNum,
        hasNativeText,
        pyAmount, nodeAmount, amountVerdict,
        pyDate, nodeDate: nodeDateIso, dateVerdict,
        pyCategory, nodeCategory, categoryVerdict,
      });
    }
  }

  console.log("=== Disagreements (the only rows worth reading closely) ===");
  for (const r of rows) {
    if (r.error) {
      console.log(`${r.file} p${r.page}: ERROR ${r.error}`);
      continue;
    }
    if (r.amountVerdict === "DISAGREE" || r.dateVerdict === "DISAGREE" || r.categoryVerdict === "DISAGREE") {
      console.log(
        `${r.file} p${r.page} [native=${r.hasNativeText}] ` +
        `amount py=${r.pyAmount} node=${r.nodeAmount} (${r.amountVerdict}) | ` +
        `date py=${r.pyDate} node=${r.nodeDate} (${r.dateVerdict}) | ` +
        `category py=${r.pyCategory} node=${r.nodeCategory} (${r.categoryVerdict})`
      );
    }
  }

  console.log("\n=== Summary ===");
  console.log(`Total bill pages checked: ${rows.length}`);
  console.log(`Pages with native (non-scanned) text: ${nativeTextPages}`);
  console.log(`Pages with no/thin native text (scanned - Node has no OCR here): ${scannedOrEmptyPages}`);
  console.log(`Amount:   agree=${amountAgree} both-null=${amountBothNull} DISAGREE=${amountDisagree}`);
  console.log(`Date:     agree=${dateAgree} both-null=${dateBothNull} DISAGREE=${dateDisagree}`);
  console.log(`Category: agree=${categoryAgree} both-null=${categoryBothNull} DISAGREE=${categoryDisagree}`);

  fs.writeFileSync(path.join(__dirname, "corpus-comparison-results.json"), JSON.stringify(rows, null, 2));
  console.log("\nFull per-page results written to corpus-comparison-results.json");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
