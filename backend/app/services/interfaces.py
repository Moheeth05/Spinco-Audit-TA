"""Contracts for the document-processing pipeline.

Phase 1 defines these interfaces only, so the upload/API layer and the
database schema can be built against a stable shape. No implementation lives
here yet - each is built and tested as its own later phase:

    PDFProcessor            Phase 2/3 - page splitting, text vs. scanned detection
    OCREngine                Phase 3   - OCR for photographed/scanned bill pages
    ExpenseExtractor        Phase 3   - expense table + header extraction from ER PDFs
    BillExtractor            Phase 4   - supporting bill/receipt extraction
    BillMatcher              Phase 4   - expense-to-bill matching
    PolicyValidationEngine  Phase 5   - policy/limit/date/amount/category checks
    TripMatcher              Phase 6   - Batta-to-Trip matching (including embedded trip summaries)
    ExceptionEngine          Phase 7   - aggregates check results into exceptions + report status
    ExcelGenerator            Phase 8   - consolidated monthly workbook generation/update

Every method raises NotImplementedError until its phase implements it - no
fake/stub data is ever returned from these.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class PDFProcessor(ABC):
    @abstractmethod
    def split_pages(self, pdf_path: Path) -> list[dict[str, Any]]:
        """Return per-page metadata (page number, has_selectable_text, image bytes)."""

    @abstractmethod
    def detect_document_type(self, pdf_path: Path) -> str:
        """Return 'EXPENSE_REPORT' or 'TRIP_REPORT' based on document content."""


class OCREngine(ABC):
    @abstractmethod
    def extract_text(self, image_bytes: bytes) -> dict[str, Any]:
        """Return {"text": str, "confidence": float} for a scanned/photographed page."""


class ExpenseExtractor(ABC):
    @abstractmethod
    def extract_header(self, pdf_path: Path) -> dict[str, Any]:
        """Extract ER ID, employee, report month, submitted date, totals."""

    @abstractmethod
    def extract_expense_lines(self, pdf_path: Path) -> list[dict[str, Any]]:
        """Extract the expense summary table rows."""

    @abstractmethod
    def extract_embedded_trip_summary(self, pdf_path: Path) -> dict[str, Any] | None:
        """Extract an embedded TRIP SUMMARY section when present in the ER PDF itself."""


class BillExtractor(ABC):
    @abstractmethod
    def extract_bills(self, pdf_path: Path, start_page: int) -> list[dict[str, Any]]:
        """Extract supporting bill/receipt pages (vendor, amount, date, GSTIN, etc.)."""


class BillMatcher(ABC):
    @abstractmethod
    def match(self, expenses: list[dict[str, Any]], bills: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return expense-to-bill matches using date/amount/vendor/description signals."""


class PolicyValidationEngine(ABC):
    @abstractmethod
    def validate_expense(self, expense: dict[str, Any], policy: dict[str, Any]) -> list[dict[str, Any]]:
        """Return a list of check results (policy limit, bill amount/date, category consistency, ...)."""


class TripMatcher(ABC):
    @abstractmethod
    def match_batta(self, batta_expense: dict[str, Any], candidate_trips: list[dict[str, Any]]) -> dict[str, Any]:
        """Return the matched trip (if any) and the resulting check outcome for a BATTA expense."""


class ExceptionEngine(ABC):
    @abstractmethod
    def evaluate_report(self, expense_report_id: int) -> dict[str, Any]:
        """Aggregate all check/exception rows for a report into its overall status."""


class ExcelGenerator(ABC):
    @abstractmethod
    def update_monthly_workbook(self, report_month: str) -> Path:
        """Append/update the given month's section in the single consolidated workbook."""
