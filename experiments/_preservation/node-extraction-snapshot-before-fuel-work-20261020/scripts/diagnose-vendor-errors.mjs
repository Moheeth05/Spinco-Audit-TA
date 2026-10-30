import { PdfJsTextExtractor } from "../src/textExtraction.mjs";
import { TesseractJsOCREngine } from "../src/ocrEngine.mjs";

const targets = [
  { filename: "vivek-bhai-report-ER41220.pdf", page: 25 },
  { filename: "ER-40706.pdf", page: 6 },
  { filename: "ER-40706.pdf", page: 10 },
  { filename: "ER-40706.pdf", page: 12 },
  { filename: "ER-40782.pdf", page: 3 },
];

for (const { filename, page: pageNum } of targets) {
  const pdfPath = `fixtures/reference-pdfs/${filename}`;
  const ocr = new TesseractJsOCREngine();
  const extractor = new PdfJsTextExtractor(ocr);
  const pages = await extractor.splitPages(pdfPath);
  const page = pages.find((p) => p.pageNumber === pageNum);
  await extractor._applyOcr(pdfPath, [page]);
  console.log(`\n=== ${filename} page ${pageNum} (source=${page.source}) ===`);
  console.log(page.text);
  await ocr.terminate();
}
