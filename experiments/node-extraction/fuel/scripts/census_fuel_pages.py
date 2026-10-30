"""Census: which pages of the approved reference Expense Reports look like fuel-pump receipts?

READ-ONLY on the PDFs. Uses the production backend's LOCAL text/OCR (PyMuPDF native text + local Tesseract) purely to
FIND candidate pages; it is not the pipeline under evaluation and its text is not used as ground truth. No network, no
external AI. Output: fuel/corpus/authentic/census.json (page text + keyword hits per page).
Usage: python census_fuel_pages.py
"""
import json, os, re, sys, time
from pathlib import Path

os.environ["SPINCO_TESSERACT_DIR"] = r"D:\Bills\resources\tesseract"
for k in list(os.environ):
    if k.startswith(("SPINCO_NVIDIA_", "SPINCO_DEEPSEEK_")):
        del os.environ[k]
import httpx

httpx.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network blocked"))
sys.path.insert(0, r"D:\Bills\backend")
from app.services.extraction.ocr_engine import TesseractOCREngine  # noqa: E402
from app.services.extraction.text_extraction import PyMuPDFTextExtractor  # noqa: E402

ROOT = Path(r"D:\Bills")
PDFS = {
    "ER-39832": ROOT / "ER-39832.pdf",
    "ER-40223": ROOT / "ER-40223.pdf",
    "ER-40706": ROOT / "ER-40706 (1).pdf",
    "ER-40782": ROOT / "ER-40782.pdf",
    "ER-40513": ROOT / "uat-data-test/documents/expense_reports/506d7dbf9383_reahul report.pdf",
    "ER-41115": ROOT / "uat-data-test/documents/expense_reports/c967267ef0f4_ER-41115.pdf",
    "ER-38784": ROOT / "uat-data-test/documents/expense_reports/fee14d27038f_rohit.pdf",
    "ER-41220": ROOT / "uat-data-test/documents/expense_reports/f92b5b21cd72_vivek bhai report.pdf",
}
FUEL = re.compile(
    r"PETROL|DIESEL|FUEL|HPCL|BPCL|IOCL|INDIAN\s*OIL|HINDUSTAN\s*PETRO|BHARAT\s*PETRO|NAYARA|ESSAR|JIO[\s-]*BP|SHELL|NOZZLE|DENSITY|"
    r"AUTO\s*FUELS|FILLING\s*STATION|PETROLEUM|\bCNG\b|\bHP\b|\bBP\b|SERVICE\s*STATION|PETRO",
    re.I,
)

out = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "reports": {}}
extractor = PyMuPDFTextExtractor(ocr_engine=TesseractOCREngine())
for name, path in PDFS.items():
    t0 = time.time()
    content = extractor.content_pages(path)
    last = content[-1]["page_number"] if content else 0
    pages = extractor.bill_pages_from(path, last + 1)
    rows = []
    for p in pages:
        text = p["text"] or ""
        hits = sorted({m.group(0).upper().replace("  ", " ") for m in FUEL.finditer(text)})
        rows.append({"page": p["page_number"], "source": p["source"], "ocr_confidence": p.get("ocr_confidence"), "chars": len(text), "fuel_hits": hits, "text": text})
    out["reports"][name] = {"pdf": str(path), "pages": rows}
    print(f"{name}: {len(rows)} bill pages, {sum(1 for r in rows if r['fuel_hits'])} with fuel keywords, {time.time()-t0:.0f}s", flush=True)
dest = Path(__file__).resolve().parent.parent / "corpus" / "authentic"
dest.mkdir(parents=True, exist_ok=True)
(dest / "census.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
print("wrote", dest / "census.json")
