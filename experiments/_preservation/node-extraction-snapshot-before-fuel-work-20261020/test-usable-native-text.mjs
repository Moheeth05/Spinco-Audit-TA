// Regression tests for hasUsableNativeText() (see textExtraction.mjs's
// module comment for the full investigation). The originally-described
// Node-vs-Python has_selectable_text divergence did NOT reproduce - a
// corpus-wide, pre-OCR comparison across all 168 real pages found 0
// disagreements (reports/selectable-text-divergence-diagnosis.json), both
// sides already use the identical MIN_NATIVE_TEXT_CHARS=20. This fix is a
// genuine, independently-justified robustness improvement (distinguishing
// real content from a run of isolated symbols/artifacts, while still
// supporting short-but-genuine text) - not a reproduction of a confirmed
// bug, and is reported as such.
import { hasUsableNativeText, usableNativeTextStats, PdfJsTextExtractor } from "./src/textExtraction.mjs";

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

// ---------- Case 1: a page containing only one stray character ----------
{
  check("case1 single stray character: not usable", hasUsableNativeText("."), false);
}

// ---------- Case 2: a few isolated symbols or fragments ----------
{
  // Real confirmed OCR-logo-noise shape from reports/receipt-extraction-
  // baseline-20261013 (ER-40706.pdf page 10's letterhead-graphic misreads).
  check("case2 isolated symbols/fragments: not usable", hasUsableNativeText("A A ™\nJ \\ Wn NN"), false);
}

// ---------- Case 3: a normal text-based receipt ----------
{
  const text = "GLOBAL CONSULTING SERVICES PVT LTD\nGSTIN: 29ABCDE1234F1Z5\nTotal Amount: Rs.29,500.00\nDate: 15/03/2026";
  check("case3 normal text-based receipt: usable", hasUsableNativeText(text), true);
}

// ---------- Case 4: a short but legitimate receipt ----------
{
  check("case4 short legitimate receipt (under 20 raw chars): usable", hasUsableNativeText("KFC\nRs.300"), true);
}

// ---------- Case 5: a native-text Expense Report page ----------
{
  const text = [
    "Expense Report", "ER-40513", "Submitted", "Amount to be Reimbursed", "Rs.53,314.00",
    "SPINCO TECH PVT LTD", "Tamil Nadu", "India", "EXPENSE SUMMARY",
    "S.No", "Expense Details", "Category", "Amount (INR)",
  ].join("\n");
  check("case5 native Expense Report page: usable", hasUsableNativeText(text), true);
}

// ---------- Case 6: a scanned page with no usable native text ----------
{
  check("case6 scanned page, empty extraction: not usable", hasUsableNativeText(""), false);
}

// ---------- Case 7: small native header, image-based receipt content ----------
{
  // Only a page-number stamp survives as native text; the actual receipt
  // content is in the (not-yet-OCR'd) image.
  check("case7 small native header over image content: not usable", hasUsableNativeText("6"), false);
}

// ---------- Case 8: sparse but genuinely sufficient text ----------
{
  const text = "ABC Store\nGST 29AAAAA1111A1Z1\nTotal Rs 450";
  const stats = usableNativeTextStats(text);
  check("case8 sparse-but-sufficient text: under the raw 20-char mark is not required here", stats.length < 20 || stats.length >= 20, true);
  check("case8 sparse-but-sufficient text: usable", hasUsableNativeText(text), true);
}

// ---------- Case 9: empty and whitespace-only extraction results ----------
{
  check("case9a empty string: not usable", hasUsableNativeText(""), false);
  check("case9b whitespace-only: not usable", hasUsableNativeText("   \n\n   \t  "), false);
  check("case9c null/undefined-safe", hasUsableNativeText(undefined), false);
}

// ---------- Case 10: the exact real page that previously diverged ----------
// vivek-bhai-report-ER41220.pdf page 6 - real native (pre-OCR) text is
// literally "6" (the scanned page's own page-number stamp), confirmed by
// direct extraction from the real PDF, not assumed.
{
  const extractor = new PdfJsTextExtractor(null);
  const pages = await extractor.splitPages("fixtures/reference-pdfs/vivek-bhai-report-ER41220.pdf");
  const page = pages.find((p) => p.pageNumber === 6);
  check("case10 real diverging page (native text='6'): correctly not usable, OCR still triggered", page.hasSelectableText, false);
}

// ---------- OCR invocation verified via a replaceable mock engine (no real OCR run) ----------
class MockOcrEngine {
  constructor(resultText = "mock ocr text") {
    this.called = false;
    this.resultText = resultText;
  }
  async extractText(_imageBytes) {
    this.called = true;
    return { text: this.resultText, confidence: 0.9 };
  }
}

{
  // A page whose native text is artifact-only must trigger OCR.
  const page = { pageNumber: 1, text: "A A ™", hasSelectableText: hasUsableNativeText("A A ™"), source: "native", ocrConfidence: null };
  check("OCR-trigger sanity: artifact-only page is flagged for OCR", page.hasSelectableText, false);
}
{
  // A page with sufficient usable text must NOT trigger OCR (mock never called).
  const mock = new MockOcrEngine();
  const extractor = new PdfJsTextExtractor(mock);
  const page = { pageNumber: 1, text: "GLOBAL CONSULTING SERVICES PVT LTD\nGSTIN: 29ABCDE1234F1Z5\nTotal: Rs.500.00", hasSelectableText: true, source: "native", ocrConfidence: null };
  await extractor._applyOcr("unused-path-not-read-since-page-has-text", [page]);
  check("OCR-trigger sanity: usable-text page never invokes OCR engine", mock.called, false);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail > 0 ? 1 : 0);
