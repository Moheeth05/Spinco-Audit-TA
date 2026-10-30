// Section 3 of the receipt-extraction diagnostic task: a full page-by-page
// census of all 6 reference PDFs, using ONLY the deterministic signals
// already in the Python pipeline (has_selectable_text/source, bill_parser's
// has_bill_evidence/parsed_has_evidence, GSTIN presence, and the
// EXPENSE SUMMARY / REPORT SUMMARY / TRIP SUMMARY section markers already
// used by expense_report_parser.py) - never the opt-in NVIDIA page
// classifier (document_classifier.py), which is disabled anyway (no
// SPINCO_NVIDIA_API_KEY configured) and would mean sending page text to an
// external service, which this task's safety rules forbid. OCR is run
// (ocr_engine=TesseractOCREngine, a local engine, not external) so the
// census also reports whether OCR actually fired and what it recovered -
// this is the same local OCR the real production app uses by default.
import { readFile, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";

const manifest = JSON.parse(await readFile("datasets/manifest.json", "utf-8"));

function censusOnePdf(pdfPath) {
  const script = `
import sys, json
sys.path.insert(0, r"D:\\Bills\\backend")
from pathlib import Path
from app.services.extraction.text_extraction import PyMuPDFTextExtractor
from app.services.extraction.ocr_engine import TesseractOCREngine
from app.services.extraction import bill_parser, expense_report_parser

ocr = TesseractOCREngine()
extractor = PyMuPDFTextExtractor(ocr_engine=ocr)
path = Path(r"${pdfPath}")

# Native-only pass first, to know which pages genuinely have no selectable
# text BEFORE OCR runs (what actually determines whether OCR is invoked).
native = extractor.split_pages(path)
summary_page = expense_report_parser.find_report_summary_page(native)

# Full production-shaped pass: content_pages() (header+table, OCR'd where
# needed) + bill_pages_from(start) for everything after the report summary.
# Boundary matches the REAL production caller (pipeline.py lines ~194-199)
# EXACTLY: find_report_summary_page() first, falling back to the last page
# only when no "REPORT SUMMARY" marker is found at all - NOT content_pages()'s
# own (sometimes over-extended) last page, which was this script's first,
# incorrect version. content_pages()'s own blank-page heuristic can keep
# classifying pages as "content" past the real table when later supporting
# bills also carry native text (e.g. payment-app screenshots saved as PDF
# pages) - exactly the documented reason find_report_summary_page() exists,
# per text_extraction.py's own module comment.
content = extractor.content_pages(path)
content_last_page = summary_page if summary_page is not None else (native[-1]["page_number"] if native else 0)
start_bill_page = content_last_page + 1
bill_pages = extractor.bill_pages_from(path, start_bill_page) if start_bill_page <= len(native) else []

pages_out = []
for p in native:
    page_num = p["page_number"]
    is_content = any(c["page_number"] == page_num for c in content)
    bp = next((b for b in bill_pages if b["page_number"] == page_num), None)
    text = bp["text"] if bp else p["text"]
    source = bp["source"] if bp else p["source"]
    ocr_confidence = bp.get("ocr_confidence") if bp else None

    has_evidence = bill_parser.has_bill_evidence(text)
    gstin = bool(bill_parser.GSTIN_RE.search(text.upper())) if text else False
    parsed = bill_parser.parse_bill_text(text) if has_evidence else None
    post_evidence = bill_parser.parsed_has_evidence(parsed, text) if parsed else False

    upper = text.upper()
    pages_out.append({
        "pageNumber": page_num,
        "nativeHasSelectableText": p["has_selectable_text"],
        "nativeTextLength": len(p["text"].strip()),
        "isExpenseReportContentPage": is_content,
        "isBillRegionPage": bp is not None,
        "finalSource": source,
        "ocrConfidence": ocr_confidence,
        "hasBillEvidence": has_evidence,
        "hasGstin": gstin,
        "parsedHasEvidence": post_evidence,
        "parsedAmount": parsed.amount if parsed else None,
        "parsedVendor": parsed.vendor if parsed else None,
        "parsedCategoryGuess": parsed.category_guess if parsed else None,
        "containsExpenseSummaryMarker": "EXPENSE SUMMARY" in upper,
        "containsReportSummaryMarker": "REPORT SUMMARY" in upper,
        "containsTripSummaryMarker": "TRIP SUMMARY" in upper,
        "finalTextPreview": text[:200] if text else "",
    })

print(json.dumps({"summaryPage": summary_page, "contentLastPage": content_last_page, "pages": pages_out}))
`;
  return JSON.parse(execFileSync("D:\\Bills\\backend\\.venv\\Scripts\\python.exe", ["-I", "-c", script], { encoding: "utf-8", maxBuffer: 80 * 1024 * 1024 }));
}

const census = { generatedAt: new Date().toISOString(), reports: [] };
for (const report of manifest.reportsCovered) {
  const pdfPath = `fixtures/reference-pdfs/${report.filename}`;
  console.log(`Censusing ${report.filename} (${report.pageCount} pages)...`);
  const result = censusOnePdf(pdfPath);
  census.reports.push({ filename: report.filename, role: report.role, ...result });
  const billPages = result.pages.filter((p) => p.isBillRegionPage);
  const ocrPages = billPages.filter((p) => p.finalSource === "ocr" || p.finalSource === "native_positioned" || p.finalSource === "nvidia_ocr");
  const evidencePages = billPages.filter((p) => p.hasBillEvidence);
  console.log(`  content pages 1-${result.contentLastPage}, bill-region pages ${result.contentLastPage + 1}-${report.pageCount} (${billPages.length} pages)`);
  console.log(`  of those: ${evidencePages.length} with bill evidence, ${billPages.filter((p) => !p.nativeHasSelectableText).length} needed OCR (no native text)`);
}

await writeFile("reports/page-census.json", JSON.stringify(census, null, 2));
console.log("\nWrote reports/page-census.json");
