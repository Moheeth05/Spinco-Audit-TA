"""Central-storage bookkeeping for documents held in Google Drive. NEW tables only (created by create_all as empty tables):
no existing table or column is altered, so existing databases and code paths are unaffected.

`state` is the STORAGE state of the stored object. It is deliberately separate from the document's own processing_status /
extraction_status (which keep their current meaning): UPLOADING | STORED | ARCHIVED | MISSING_EXTERNAL | FAILED.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

UPLOADING = "UPLOADING"
STORED = "STORED"
ARCHIVED = "ARCHIVED"
MISSING_EXTERNAL = "MISSING_EXTERNAL"
FAILED = "FAILED"
ACTIVE_STATES = (STORED,)
ALL_STATES = (UPLOADING, STORED, ARCHIVED, MISSING_EXTERNAL, FAILED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DocumentStorageRecord(Base):
    __tablename__ = "document_storage"
    # The same bytes may legitimately exist as an expense report AND as a trip report, as today (dedupe is per kind).
    __table_args__ = (UniqueConstraint("kind", "sha256", name="uq_document_storage_kind_sha256"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)  # expense_report | trip_report | excel
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    original_filename: Mapped[str] = mapped_column(String(500))
    size: Mapped[int] = mapped_column()
    md5: Mapped[str] = mapped_column(String(32))  # of the bytes we intended to store; compared with Drive's checksum
    location: Mapped[str] = mapped_column(String(200))  # opaque gdrive:<id>; reserved BEFORE the upload so a crash leaves a trace
    state: Mapped[str] = mapped_column(String(20), default=UPLOADING, index=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempts: Mapped[int] = mapped_column(default=0)

    # Link to the application's document row once it exists (set by the upload flow after the file is STORED).
    document_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)  # expense | trip
    document_pk: Mapped[int | None] = mapped_column(nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    stored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    restored_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    missing_detected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class StorageEvent(Base):
    """Append-only trail of storage operations and reconciliation findings. Never holds file content, credentials or Drive ids."""

    __tablename__ = "storage_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[int | None] = mapped_column(ForeignKey("document_storage.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    detail: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class DriveSyncState(Base):
    __tablename__ = "drive_sync_state"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)  # 'changes'
    page_token: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
