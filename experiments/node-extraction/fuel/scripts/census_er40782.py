"""ER-40782's content_pages() heuristic over-extends to every page (a documented quirk, see reports/receipt-extraction-baseline-
20261013.md), so the first census saw no bill pages. Its Expense Summary is pages 1-2; the supporting pages are 3-10.
Read-only; merges the result into census.json."""
import json, os, re, sys
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

FUEL = re.compile(r"PETROL|DIESEL|FUEL|HPCL|BPCL|IOCL|INDIAN\s*OIL|HINDUSTAN\s*PETRO|BHARAT\s*PETRO|NAYARA|ESSAR|JIO[\s-]*BP|SHELL|NOZZLE|DENSITY|AUTO\s*FUELS|FILLING\s*STATION|PETROLEUM|\bCNG\b|\bHP\b|\bBP\b|SERVICE\s*STATION|PETRO", re.I)
path = Path(r"D:\Bills\ER-40782.pdf")
extractor = PyMuPDFTextExtractor(ocr_engine=TesseractOCREngine())
pages = extractor.bill_pages_from(path, 3)
rows = [{"page": p["page_number"], "source": p["source"], "ocr_confidence": p.get("ocr_confidence"), "chars": len(p["text"] or ""),
         "fuel_hits": sorted({m.group(0).upper() for m in FUEL.finditer(p["text"] or "")}), "text": p["text"] or ""} for p in pages]
census = Path(__file__).resolve().parent.parent / "corpus" / "authentic" / "census.json"
data = json.loads(census.read_text(encoding="utf-8"))
data["reports"]["ER-40782"] = {"pdf": str(path), "pages": rows, "note": "bill pages = 3.. (content_pages quirk)"}
census.write_text(json.dumps(data, indent=1), encoding="utf-8")
print(f"ER-40782: {len(rows)} bill pages, {sum(1 for r in rows if r['fuel_hits'])} with fuel keywords")
