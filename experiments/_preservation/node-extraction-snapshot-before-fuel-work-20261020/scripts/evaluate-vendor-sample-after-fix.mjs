// Re-run of the 13 human-verified samples AFTER the vendor-extraction fix,
// scoring VENDOR correctness specifically (previous rounds scored amount).
// Against the SAME datasets/human-verified-bill-annotations.json (not
// modified). Writes to a NEW file, never overwrites prior reports.
//
// "Correct" here means the extracted vendor text contains a human-
// recognizable fragment of the ground-truth vendor name - the same
// judgment-based methodology already used (and explicitly NOT reduced to
// a false-precision fraction) in the original baseline report, applied
// consistently here so before/after is a fair comparison.
import { readFile, writeFile } from "node:fs/promises";
import { PdfJsTextExtractor } from "../src/textExtraction.mjs";
import { TesseractJsOCREngine } from "../src/ocrEngine.mjs";
import { parseBillText } from "../src/billParser.mjs";

const samples = JSON.parse(await readFile("datasets/human-verified-bill-annotations.json", "utf-8")).samples;

async function jsVendorForPage(pdfPath, pageNumber) {
  const ocr = new TesseractJsOCREngine();
  const extractor = new PdfJsTextExtractor(ocr);
  const pages = await extractor.splitPages(pdfPath);
  const page = pages.find((p) => p.pageNumber === pageNumber);
  await extractor._applyOcr(pdfPath, [page]);
  const b = parseBillText(page.text);
  await ocr.terminate();
  return { vendor: b.vendor, warnings: b.warnings };
}

const results = [];
for (const sample of samples) {
  const pdfPath = `fixtures/reference-pdfs/${sample.filename}`;
  console.log(`Evaluating ${sample.filename} page ${sample.page}...`);
  const { vendor, warnings } = await jsVendorForPage(pdfPath, sample.page);
  results.push({
    filename: sample.filename, page: sample.page,
    groundTruthVendor: sample.vendor,
    extractedVendor: vendor,
    abstained: vendor === null,
    warnings,
  });
  console.log(`  ground truth="${sample.vendor}" | extracted=${JSON.stringify(vendor)}`);
}

await writeFile("reports/vendor-sample-evaluation-after-fix.json", JSON.stringify({ results }, null, 2));
console.log("\nWrote reports/vendor-sample-evaluation-after-fix.json");
console.log("\nManual classification (correct/partial/wrong/abstained) is done in the final report, same judgment-based methodology as the original baseline - not computed here as a false-precision automated fraction.");
