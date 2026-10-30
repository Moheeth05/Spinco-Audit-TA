// Re-run of evaluate-ocr-sample.mjs AFTER the statement-aware amount fix,
// against the SAME datasets/human-verified-bill-annotations.json (not
// modified), writing to a NEW report file (never overwrites reports/
// ocr-sample-evaluation.json, the pre-fix baseline) so before/after can be
// compared directly.
import { readFile, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { PdfJsTextExtractor } from "../src/textExtraction.mjs";
import { TesseractJsOCREngine } from "../src/ocrEngine.mjs";
import { parseBillText } from "../src/billParser.mjs";

const samples = JSON.parse(await readFile("datasets/human-verified-bill-annotations.json", "utf-8")).samples;

function pyPageAndParse(pdfPath, pageNumber) {
  const script = `
import sys, json, time
sys.path.insert(0, r"D:\\Bills\\backend")
from pathlib import Path
from app.services.extraction.text_extraction import PyMuPDFTextExtractor
from app.services.extraction.ocr_engine import TesseractOCREngine
from app.services.extraction import bill_parser

extractor = PyMuPDFTextExtractor(ocr_engine=TesseractOCREngine())
pages = extractor.split_pages(Path(r"${pdfPath}"))
page = next(p for p in pages if p["page_number"] == ${pageNumber})
native_has_text = page["has_selectable_text"]
extractor._apply_ocr(Path(r"${pdfPath}"), [page])
b = bill_parser.parse_bill_text(page["text"])
print(json.dumps({
    "nativeHasSelectableText": native_has_text,
    "finalSource": page["source"],
    "amount": b.amount,
}))
`;
  return JSON.parse(execFileSync("D:\\Bills\\backend\\.venv\\Scripts\\python.exe", ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 20 * 1024 * 1024 }));
}

async function jsPageAndParse(pdfPath, pageNumber) {
  const ocr = new TesseractJsOCREngine();
  const extractor = new PdfJsTextExtractor(ocr);
  const pages = await extractor.splitPages(pdfPath);
  const page = pages.find((p) => p.pageNumber === pageNumber);
  const nativeHasText = page.hasSelectableText;
  await extractor._applyOcr(pdfPath, [page]);
  const b = parseBillText(page.text);
  await ocr.terminate();
  return {
    nativeHasSelectableText: nativeHasText,
    finalSource: page.source,
    amount: b.amount,
    isMultiTransactionStatement: b.isMultiTransactionStatement,
  };
}

// Documented correction (see reports/receipt-extraction-baseline-20261013.json
// "groundTruthCorrection") - the stored annotations file is NOT modified;
// this is applied only when interpreting results, exactly as in the prior
// diagnostic report.
function correctedGroundTruthAmount(sample) {
  if (sample.filename === "ER-40706.pdf" && sample.page === 22) return null; // FASTag statement, not KFC
  return sample.totalAmount;
}

const results = [];
for (const sample of samples) {
  const pdfPath = `fixtures/reference-pdfs/${sample.filename}`;
  console.log(`Evaluating ${sample.filename} page ${sample.page}...`);
  const py = pyPageAndParse(pdfPath, sample.page);
  const js = await jsPageAndParse(pdfPath, sample.page);
  const gt = correctedGroundTruthAmount(sample);

  const amountCorrectJs = gt === null ? js.amount === null : js.amount === gt;
  const amountCorrectPy = gt === null ? py.amount === null : py.amount === gt;

  results.push({
    filename: sample.filename, page: sample.page,
    groundTruthAmount: gt, groundTruthCorrected: gt !== sample.totalAmount,
    python: py, node: js,
    amountCorrectNode: amountCorrectJs, amountCorrectPython: amountCorrectPy,
  });
  console.log(`  ground truth=${gt} | Python=${py.amount} (${amountCorrectPy ? "OK" : "WRONG"}) | Node=${js.amount} (${amountCorrectJs ? "OK" : "WRONG"}, statementDetected=${js.isMultiTransactionStatement})`);
}

const summary = {
  total: results.length,
  nodeAmountCorrect: results.filter((r) => r.amountCorrectNode).length,
  pythonAmountCorrect: results.filter((r) => r.amountCorrectPython).length,
};
console.log(`\n=== Summary (after statement fix) ===`);
console.log(`Node amount correct: ${summary.nodeAmountCorrect}/${summary.total}`);
console.log(`Python amount correct: ${summary.pythonAmountCorrect}/${summary.total}`);

await writeFile("reports/ocr-sample-evaluation-after-statement-fix.json", JSON.stringify({ summary, results }, null, 2));
console.log("\nWrote reports/ocr-sample-evaluation-after-statement-fix.json");
