"""
TASK: Benchmark Python OCR Rendering at 200 DPI vs 300 DPI - Isolated Evaluation Only

Runs the SAME, unmodified production OCR implementation
(app.services.extraction.ocr_engine.TesseractOCREngine) against the SAME
pages, changing only the PyMuPDF render DPI (200 vs 300). Nothing in
backend/ is imported-and-modified; this script only calls existing public
functions/classes with data it renders itself, exactly mirroring the
`dpi=200` call site at text_extraction.py:171
(`doc[page_number-1].get_pixmap(dpi=200).tobytes("png")`).

Writes benchmarks/dpi-200-vs-300/results-raw.json only. Does not touch any
production file, the production DB, UAT data, baseline reports, or the
ground-truth annotations file (read-only).
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\Bills\backend")
import fitz  # noqa: E402
from app.services.extraction.bill_parser import parse_bill_text  # noqa: E402
from app.services.extraction.ocr_engine import TesseractOCREngine  # noqa: E402

REF_PDF_DIR = Path(r"D:\Bills\experiments\node-extraction\fixtures\reference-pdfs")
GROUND_TRUTH_PATH = Path(
    r"D:\Bills\experiments\node-extraction\datasets\human-verified-bill-annotations.json"
)
OUT_PATH = Path(__file__).parent / "results-raw.json"

DPI_VALUES = [200, 300]

# Documented, previously-established corrections (see
# reports/receipt-extraction-baseline-20261013.json "groundTruthCorrection").
# The stored annotations file is NEVER modified; these corrections are
# applied only when interpreting results, exactly as in prior evaluation
# scripts (scripts/evaluate-ocr-sample-after-statement-fix.mjs).
GROUND_TRUTH_CORRECTIONS = {
    ("ER-40706.pdf", 6): {
        "amount": 3000,  # unchanged - coincidentally the same figure on the real document (PhonePe payment to RAM MURTI, not the Taxi Operator's Union voucher the sample was mislabeled as)
        "note": "mislabeled vendor in source annotation; actual page is a PhonePe payment to RAM MURTI, not Taxi Operator's Union. totalAmount (3000) is coincidentally unaffected.",
    },
    ("ER-40706.pdf", 22): {
        "amount": None,  # FASTag multi-transaction e-statement, not KFC - no single "bill total" exists on this page
        "note": "mislabeled document in source annotation; actual page is an ICICI FASTag E-Statement, not a KFC receipt. Corrected ground-truth amount is null (multi-transaction statement, not a bill total).",
    },
}

# Pages selected to satisfy every criterion in Step 1 of the task:
#  - the known failing low-resolution receipt page (vivek-bhai p6)
#  - other image-heavy receipt/bill pages (vivek p7/p9, ER-39832 p3/p6, ER-40706 p10/p12)
#  - short receipts / transaction screenshots (vivek p25)
#  - multi-transaction statements (ER-40706 p20, p22)
#  - a page whose native-PDF-text status was surprising vs. its ground-truth note (vivek p15 -
#    annotated as "likely has native PDF text" but page-census.json shows it is in fact OCR'd,
#    nativeTextLength=2 - a genuinely useful inclusion, not hand-picked for a good outcome)
#  - Expense Report pages whose extraction is already correct: NOT included in the render/OCR
#    loop below. Every ER-* content/header page in the corpus (e.g. ER-39832 p1-2, ER-40706 p1-2)
#    has native selectable text and is never routed to OCR at all (confirmed via
#    reports/page-census.json and text_extraction.py:150-177: `has_selectable_text` is computed
#    from `doc[i].get_text()` alone, which does not depend on render DPI). Re-rendering them would
#    not exercise the thing this benchmark is testing.
# One native-text CONTROL page is listed separately (see NATIVE_CONTROL_PAGE below), not sent
# through rendering/OCR at all - it exists to document why DPI cannot affect it.
PAGES = [
    ("vivek-bhai-report-ER41220.pdf", 6),
    ("vivek-bhai-report-ER41220.pdf", 7),
    ("vivek-bhai-report-ER41220.pdf", 9),
    ("vivek-bhai-report-ER41220.pdf", 15),
    ("vivek-bhai-report-ER41220.pdf", 25),
    ("ER-40706.pdf", 6),
    ("ER-40706.pdf", 10),
    ("ER-40706.pdf", 12),
    ("ER-40706.pdf", 20),
    ("ER-40706.pdf", 22),
    ("ER-39832.pdf", 3),
    ("ER-39832.pdf", 6),
]

NATIVE_CONTROL_PAGE = ("ER-40782.pdf", 3)


def load_ground_truth():
    data = json.loads(GROUND_TRUTH_PATH.read_text(encoding="utf-8"))
    by_key = {}
    for sample in data["samples"]:
        by_key[(sample["filename"], sample["page"])] = sample
    return by_key


def corrected_amount(filename, page, sample):
    correction = GROUND_TRUTH_CORRECTIONS.get((filename, page))
    if correction is not None:
        return correction["amount"], correction["note"]
    return sample.get("totalAmount"), None


def render_and_ocr(doc, page_number, dpi, engine):
    t0 = time.perf_counter()
    pix = doc[page_number - 1].get_pixmap(dpi=dpi)
    png_bytes = pix.tobytes("png")
    render_ms = (time.perf_counter() - t0) * 1000

    t1 = time.perf_counter()
    result = engine.extract_text(png_bytes)
    ocr_ms = (time.perf_counter() - t1) * 1000

    text = result["text"]
    parsed = parse_bill_text(text)

    return {
        "dpi": dpi,
        "pixelDims": f"{pix.width}x{pix.height}",
        "renderMs": round(render_ms, 2),
        "ocrMs": round(ocr_ms, 2),
        "textLength": len(text.strip()),
        "ocrConfidence": result["confidence"],
        "text": text,
        "parsed": {
            "vendor": parsed.vendor,
            "billNumber": parsed.bill_number,
            "billDate": str(parsed.bill_date) if parsed.bill_date else None,
            "amount": parsed.amount,
            "gstin": parsed.gstin,
            "categoryGuess": parsed.category_guess,
            "confidence": parsed.confidence,
            "status": parsed.status,
            "warnings": parsed.warnings,
        },
    }


def main():
    ground_truth = load_ground_truth()
    engine = TesseractOCREngine()

    results = []
    for filename, page_number in PAGES:
        sample = ground_truth.get((filename, page_number))
        if sample is None:
            raise RuntimeError(f"No ground-truth sample for {filename} page {page_number}")
        gt_amount, correction_note = corrected_amount(filename, page_number, sample)

        pdf_path = REF_PDF_DIR / filename
        doc = fitz.open(pdf_path)
        try:
            runs = [render_and_ocr(doc, page_number, dpi, engine) for dpi in DPI_VALUES]
        finally:
            doc.close()

        entry = {
            "filename": filename,
            "page": page_number,
            "documentType": sample.get("documentType"),
            "quality": sample.get("quality"),
            "groundTruth": {
                "vendor": sample.get("vendor"),
                "amount": gt_amount,
                "amountCorrectionNote": correction_note,
                "invoiceNumber": sample.get("invoiceNumber"),
                "gstin": sample.get("gstin"),
            },
            "runs": runs,
        }
        results.append(entry)
        print(
            f"{filename} p{page_number}: "
            f"200dpi textLen={runs[0]['textLength']} amount={runs[0]['parsed']['amount']} | "
            f"300dpi textLen={runs[1]['textLength']} amount={runs[1]['parsed']['amount']} "
            f"(gt amount={gt_amount})"
        )

    # Native-text control page: documented, not executed, since the routing
    # decision (text_extraction.py:150-177) never reads `dpi` - has_selectable_text
    # depends only on doc.get_text(), so rendering it at either DPI is a no-op for
    # the production decision this benchmark is evaluating.
    control_sample = ground_truth[NATIVE_CONTROL_PAGE]
    native_control = {
        "filename": NATIVE_CONTROL_PAGE[0],
        "page": NATIVE_CONTROL_PAGE[1],
        "note": (
            "Native-selectable-text control page (confirmed via reports/page-census.json: "
            "nativeHasSelectableText=true, nativeTextLength=347 >= MIN_NATIVE_TEXT_CHARS=20). "
            "Not rendered/OCR'd by this benchmark: text_extraction.py's has_selectable_text gate "
            "(split_pages, line 155) is computed solely from doc[i].get_text() and never reads "
            "`dpi` - the dpi parameter is only used inside _apply_ocr's OCR-only branch (line 171), "
            "which this page never reaches in production at either DPI setting."
        ),
        "groundTruth": {
            "vendor": control_sample.get("vendor"),
            "amount": control_sample.get("totalAmount"),
        },
    }

    output = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dpiValues": DPI_VALUES,
        "pageCount": len(results),
        "results": results,
        "nativeTextControl": native_control,
    }
    OUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
