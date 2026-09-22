from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ValidationResult(Base):
    """One row per deterministic policy check performed against an expense
    (or, for the total check, the report as a whole - expense_id is null
    there). Replaced wholesale on every (re)validation run, the same
    pattern used for extraction/bill matching in earlier phases."""

    __tablename__ = "validation_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_report_id: Mapped[int] = mapped_column(ForeignKey("expense_reports.id"), index=True)
    expense_id: Mapped[int | None] = mapped_column(ForeignKey("expenses.id"), nullable=True, index=True)

    check_type: Mapped[str] = mapped_column(String(100))
    details: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # PASS | EXCEPTION | REVIEW_REQUIRED | NOT_APPLICABLE
    result: Mapped[str] = mapped_column(String(20))
    remarks: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Set only when result == EXCEPTION - OVER LIMIT | AMOUNT MISMATCH | DATE MISMATCH |
    # MISSING BILL | CATEGORY MISMATCH | DUPLICATE BILL | CATERING INCLUDED | TOTAL MISMATCH
    exception_type: Mapped[str | None] = mapped_column(String(50), nullable=True)

    claimed_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    policy_limit: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    bill_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    expense_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    bill_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # The category actually used for the policy lookup (post-normalization) and the
    # exact policy_version in force at validation time - so a later Policy Master
    # edit never silently changes what a historical validation meant.
    policy_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    policy_version: Mapped[int | None] = mapped_column(nullable=True)

    source_document_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_page: Mapped[int | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


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
