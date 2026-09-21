from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ExpenseReport(Base):
    __tablename__ = "expense_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    er_id: Mapped[str | None] = mapped_column(String(50), index=True, nullable=True)  # e.g. ER-39832, from filename only
    filename: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(500))
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    file_type: Mapped[str] = mapped_column(String(100), default="application/pdf")
    file_size: Mapped[int] = mapped_column()
    page_count: Mapped[int | None] = mapped_column(nullable=True)
    source_path: Mapped[str] = mapped_column(String(1000))

    # Upload/intake lifecycle for this phase: QUEUED | PROCESSING | PROCESSED | FAILED.
    # Deliberately separate from overall_status, which is the later validation/audit outcome.
    processing_status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    employee_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    employee_code: Mapped[str | None] = mapped_column(String(50), nullable=True)

    report_month: Mapped[str | None] = mapped_column(String(7), index=True, nullable=True)  # "2026-06"
    report_month_label: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "June 2026"
    submitted_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    total_claimed: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    total_expenses: Mapped[int] = mapped_column(default=0)
    verified_expense_count: Mapped[int] = mapped_column(default=0)
    exception_count: Mapped[int] = mapped_column(default=0)
    review_required_count: Mapped[int] = mapped_column(default=0)

    # Set by the future validation/exception engine: ACCOUNT VERIFIED | EXCEPTION | REVIEW REQUIRED.
    # Left null until that phase actually computes it - never defaulted or guessed here.
    overall_status: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    policy_version: Mapped[int | None] = mapped_column(nullable=True)

    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    expenses: Mapped[list["Expense"]] = relationship(back_populates="expense_report", cascade="all, delete-orphan")
    bills: Mapped[list["Bill"]] = relationship(back_populates="expense_report", cascade="all, delete-orphan")


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_report_id: Mapped[int] = mapped_column(ForeignKey("expense_reports.id"), index=True)
    s_no: Mapped[int | None] = mapped_column(nullable=True)

    expense_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    expense_details: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    claimed_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    policy_basis: Mapped[str | None] = mapped_column(String(20), nullable=True)
    policy_limit: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    bill_found: Mapped[bool] = mapped_column(default=False)
    bill_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), nullable=True)
    bill_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    bill_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    trip_report_id: Mapped[int | None] = mapped_column(ForeignKey("trip_reports.id"), nullable=True)

    # PASS | FAIL | NOT_APPLICABLE | REVIEW_REQUIRED
    validation_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    final_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_page: Mapped[int | None] = mapped_column(nullable=True)

    expense_report: Mapped["ExpenseReport"] = relationship(back_populates="expenses")
