import os
import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
STORAGE_ROOT = (
    Path(os.environ["SPINCO_STORAGE_ROOT"]) if "SPINCO_STORAGE_ROOT" in os.environ else (BACKEND_DIR.parent / "storage")
).resolve()

EXPENSE_REPORTS_DIR = STORAGE_ROOT / "expense_reports"
TRIP_REPORTS_DIR = STORAGE_ROOT / "trip_reports"

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._ \-()]+")


def sanitize_filename(name: str) -> str:
    name = Path(name).name  # strip any path components
    return _UNSAFE_CHARS.sub("_", name).strip() or "document.pdf"


def save_upload(directory: Path, file_hash: str, original_filename: str, content: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(original_filename)
    stored_name = f"{file_hash[:12]}_{safe_name}"
    path = directory / stored_name
    path.write_bytes(content)
    return path
