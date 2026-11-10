"""Durable background work for centrally stored documents (central storage phase).

The previous mechanism (FastAPI BackgroundTasks) lives only in the memory of one process: a restart lost the work and left the document's extraction status stuck
on PROCESSING. A job is a row; its state, attempts, lease and schedule are in the database, so a restart, a crashed worker or a second process all recover by the
same compare-and-swap lease rules as the Drive uploads:

  EXTRACT       run the existing OCR / extraction / audit pipeline on the document (its PDF is fetched through the central storage layer);
  SHADOW_COPY   send the local original to Google Drive (mode `shadow`).

States: QUEUED -> RUNNING -> DONE | DEFERRED (waiting to retry: outage, authentication, ...) | FAILED (permanent; the original PDF is never lost).
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

QUEUED, RUNNING, DONE, DEFERRED, FAILED = "QUEUED", "RUNNING", "DONE", "DEFERRED", "FAILED"
EXTRACT, SHADOW_COPY = "EXTRACT", "SHADOW_COPY"
STATES = (QUEUED, RUNNING, DONE, DEFERRED, FAILED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (UniqueConstraint("document_kind", "document_pk", "job_type", name="uq_processing_jobs_document_type"),)  # one job per document and kind of work

    id: Mapped[int] = mapped_column(primary_key=True)
    document_kind: Mapped[str] = mapped_column(String(10))  # expense | trip
    document_pk: Mapped[int] = mapped_column()
    job_type: Mapped[str] = mapped_column(String(12))
    state: Mapped[str] = mapped_column(String(12), default=QUEUED, index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    failure_class: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(40), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
