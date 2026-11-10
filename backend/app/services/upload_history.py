"""Upload attempt history: recording, IST display values and duplicate counts (central storage phase)."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.upload_attempt import DUPLICATE, UploadAttempt

IST = timezone(timedelta(hours=5, minutes=30), "IST")  # India has no daylight-saving time, so a fixed offset is exact (and needs no tz database)


def as_utc(moment: datetime | None) -> datetime | None:
    """A naive value read back from SQLite is UTC by construction (every writer uses datetime.now(timezone.utc))."""
    if moment is None:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def to_ist_iso(moment: datetime | None) -> str | None:
    value = as_utc(moment)
    return value.astimezone(IST).isoformat() if value is not None else None


def client_identifier(request) -> str | None:
    """Where the upload came from: the client's own X-Client-Id header when it sends one, else its network address. Never a secret; at most 100 characters."""
    if request is None:
        return None
    header = (request.headers.get("x-client-id") or "").strip()
    if header:
        return "".join(ch for ch in header if ch.isprintable())[:100] or None
    host = getattr(getattr(request, "client", None), "host", None)
    return f"ip:{host}"[:100] if host else None


def record_attempt(db: Session, *, filename: str, user_id: int | None, size: int, sha256: str | None, document_type: str, client_id: str | None, result: str,
                   error_message: str | None = None, canonical_kind: str | None = None, canonical_pk: int | None = None, duplicate_reason: str | None = None,
                   storage_record_id: int | None = None, retained_copy: bool = False, uploaded_at: datetime | None = None) -> UploadAttempt:
    """Adds one row (the caller commits, together with whatever else belongs to the same transaction). `uploaded_at` is the SERVER's receive time."""
    attempt = UploadAttempt(
        uploaded_at_utc=uploaded_at or datetime.now(timezone.utc), original_filename=(filename or "document.pdf")[:500], uploaded_by_id=user_id, file_size=size,
        sha256=sha256, document_type=document_type, client_id=client_id, result=result, error_message=(error_message or None) and error_message[:500],
        canonical_kind=canonical_kind, canonical_pk=canonical_pk, duplicate_reason=duplicate_reason, storage_record_id=storage_record_id, retained_copy=retained_copy,
    )
    db.add(attempt)
    db.flush()
    return attempt


def duplicate_counts(db: Session, documents: list[tuple[str, int]]) -> dict[tuple[str, int], int]:
    """{(canonical_kind, canonical_pk): how many DUPLICATE attempts were recorded against it} for the given documents (one query)."""
    if not documents:
        return {}
    pks = {pk for _kind, pk in documents}
    rows = db.execute(
        select(UploadAttempt.canonical_kind, UploadAttempt.canonical_pk, func.count()).where(UploadAttempt.result == DUPLICATE, UploadAttempt.canonical_pk.in_(pks))
        .group_by(UploadAttempt.canonical_kind, UploadAttempt.canonical_pk)
    ).all()
    return {(kind, pk): count for kind, pk, count in rows}
