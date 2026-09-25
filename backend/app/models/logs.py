from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProcessingLog(Base):
    """Pipeline-stage log entries for uploaded files (queued/processing/processed/failed)."""

    __tablename__ = "processing_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_report_id: Mapped[int | None] = mapped_column(ForeignKey("expense_reports.id"), nullable=True, index=True)
    trip_report_id: Mapped[int | None] = mapped_column(ForeignKey("trip_reports.id"), nullable=True, index=True)
    filename: Mapped[str] = mapped_column(String(500))

    stage: Mapped[str] = mapped_column(String(100))
    # QUEUED | PROCESSING | PROCESSED | EXCEPTION_FOUND | FAILED
    status: Mapped[str] = mapped_column(String(30))
    message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AuditLog(Base):
    """Records admin/auditor actions (manual corrections, policy edits, reprocessing) for traceability."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    details: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
