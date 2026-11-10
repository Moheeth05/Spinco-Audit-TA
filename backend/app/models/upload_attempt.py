"""Every upload attempt, successful or not (central storage phase). One row per file in an upload request:

  SUCCESS   the document was created (in `serve` mode: already stored centrally);
  DUPLICATE identical bytes already exist as a document of the same type - NO new document is created, the attempt is recorded against the original;
  FAILED    rejected (not a valid PDF, too large, permanent storage problem);
  DEFERRED  in `serve` mode central storage was temporarily unavailable: nothing was created, the user may retry.

`uploaded_at_utc` is the authoritative moment the server received the upload (never a file's modified time); the IST display value is derived from it.
The Drive file itself is referenced through `storage_record_id` (-> document_storage, which holds the opaque Drive location and checksums); the Drive id is never
copied into this table or exposed through the API.
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

SUCCESS = "SUCCESS"
DUPLICATE = "DUPLICATE"
FAILED = "FAILED"
DEFERRED = "DEFERRED"
RESULTS = (SUCCESS, DUPLICATE, FAILED, DEFERRED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UploadAttempt(Base):
    __tablename__ = "upload_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    uploaded_at_utc: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    original_filename: Mapped[str] = mapped_column(String(500))
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    file_size: Mapped[int] = mapped_column(default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # NULL only when the bytes were rejected before they could be hashed
    document_type: Mapped[str] = mapped_column(String(20))  # expense_report | trip_report
    client_id: Mapped[str | None] = mapped_column(String(100), nullable=True)  # the client that sent it (X-Client-Id header, else its network address)
    result: Mapped[str] = mapped_column(String(12), index=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # The canonical (original) document: for SUCCESS the document just created, for DUPLICATE the one that already existed.
    canonical_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)  # expense | trip
    canonical_pk: Mapped[int | None] = mapped_column(nullable=True, index=True)
    duplicate_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)  # identical_sha256
    storage_record_id: Mapped[int | None] = mapped_column(ForeignKey("document_storage.id"), nullable=True, index=True)  # the Drive record involved
    retained_copy: Mapped[bool] = mapped_column(Boolean, default=False)  # True when the duplicate's bytes were kept in the Drive "Duplicates" folder
