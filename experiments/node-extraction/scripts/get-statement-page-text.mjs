// Diagnostic helper: dumps the FULL Node-OCR'd text for the two confirmed
// statement-misidentification failure pages, so the fix is designed
// against real text, not assumed patterns.
import { PdfJsTextExtractor } from "../src/textExtraction.mjs";
import { TesseractJsOCREngine } from "../src/ocrEngine.mjs";

const pdfPath = "fixtures/reference-pdfs/ER-40706.pdf";
const ocr = new TesseractJsOCREngine();
const extractor = new PdfJsTextExtractor(ocr);
const pages = await extractor.splitPages(pdfPath);

for (const pageNum of [20, 22]) {
  const page = pages.find((p) => p.pageNumber === pageNum);
  await extractor._applyOcr(pdfPath, [page]);
  console.log(`\n=== Page ${pageNum} (source=${page.source}) ===`);
  console.log(page.text);
}
await ocr.terminate();
