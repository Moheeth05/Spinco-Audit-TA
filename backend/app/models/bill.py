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

    # UNMATCHED | MATCHED | DUPLICATE
    match_status: Mapped[str] = mapped_column(String(20), default="UNMATCHED")
    matched_expense_id: Mapped[int | None] = mapped_column(nullable=True)

    expense_report: Mapped["ExpenseReport"] = relationship(back_populates="bills")
