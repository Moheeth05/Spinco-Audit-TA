from datetime import date

from sqlalchemy import Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Bill(Base):
    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_report_id: Mapped[int] = mapped_column(ForeignKey("expense_reports.id"), index=True)

    page_number: Mapped[int | None] = mapped_column(nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bill_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bill_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    gstin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    category_guess: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # Phase 4 extraction outcome for this bill record only - EXTRACTED | REVIEW_REQUIRED.
    # Separate from match_status below, which describes matching against an expense.
    extraction_status: Mapped[str] = mapped_column(String(20), default="REVIEW_REQUIRED")
    extraction_confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)  # HIGH | MEDIUM | LOW
    extraction_notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # UNMATCHED | MATCHED - whether some expense has claimed this bill as its best match.
    # (The DUPLICATE business exception itself is a later validation-phase concern.)
    match_status: Mapped[str] = mapped_column(String(20), default="UNMATCHED")
    # Was a plain, unconstrained int (no referential integrity at all) until
    # Phase 1 hardening - confirmed zero orphaned values in dev/production
    # before adding this FK (see docs/CENTRALIZED_ARCHITECTURE.md).
    matched_expense_id: Mapped[int | None] = mapped_column(ForeignKey("expenses.id"), nullable=True)

    expense_report: Mapped["ExpenseReport"] = relationship(back_populates="bills")
