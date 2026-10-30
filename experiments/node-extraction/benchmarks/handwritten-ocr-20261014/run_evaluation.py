"""
TASK: Improve handwritten bill OCR and exception handling - ground-truth
evaluation. Manually verified ground truth (recorded by directly viewing
each rendered page, BEFORE running any OCR - never from the system's own
predictions):

RATHNA ELECTRICALS & HARDWARES (ER-39832.pdf page 6):
  - This page is NOT upright as rendered by PyMuPDF (confirmed: page.rotation
    == 0, i.e. no PDF-level rotation metadata - the embedded photo itself is
    landscape). A real, confirmed case for the new orientation-correction
    feature, found during this evaluation, not constructed for it.
  - vendor: RATHNA ELECTRICALS & HARDWARES
  - invoice_number: 664
  - gstin: 32ARWPK8203C1ZW
  - amount: 220 (handwritten "GRAND TOTAL ... 220/-")
  - date: handwritten, read as 9/6/2026 after rotation correction (day/month
    order ambiguous as with any DD/MM numeric date - not resolved further
    here, this evaluation records the digits actually visible)

DISHANT PETROLEUM SERVICE (vivek-bhai-report-ER41220.pdf page 7):
  - Already upright as rendered (confirmed via OSD/visual check).
  - vendor: DISHANT PETROLEUM SERVICE
  - invoice_number: 648591
  - gstin: 24AKHPP8768M1ZA
  - amount: 300 (handwritten, appears twice - row amount and total)
  - date: handwritten, read as 17/9/26

Compares: (a) the OLD code path (plain pytesseract.image_to_data, no
rotation correction, no preprocessing - exactly what ocr_engine.py did
before this task) vs (b) the NEW TesseractOCREngine.extract_text() (rotation
correction always applied; preprocessing fallback only reachable when BOTH
the default pass and the plain higher-DPI retry return nothing, consistent
with the existing, evidence-based narrow-retry design - see
text_extraction.py::_retry_ocr_at_higher_dpi).
"""
import re
import sys
import time

sys.path.insert(0, r"D:\Bills\backend")
import fitz  # noqa: E402
import pytesseract  # noqa: E402
from PIL import Image  # noqa: E402
from io import BytesIO  # noqa: E402

from app.services.extraction.bill_parser import parse_bill_text  # noqa: E402
from app.services.extraction.ocr_engine import TesseractOCREngine  # noqa: E402

CASES = [
    {
        "name": "RATHNA ELECTRICALS (rotated page)",
        "pdf": r"D:\Bills\experiments\node-extraction\fixtures\reference-pdfs\ER-39832.pdf",
        "page": 6,
        "ground_truth": {
            "vendor_contains": "RATHNA",
            "invoice_number": "664",
            "gstin": "32ARWPK8203C1ZW",
            "amount": 220.0,
            "date_digits_seen": "9/6/2026",
        },
    },
    {
        "name": "DISHANT PETROLEUM SERVICE",
        "pdf": r"D:\Bills\experiments\node-extraction\fixtures\reference-pdfs\vivek-bhai-report-ER41220.pdf",
        "page": 7,
        "ground_truth": {
            "vendor_contains": "DISHANT",
            "invoice_number": "648591",
            "gstin": "24AKHPP8768M1ZA",
            "amount": 300.0,
            "date_digits_seen": "17/9/26",
        },
    },
]


def old_style_ocr(image_bytes: bytes) -> dict:
    """Exactly what ocr_engine.py did before this task: no orientation
    correction, no preprocessing - plain image_to_data on the raw render."""
    image = Image.open(BytesIO(image_bytes))
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    words, confidences = [], []
    for i, text in enumerate(data["text"]):
        if not text.strip():
            continue
        words.append((text, data["block_num"][i], data["par_num"][i], data["line_num"][i]))
        conf = data["conf"][i]
        if conf != "-1":
            confidences.append(int(conf))
    lines, current_key = [], None
    for text, b, p, l in words:
        key = (b, p, l)
        if key != current_key:
            lines.append([])
            current_key = key
        lines[-1].append(text)
    text_out = "\n".join(" ".join(w) for w in lines)
    avg_conf = (sum(confidences) / len(confidences) / 100) if confidences else 0.0
    return {"text": text_out, "confidence": avg_conf}


def main():
    engine = TesseractOCREngine()
    results = []

    for case in CASES:
        print("=" * 70)
        print(case["name"])
        print("=" * 70)
        doc = fitz.open(case["pdf"])
        page = doc[case["page"] - 1]
        image_bytes = page.get_pixmap(dpi=200).tobytes("png")
        doc.close()

        t0 = time.perf_counter()
        before = old_style_ocr(image_bytes)
        before_ms = (time.perf_counter() - t0) * 1000
        before_parsed = parse_bill_text(before["text"])

        t1 = time.perf_counter()
        after = engine.extract_text(image_bytes)
        after_ms = (time.perf_counter() - t1) * 1000
        after_parsed = parse_bill_text(after["text"])

        gt = case["ground_truth"]

        def field_result(label, before_val, after_val, gt_val, matcher=None):
            match_fn = matcher or (lambda v: v == gt_val)
            print(f"  {label:18s} GT={gt_val!r:30s} BEFORE={before_val!r:30s} [{'OK' if before_val and match_fn(before_val) else 'no'}]"
                  f"  AFTER={after_val!r:30s} [{'OK' if after_val and match_fn(after_val) else 'no'}]")
            return {
                "field": label,
                "ground_truth": gt_val,
                "before": before_val,
                "before_correct": bool(before_val and match_fn(before_val)),
                "after": after_val,
                "after_correct": bool(after_val and match_fn(after_val)),
            }

        case_result = {
            "name": case["name"],
            "rotation_applied": after.get("rotation_applied", 0),
            "before_text_length": len(before["text"].strip()),
            "after_text_length": len(after["text"].strip()),
            "before_confidence": before["confidence"],
            "after_confidence": after["confidence"],
            "before_ms": round(before_ms, 1),
            "after_ms": round(after_ms, 1),
            "fields": [],
        }

        print(f"  rotation_applied: {after.get('rotation_applied', 0)} degrees")
        print(f"  text length:  before={case_result['before_text_length']}  after={case_result['after_text_length']}")
        print(f"  confidence:   before={before['confidence']:.3f}  after={after['confidence']:.3f}")
        print(f"  runtime (ms): before={before_ms:.0f}  after={after_ms:.0f}")
        print()

        case_result["fields"].append(
            field_result("vendor", before_parsed.vendor, after_parsed.vendor, gt["vendor_contains"],
                          matcher=lambda v: gt["vendor_contains"] in (v or "").upper())
        )
        case_result["fields"].append(
            field_result("invoice_number", before_parsed.bill_number, after_parsed.bill_number, gt["invoice_number"])
        )
        case_result["fields"].append(
            field_result("gstin", before_parsed.gstin, after_parsed.gstin, gt["gstin"])
        )
        case_result["fields"].append(
            field_result("amount", before_parsed.amount, after_parsed.amount, gt["amount"])
        )
        # Date: report whether a date was found at all, and whether the
        # digits visible in the ground truth appear anywhere in the raw OCR
        # text (a looser, honest check - this task does not claim to have
        # solved ambiguous-date parsing for handwritten digits specifically).
        date_digits = re.sub(r"\D", "", gt["date_digits_seen"])
        before_has_digits = date_digits[:4] in re.sub(r"\D", "", before["text"])
        after_has_digits = date_digits[:4] in re.sub(r"\D", "", after["text"])
        print(f"  {'date (raw digits)':18s} GT={gt['date_digits_seen']!r:30s} "
              f"BEFORE parsed={before_parsed.bill_date!r} digits-in-text={before_has_digits}   "
              f"AFTER parsed={after_parsed.bill_date!r} digits-in-text={after_has_digits}")
        case_result["fields"].append(
            {
                "field": "date",
                "ground_truth": gt["date_digits_seen"],
                "before_parsed_date": str(before_parsed.bill_date) if before_parsed.bill_date else None,
                "before_digits_in_raw_text": before_has_digits,
                "after_parsed_date": str(after_parsed.bill_date) if after_parsed.bill_date else None,
                "after_digits_in_raw_text": after_has_digits,
            }
        )

        print()
        print(f"  BEFORE status: {before_parsed.status} ({before_parsed.confidence})")
        print(f"  AFTER  status: {after_parsed.status} ({after_parsed.confidence})")
        case_result["before_status"] = before_parsed.status
        case_result["after_status"] = after_parsed.status
        print()

        results.append(case_result)

    import json

    with open(r"D:\Bills\experiments\node-extraction\benchmarks\handwritten-ocr-20261014\results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Wrote results.json")


if __name__ == "__main__":
    main()
