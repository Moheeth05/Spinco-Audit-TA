"""Structural PDF validation for upload intake: is this actually a readable PDF?

This is deliberately NOT the extraction pipeline (see services/interfaces.py).
It never reads business content - only confirms the file is a well-formed,
openable PDF and reports its page count, so a corrupted or password-protected
document fails clearly at upload time instead of silently breaking a later
phase.
"""

from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB
PDF_MAGIC = b"%PDF-"


def max_file_size_bytes() -> int:
    """The per-file upload limit: SPINCO_MAX_UPLOAD_MB (default 25 MB, see services/hardening.py)."""
    from app.services import hardening

    return hardening.max_upload_bytes()


class PdfValidationError(Exception):
    """Raised with a user-safe message - never a raw stack trace or library exception text."""


@dataclass
class PdfValidationResult:
    page_count: int


def validate_pdf_upload(filename: str, content: bytes) -> PdfValidationResult:
    if not filename.lower().endswith(".pdf"):
        raise PdfValidationError("Only PDF files are supported.")

    if len(content) == 0:
        raise PdfValidationError("The uploaded file is empty.")

    limit = max_file_size_bytes()
    if len(content) > limit:
        limit_mb = limit // (1024 * 1024)
        raise PdfValidationError(f"File exceeds the {limit_mb} MB upload limit.")

    if not content.startswith(PDF_MAGIC):
        raise PdfValidationError("This file does not appear to be a valid PDF.")

    try:
        reader = PdfReader(BytesIO(content))
    except (PdfReadError, ValueError, OSError):
        raise PdfValidationError("The PDF could not be read - it may be corrupted.")

    if reader.is_encrypted:
        try:
            reader.decrypt("")
            _ = len(reader.pages)
        except Exception:
            raise PdfValidationError(
                "This PDF is password-protected and cannot be processed. Remove the password and re-upload."
            )

    try:
        page_count = len(reader.pages)
    except Exception:
        raise PdfValidationError("The PDF could not be read - it may be corrupted.")

    if page_count == 0:
        raise PdfValidationError("The PDF has no pages.")

    return PdfValidationResult(page_count=page_count)
