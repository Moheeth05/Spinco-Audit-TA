// Regression/lock-in test for the diagnosed vivek-bhai-report-ER41220.pdf
// page 6 OCR divergence (see reports/ocr-diagnostic-page6/ and this
// task's final report for the full cross-test evidence). Node's current
// rendering pipeline is already correct for this page and needs no
// change - this test exists to CATCH A FUTURE REGRESSION if
// textExtraction.mjs's rendering code ever changes (e.g. DPI, canvas
// settings, image smoothing) in a way that degrades OCR legibility for
// this real, previously-problematic page.
//
// Root cause (confirmed via a controlled cross-test, not assumed): the
// embedded image on this page is an 863x1435 JPEG at ~96 effective DPI,
// requiring ~1.57x upscaling to fill the page at the pipeline's 200 DPI
// render target. At that SAME target pixel size (1654x2339, confirmed
// identical for both renderers), PyMuPDF's render is measurably harder
// for BOTH OCR engines to read than pdfjs-dist+canvas's render of the
// IDENTICAL source image - pytesseract itself (the exact same Tesseract
// 5.5.3 binary) returns 328 recognizable characters on Node's render and
// 0 characters on Python's own render of the same page. A separate,
// independent experiment (varying only DPI through Python's own pipeline)
// found Python's own rendering ALSO succeeds once pushed to 300+ DPI -
// so this is a render-sharpness/scale issue, not an OCR-engine-capability
// one. See the final report for the full DPI matrix and both root causes.
import { PdfJsTextExtractor } from "./src/textExtraction.mjs";
import { TesseractJsOCREngine } from "./src/ocrEngine.mjs";

let pass = 0, fail = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) pass++;
  else {
    fail++;
    console.log(`[FAIL] ${label}\n  actual:   ${a}\n  expected: ${e}`);
  }
}

const PDF = "fixtures/reference-pdfs/vivek-bhai-report-ER41220.pdf";
const PAGE = 6;

const ocr = new TesseractJsOCREngine();
const extractor = new PdfJsTextExtractor(ocr);
const pages = await extractor.splitPages(PDF);
const page = pages.find((p) => p.pageNumber === PAGE);

check("page 6 native text is not usable (confirmed real page-number stamp only)", page.hasSelectableText, false);

await extractor._applyOcr(PDF, [page]);

check("page 6: OCR was invoked and succeeded (source=ocr)", page.source, "ocr");
check("page 6: OCR recovered a meaningful amount of text (previously 0 via Python's own render at the same DPI)", page.text.trim().length > 100, true);
// Specific, recognizable content from the real receipt (HP Auto Care
// Centre, Bodakdev) - not an exact string match, since OCR output can
// vary slightly between runs/engine versions, but the key content must
// still be present.
check("page 6: recovers the vendor location 'BODAKDEV'", page.text.toUpperCase().includes("BODAKDEV"), true);
check("page 6: recovers the amount '2500.00'", page.text.includes("2500.00"), true);

await ocr.terminate();

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
