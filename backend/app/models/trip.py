from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TripReport(Base):
    __tablename__ = "trip_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    trip_id: Mapped[str | None] = mapped_column(String(50), index=True, nullable=True)  # e.g. TRIP-24935, from filename only
    filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    file_type: Mapped[str] = mapped_column(String(100), default="application/pdf")
    file_size: Mapped[int | None] = mapped_column(nullable=True)
    page_count: Mapped[int | None] = mapped_column(nullable=True)
    source_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    processing_status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Document-extraction lifecycle (Phase 3): NOT_PROCESSED | PROCESSING | EXTRACTED |
    # REVIEW_REQUIRED | FAILED.
    extraction_status: Mapped[str] = mapped_column(String(20), default="NOT_PROCESSED", index=True)
    extraction_confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)  # HIGH | MEDIUM | LOW
    extraction_notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # True when this trip record was parsed out of an Expense Report PDF's own
    # embedded "TRIP SUMMARY" section rather than a standalone Trip Report PDF.
    is_embedded: Mapped[bool] = mapped_column(Boolean, default=False)
    source_expense_report_id: Mapped[int | None] = mapped_column(ForeignKey("expense_reports.id"), nullable=True)
    embedded_source_page: Mapped[int | None] = mapped_column(nullable=True)

    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    employee_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    employee_code: Mapped[str | None] = mapped_column(String(50), nullable=True)

    report_month: Mapped[str | None] = mapped_column(String(7), index=True, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    origin: Mapped[str | None] = mapped_column(String(255), nullable=True)
    destination: Mapped[str | None] = mapped_column(String(255), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(500), nullable=True)

    approval_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    approval_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    approver: Mapped[str | None] = mapped_column(String(255), nullable=True)
    validation_status: Mapped[str | None] = mapped_column(String(20), nullable=True)

    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    legs: Mapped[list["Trip"]] = relationship(back_populates="trip_report", cascade="all, delete-orphan")


class Trip(Base):
    """A single from/to leg within a Trip Report's itinerary."""

    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(primary_key=True)
    trip_report_id: Mapped[int] = mapped_column(ForeignKey("trip_reports.id"), index=True)
    leg_no: Mapped[int | None] = mapped_column(nullable=True)
    from_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    from_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    to_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    to_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    trip_report: Mapped["TripReport"] = relationship(back_populates="legs")
