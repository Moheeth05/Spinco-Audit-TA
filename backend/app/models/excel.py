from datetime import datetime, timezone

from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ExcelReport(Base):
    """Tracks the single consolidated workbook's per-month state (no batch/ per-ER files)."""

    __tablename__ = "excel_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_month: Mapped[str] = mapped_column(String(7), unique=True, index=True)  # "2026-06"
    report_month_label: Mapped[str] = mapped_column(String(20))  # "June 2026"
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(1000))

    total_reports: Mapped[int] = mapped_column(default=0)
    total_expenses: Mapped[int] = mapped_column(default=0)
    total_claimed: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)

    # UP_TO_DATE | STALE | GENERATING
    status: Mapped[str] = mapped_column(String(20), default="UP_TO_DATE")
    last_updated: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
