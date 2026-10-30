"""Render the candidate authentic fuel-receipt pages (chosen from census.json) to PNG at 200 DPI. READ-ONLY on the PDFs.
Images are derived from the project's own approved reports: local evaluation material, NOT redistributable (they contain
real employee/vehicle/payment details)."""
import json
from pathlib import Path

import fitz

ROOT = Path(r"D:\Bills")
PDFS = {
    "ER-39832": ROOT / "ER-39832.pdf", "ER-40223": ROOT / "ER-40223.pdf", "ER-40706": ROOT / "ER-40706 (1).pdf", "ER-40782": ROOT / "ER-40782.pdf",
    "ER-40513": ROOT / "uat-data-test/documents/expense_reports/506d7dbf9383_reahul report.pdf",
    "ER-41115": ROOT / "uat-data-test/documents/expense_reports/c967267ef0f4_ER-41115.pdf",
    "ER-41220": ROOT / "uat-data-test/documents/expense_reports/f92b5b21cd72_vivek bhai report.pdf",
}
# candidate PHYSICAL fuel-pump receipts (payment-app screenshots are a different document type and are excluded)
PAGES = {
    "ER-39832": [3, 4, 5, 7, 8, 11, 12, 13, 14], "ER-40223": [21], "ER-40706": [16, 17, 18, 19], "ER-40782": [4, 8],
    "ER-40513": [6, 22, 24], "ER-41115": [6, 49, 50], "ER-41220": [6],
}
out = Path(__file__).resolve().parent.parent / "corpus" / "authentic" / "images"
out.mkdir(parents=True, exist_ok=True)
index = []
for report, pages in PAGES.items():
    doc = fitz.open(PDFS[report])
    for p in pages:
        rid = f"auth_{report}_p{p:02d}"
        pix = doc[p - 1].get_pixmap(dpi=200)
        pix.save(out / f"{rid}.png")
        index.append({"receipt_id": rid, "report": report, "page": p, "pdf": str(PDFS[report]), "width": pix.width, "height": pix.height})
    doc.close()
(out.parent / "render_index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
print(len(index), "pages rendered")
