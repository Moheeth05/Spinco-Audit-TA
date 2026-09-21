from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ValidationResult(Base):
    """One row per check performed against an expense (or the report as a whole)."""

    __tablename__ = "validation_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_report_id: Mapped[int] = mapped_column(ForeignKey("expense_reports.id"), index=True)
    expense_id: Mapped[int | None] = mapped_column(ForeignKey("expenses.id"), nullable=True)

    check_type: Mapped[str] = mapped_column(String(100))
    details: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # PASS | FAIL | NOT_APPLICABLE | REVIEW_REQUIRED
    result: Mapped[str] = mapped_column(String(20))
    remarks: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class ExceptionRecord(Base):
    __tablename__ = "exceptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_report_id: Mapped[int] = mapped_column(ForeignKey("expense_reports.id"), index=True)
    expense_id: Mapped[int | None] = mapped_column(ForeignKey("expenses.id"), nullable=True)

    exception_type: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(1000))
    source_pdf: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_page: Mapped[int | None] = mapped_column(nullable=True)

    # OPEN | RESOLVED
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
