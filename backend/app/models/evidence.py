from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SourceEvidence(Base):
    """Per-field page traceability for extracted values - the basis for the
    UI's "View Source" action, which jumps the PDF viewer to this page.
    Rows for a document are replaced wholesale on each (re)extraction run."""

    __tablename__ = "source_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_type: Mapped[str] = mapped_column(String(20), index=True)  # EXPENSE_REPORT | TRIP_REPORT
    document_ref_id: Mapped[int] = mapped_column(index=True)
    field_name: Mapped[str] = mapped_column(String(100))  # e.g. "employee_name", "expense_line[3].amount"
    page_number: Mapped[int] = mapped_column()
    extracted_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
