"""Phase 2 storage abstraction (docs/CENTRALIZED_ARCHITECTURE.md §12).

Every current call site talks to raw pathlib.Path objects directly against
SPINCO_STORAGE_ROOT - there was no interface to swap for a future S3/Azure/
NAS backend. This module introduces one, with a LocalFilesystemStorage
implementation that is behavior-identical to what storage.py already did
(same content-hash-prefixed filenames, same directories) - nothing about
where documents physically live changes in this phase.

DocumentStorage's five operations (save, retrieve, delete, exists,
get_metadata) are the ones actually wired into the API layer in this phase:
uploads.py's save, documents.py's file-existence-check and delete. The
`local_path()` escape hatch exists ONLY for LocalFilesystemStorage and is
used by the one caller (documents.py's file-download endpoint) that
genuinely needs a real filesystem path for FastAPI's FileResponse - that
Starlette helper streams with HTTP Range support directly from disk, which
a generic retrieve()-bytes-into-memory call would silently lose for large
PDFs. When a non-local backend is introduced, that one call site is the
thing that has to change (to a signed URL redirect or a chunked streaming
response) - it is the deliberate, documented boundary of this phase, not an
oversight. The deeper PDF-processing pipeline (bill_pipeline.py,
text_extraction.py's PyMuPDF calls, excel_generator.py's openpyxl read/
write) still uses local Path objects directly too, for the same reason
(those libraries need real file handles) - migrating them is pipeline
redesign, explicitly out of scope for this phase (see
docs/CENTRALIZED_ARCHITECTURE.md §12/§26).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from app.services.storage import EXCEL_DIR, EXPENSE_REPORTS_DIR, TRIP_REPORTS_DIR, sanitize_filename


@dataclass(frozen=True)
class DocumentMetadata:
    size_bytes: int
    exists: bool


class DocumentStorage(ABC):
    """A location string returned by save() is an opaque identifier as far
    as callers are concerned - for LocalFilesystemStorage it happens to be
    an absolute filesystem path (so existing DB columns like
    ExpenseReport.source_path keep meaning exactly what they mean today),
    but callers must not assume that; a future backend could make it an
    object-store key instead."""

    @abstractmethod
    def save(self, kind: str, file_hash: str, original_filename: str, content: bytes) -> str:
        """Persists content, returns its opaque storage location."""

    @abstractmethod
    def retrieve(self, location: str) -> bytes:
        """Reads back exactly what save() wrote."""

    @abstractmethod
    def delete(self, location: str) -> None:
        """Removes the document. Never raises if it's already gone."""

    @abstractmethod
    def exists(self, location: str) -> bool: ...

    @abstractmethod
    def get_metadata(self, location: str) -> DocumentMetadata: ...


_KIND_DIRS = {
    "expense_report": EXPENSE_REPORTS_DIR,
    "trip_report": TRIP_REPORTS_DIR,
    "excel": EXCEL_DIR,
}


class LocalFilesystemStorage(DocumentStorage):
    """Byte-for-byte the same behavior storage.py's save_upload() already
    had (content-hash-prefixed filename, same directories) - this class
    just gives that behavior a swappable interface, not new behavior."""

    def save(self, kind: str, file_hash: str, original_filename: str, content: bytes) -> str:
        directory = _KIND_DIRS[kind]
        directory.mkdir(parents=True, exist_ok=True)
        stored_name = f"{file_hash[:12]}_{sanitize_filename(original_filename)}"
        path = directory / stored_name
        path.write_bytes(content)
        return str(path)

    def retrieve(self, location: str) -> bytes:
        return Path(location).read_bytes()

    def delete(self, location: str) -> None:
        path = Path(location)
        if path.exists():
            path.unlink()

    def exists(self, location: str) -> bool:
        return bool(location) and Path(location).exists()

    def get_metadata(self, location: str) -> DocumentMetadata:
        path = Path(location)
        if not path.exists():
            return DocumentMetadata(size_bytes=0, exists=False)
        return DocumentMetadata(size_bytes=path.stat().st_size, exists=True)

    def local_path(self, location: str) -> Path:
        """Local-backend-only escape hatch - see this module's docstring
        for exactly why this exists and its one caller."""
        return Path(location)


# Process-wide singleton, matching the existing pattern used for the OCR
# engine/text extractor (see extraction/shared.py) - swapping the backend
# later means changing this one assignment, not every call site.
storage: DocumentStorage = LocalFilesystemStorage()


def storage_kind_for(*, expense: bool) -> str:
    return "expense_report" if expense else "trip_report"
