import os
import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
STORAGE_ROOT = (
    Path(os.environ["SPINCO_STORAGE_ROOT"]) if "SPINCO_STORAGE_ROOT" in os.environ else (BACKEND_DIR.parent / "storage")
).resolve()

EXPENSE_REPORTS_DIR = STORAGE_ROOT / "expense_reports"
TRIP_REPORTS_DIR = STORAGE_ROOT / "trip_reports"
EXCEL_DIR = STORAGE_ROOT / "excel"

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._ \-()]+")


def sanitize_filename(name: str) -> str:
    name = Path(name).name  # strip any path components
    return _UNSAFE_CHARS.sub("_", name).strip() or "document.pdf"
