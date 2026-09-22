from datetime import datetime, timezone

from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ExcelReport(Base):
    """Tracks the ONE consolidated workbook's current generation state - a
    singleton row (Phase 8), not one row per month. Phase 1 originally
    scaffolded this as month-keyed (report_month unique); Phase 8's spec is
    explicit that there must be exactly one consolidated workbook covering
    every report, not one file per month, so report_month/report_month_label
    are repurposed as a fixed "ALL" / "All Months" descriptive sentinel
    rather than a partition key - the uniqueness constraint is dropped
    accordingly. This table never had any rows written to it before Phase 8,
    so this is a zero-risk schema change."""

    __tablename__ = "excel_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_month: Mapped[str] = mapped_column(String(7))  # "ALL" - see class docstring
    report_month_label: Mapped[str] = mapped_column(String(20))  # "All Months"
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(1000))

    total_reports: Mapped[int] = mapped_column(default=0)
    total_expenses: Mapped[int] = mapped_column(default=0)
    total_claimed: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)

    # UP_TO_DATE | STALE | GENERATING
    status: Mapped[str] = mapped_column(String(20), default="UP_TO_DATE")
    last_updated: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
