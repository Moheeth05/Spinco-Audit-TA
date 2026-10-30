// Section 6: OCR evaluated separately, against real human-verified ground
// truth (datasets/human-verified-bill-annotations.json) - not just Node-
// vs-Python agreement, which could both be wrong the same way. Runs the
// FULL end-to-end Node pipeline (PdfJsTextExtractor with the real
// tesseract.js OCR engine, exactly as a real Node deployment would) on
// each sample page, and the FULL Python pipeline (PyMuPDFTextExtractor
// with the real local Tesseract via pytesseract) on the same page, then
// compares both against the human-read ground truth.
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
t0 = time.time()
extractor._apply_ocr(Path(r"${pdfPath}"), [page])
ocr_ms = (time.time() - t0) * 1000
b = bill_parser.parse_bill_text(page["text"])
print(json.dumps({
    "nativeHasSelectableText": native_has_text,
    "finalSource": page["source"],
    "ocrConfidence": page.get("ocr_confidence"),
    "ocrMs": ocr_ms if not native_has_text else None,
    "textLength": len(page["text"].strip()),
    "vendor": b.vendor, "billNumber": b.bill_number,
    "billDate": b.bill_date.isoformat() if b.bill_date else None,
    "amount": b.amount, "gstin": b.gstin,
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
  const t0 = Date.now();
  await extractor._applyOcr(pdfPath, [page]);
  const ocrMs = Date.now() - t0;
  const b = parseBillText(page.text);
  await ocr.terminate();
  return {
    nativeHasSelectableText: nativeHasText,
    finalSource: page.source,
    ocrConfidence: page.ocrConfidence,
    ocrMs: nativeHasText ? null : ocrMs,
    textLength: page.text.trim().length,
    vendor: b.vendor, billNumber: b.billNumber,
    billDate: b.billDate ? b.billDate.iso : null,
    amount: b.amount, gstin: b.gstin,
  };
}

const results = [];
for (const sample of samples) {
  const pdfPath = `fixtures/reference-pdfs/${sample.filename}`;
  console.log(`Evaluating ${sample.filename} page ${sample.page}...`);
  let py, js;
  try {
    py = pyPageAndParse(pdfPath, sample.page);
  } catch (e) {
    py = { error: String(e.message || e).slice(0, 300) };
  }
  try {
    js = await jsPageAndParse(pdfPath, sample.page);
  } catch (e) {
    js = { error: String(e.message || e).slice(0, 300) };
  }

  const amountCorrectJs = sample.totalAmount === null ? js.amount === null : js.amount === sample.totalAmount;
  const amountCorrectPy = sample.totalAmount === null ? py.amount === null : py.amount === sample.totalAmount;

  results.push({
    filename: sample.filename,
    page: sample.page,
    groundTruth: { vendor: sample.vendor, totalAmount: sample.totalAmount, invoiceNumber: sample.invoiceNumber, quality: sample.quality, confidence: sample.confidence, note: sample.note },
    python: py,
    node: js,
    amountCorrectNode: amountCorrectJs,
    amountCorrectPython: amountCorrectPy,
  });

  console.log(`  ground truth amount=${sample.totalAmount} | Python amount=${py.amount} (${amountCorrectPy ? "OK" : "WRONG"}, ocr=${py.finalSource}) | Node amount=${js.amount} (${amountCorrectJs ? "OK" : "WRONG"}, ocr=${js.finalSource})`);
}

const summary = {
  total: results.length,
  nodeAmountCorrect: results.filter((r) => r.amountCorrectNode).length,
  pythonAmountCorrect: results.filter((r) => r.amountCorrectPython).length,
  ocrInvokedNode: results.filter((r) => r.node.finalSource === "ocr").length,
  ocrInvokedPython: results.filter((r) => r.python.finalSource === "ocr").length,
};
console.log(`\n=== Summary ===`);
console.log(`Node amount correct: ${summary.nodeAmountCorrect}/${summary.total}`);
console.log(`Python amount correct: ${summary.pythonAmountCorrect}/${summary.total}`);
console.log(`OCR actually invoked - Node: ${summary.ocrInvokedNode}/${summary.total}, Python: ${summary.ocrInvokedPython}/${summary.total}`);

await writeFile("reports/ocr-sample-evaluation.json", JSON.stringify({ summary, results }, null, 2));
console.log("\nWrote reports/ocr-sample-evaluation.json");
