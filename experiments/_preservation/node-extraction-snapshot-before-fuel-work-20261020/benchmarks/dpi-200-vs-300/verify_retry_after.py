"""
Post-implementation verification for "Implement a Targeted 300 DPI OCR Retry
for Empty Results". Re-runs the SAME 12 benchmark pages used in REPORT.md,
this time through the real, now-patched production pipeline
(PyMuPDFTextExtractor.split_pages + _apply_ocr, with TesseractOCREngine),
to confirm:

  1. The two pages that returned 0 chars at 200 DPI in the original
     benchmark now get a transparent retry and recover text/an amount.
  2. Every page that already had non-empty text at 200 DPI in the original
     benchmark is untouched by this change (same text, same parsed amount).

Writes only results-after-retry.json in this same benchmark directory. Does
not modify results-raw.json, REPORT.md, or any production/baseline file.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\Bills\backend")
from app.services.extraction.bill_parser import parse_bill_text  # noqa: E402
from app.services.extraction.ocr_engine import TesseractOCREngine  # noqa: E402
from app.services.extraction.text_extraction import PyMuPDFTextExtractor  # noqa: E402

REF_PDF_DIR = Path(r"D:\Bills\experiments\node-extraction\fixtures\reference-pdfs")
RAW_RESULTS_PATH = Path(__file__).parent / "results-raw.json"
OUT_PATH = Path(__file__).parent / "results-after-retry.json"

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


def main():
    raw = json.loads(RAW_RESULTS_PATH.read_text(encoding="utf-8"))
    before_200dpi = {(r["filename"], r["page"]): r["runs"][0] for r in raw["results"]}

    engine = TesseractOCREngine()
    extractor = PyMuPDFTextExtractor(ocr_engine=engine)

    results = []
    total_ms = 0.0
    for filename, page_number in PAGES:
        pdf_path = REF_PDF_DIR / filename
        t0 = time.perf_counter()
        pages = extractor.split_pages(pdf_path)
        subset = [p for p in pages if p["page_number"] == page_number]
        extractor._apply_ocr(pdf_path, subset)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        total_ms += elapsed_ms

        page = subset[0]
        parsed = parse_bill_text(page["text"])
        before = before_200dpi[(filename, page_number)]
        before_text_len = before["textLength"]
        after_text_len = len(page["text"].strip())
        retried = before_text_len == 0 and after_text_len > 0

        entry = {
            "filename": filename,
            "page": page_number,
            "elapsedMs": round(elapsed_ms, 2),
            "before200dpiTextLength": before_text_len,
            "before200dpiAmount": before["parsed"]["amount"],
            "afterTextLength": after_text_len,
            "afterAmount": parsed.amount,
            "afterSource": page["source"],
            "afterOcrConfidence": page["ocr_confidence"],
            "retryDetected": retried,
            "textUnchangedFromBefore200dpi": page["text"] == before["text"] if before_text_len > 0 else None,
        }
        results.append(entry)
        print(
            f"{filename} p{page_number}: before200dpi textLen={before_text_len} amount={before['parsed']['amount']} | "
            f"after textLen={after_text_len} amount={parsed.amount} source={page['source']} "
            f"retried={retried} elapsed={elapsed_ms:.0f}ms"
        )

    output = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "totalElapsedMs": round(total_ms, 2),
        "pageCount": len(results),
        "results": results,
    }
    OUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\nTotal elapsed (12 pages, production _apply_ocr with retry): {total_ms:.0f} ms")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
