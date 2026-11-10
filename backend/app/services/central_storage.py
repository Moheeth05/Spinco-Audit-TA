"""Application-level CENTRAL STORAGE service: the one place (besides the admin storage router) where the document workflows (upload, list, preview/download,
extraction, delete) meet the Google Drive foundation. It does not rebuild any of it: the guarded client, the checksum-verified streaming, the compare-and-swap leases
and the `document_storage` records are used as they are, and it uses ONLY the checked interface (StorageManager, verified streaming / verified retrieval) - never the
unchecked metadata view or the raw client (tests/boundary_scan.py enforces that for this module exactly as for the admin router).

MODES (SPINCO_CENTRAL_STORAGE_MODE, default `off`; an unknown value is refused and treated as `off`, with the error reported to administrators):
  off     the application behaves exactly as before: local storage only, nothing here is called with any effect.
  shadow  LOCAL storage stays authoritative. After a successful local upload the original is also copied to Drive by a durable background job. A Drive failure never
          fails a local upload; the copy's state (PENDING / STORED / FAILED) is exposed.
  serve   CENTRAL Drive is authoritative. An upload is reported successful only after the verified Drive copy exists; listing, preview and download use the central
          record. If the central file is missing or Drive is unavailable the caller gets an EXPLICIT error. Another machine's local copy is never substituted.
          The backend keeps a local working copy ONLY as a cache for OCR/extraction (it can always be rebuilt from Drive).

Google credentials exist only in the backend's environment (read by app.services.drive.runtime); nothing here returns a Drive id, URL, path or credential.
"""

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import engine
from app.models import storage_record as sr
from app.models.storage_record import DocumentStorageRecord
from app.services.drive import runtime as drive_runtime
from app.services.drive import schema as drive_schema
from app.services.drive.auth import DriveAuthError
from app.services.drive.client import DriveError, DriveIntegrityError, DriveNotFound
from app.services.drive.config import DriveConfigError, load_runtime_config
from app.services.drive.errors import DriveForeignObject, DriveIdentityMismatch, DriveNotADocument
from app.services.drive.lifecycle import StorageManager, StorageStateError, StorageUploadError
from app.services.upload_history import as_utc

log = logging.getLogger("app.central_storage")

OFF, SHADOW, SERVE = "off", "shadow", "serve"
MODES = (OFF, SHADOW, SERVE)
MODE_ENV = "SPINCO_CENTRAL_STORAGE_MODE"
RETENTION_ENV = "SPINCO_DUPLICATE_RETENTION"  # metadata (default: the attempt is recorded, no second PDF is stored) | copy (also keep the duplicate's bytes in Drive "Duplicates")
RETENTION_METADATA, RETENTION_COPY = "metadata", "copy"

DOC_KIND_TO_STORAGE = {"expense": "expense_report", "trip": "trip_report"}
DUPLICATE_KIND = "duplicate"

# What the application shows for a document's central copy.
NOT_CENTRAL, PENDING, STORED, FAILED, ARCHIVED, MISSING_EXTERNAL = "NOT_CENTRAL", "PENDING", "STORED", "FAILED", "ARCHIVED", "MISSING_EXTERNAL"
_STATE_LABELS = {sr.STORED: STORED, sr.UPLOADING: PENDING, sr.FAILED: FAILED, sr.ARCHIVED: ARCHIVED, sr.MISSING_EXTERNAL: MISSING_EXTERNAL}

GENERIC_UNAVAILABLE = "Central storage is temporarily unavailable. Nothing was saved; please try again shortly."
NOT_CONFIGURED = "Central storage is not available. Contact an administrator."


@dataclass(frozen=True)
class CentralConfig:
    mode: str
    duplicate_retention: str
    error: str | None = None  # set when a setting was invalid (and was therefore replaced by its safe default)

    @property
    def active(self) -> bool:
        return self.mode != OFF


def config(env=None) -> CentralConfig:
    env = os.environ if env is None else env
    errors = []
    raw_mode = str(env.get(MODE_ENV, "")).strip().lower()
    mode = raw_mode or OFF
    if mode not in MODES:
        errors.append(f"{MODE_ENV} must be one of: {', '.join(MODES)}")
        mode = OFF  # fail safe: an unreadable setting never switches central storage ON
    raw_retention = str(env.get(RETENTION_ENV, "")).strip().lower()
    retention = raw_retention or RETENTION_METADATA
    if retention not in (RETENTION_METADATA, RETENTION_COPY):
        errors.append(f"{RETENTION_ENV} must be one of: {RETENTION_METADATA}, {RETENTION_COPY}")
        retention = RETENTION_METADATA
    return CentralConfig(mode=mode, duplicate_retention=retention, error="; ".join(errors) or None)


class CentralUnavailable(Exception):
    """Central storage cannot serve this request. `detail` is safe to show to any user (no id, URL, path or credential); `status` is the HTTP status the caller
    should answer with; `retryable` says whether the same request may succeed later."""

    def __init__(self, detail: str, *, status: int = 503, retryable: bool = True):
        super().__init__(detail)
        self.detail, self.status, self.retryable = detail, status, retryable


def _from_http(exc: HTTPException) -> CentralUnavailable:
    log.warning("Central storage is not available: %s", str(exc.detail)[:200])  # the detail may name a setting; it is for the log, not for users
    return CentralUnavailable(NOT_CONFIGURED, status=503, retryable=False)


def manager() -> StorageManager:
    """The process-wide guarded storage manager, or CentralUnavailable (Drive off, misconfigured, schema not ready, credentials unusable)."""
    try:
        drive_runtime.check_schema_ready(detailed=False)
        central = drive_schema.central_schema_report(engine)
        if not central.ready:
            log.warning("Central storage refused: central tables %s", central.status)
            raise CentralUnavailable(NOT_CONFIGURED, status=503, retryable=False)
        return drive_runtime.get_storage_manager()
    except HTTPException as exc:
        raise _from_http(exc) from None


def map_drive_error(exc: BaseException) -> CentralUnavailable:
    """One mapping from a Drive/sign-in failure to what a user may be told (never the underlying text)."""
    if isinstance(exc, (DriveIntegrityError, DriveIdentityMismatch)):
        return CentralUnavailable("The stored file failed verification and was not served.", status=502, retryable=False)
    if isinstance(exc, (DriveNotFound, DriveForeignObject, DriveNotADocument)):
        return CentralUnavailable("The central copy of this document could not be found.", status=404, retryable=False)
    if isinstance(exc, (DriveConfigError, DriveAuthError)):
        return CentralUnavailable(GENERIC_UNAVAILABLE, status=503, retryable=not isinstance(exc, DriveConfigError))
    if isinstance(exc, DriveError):
        return CentralUnavailable(GENERIC_UNAVAILABLE, status=503, retryable=True)
    return CentralUnavailable(GENERIC_UNAVAILABLE, status=503, retryable=True)


def drive_configured(env=None) -> bool:
    """Whether the Drive settings of this process are complete and consistent (environment, expected Shared Drive, credential files). No network call, no credential is read
    or returned: used by the readiness endpoint, which reports only the boolean."""
    try:
        load_runtime_config(dict(os.environ if env is None else env))
        return True
    except Exception:  # noqa: BLE001 - the reason (a setting name) belongs in the configuration checker, not in an unauthenticated answer
        return False


def schema_problems() -> list[str]:
    """What is wrong with the Drive / central tables of THIS database, read-only (empty list = ready). Used by the start-up check; it never changes anything."""
    drive = drive_schema.inspect_drive_schema(engine)
    central = drive_schema.inspect_drive_schema(engine, drive_schema.CENTRAL_TABLES)
    return ([f"Drive tables: {drive.status}"] if not drive.ready else []) + ([f"central tables: {central.status}"] if not central.ready else [])


def status_details() -> dict:
    """Administrator-only facts for GET /api/central/status: whether the schema gates are open."""
    return {"storage_ready": drive_schema.schema_report(engine).ready and drive_schema.central_schema_report(engine).ready}


# ----------------------------------------------------------------------------------------------------------------------------------------------- state


def label_for(record: DocumentStorageRecord | None) -> str:
    return _STATE_LABELS.get(record.state, FAILED) if record is not None else NOT_CENTRAL


def lookup(db: Session, doc_kind: str, sha256: str | None) -> DocumentStorageRecord | None:
    if not sha256 or doc_kind not in DOC_KIND_TO_STORAGE:
        return None
    return db.execute(select(DocumentStorageRecord).where(DocumentStorageRecord.kind == DOC_KIND_TO_STORAGE[doc_kind], DocumentStorageRecord.sha256 == sha256)).scalars().first()


def storage_labels(db: Session, documents: list[tuple[str, str | None]]) -> dict[tuple[str, str], str]:
    """{(doc_kind, sha256): label} for a whole list in ONE query. Everything is NOT_CENTRAL when central storage is off."""
    out = {(kind, sha): NOT_CENTRAL for kind, sha in documents if sha}
    if not config().active or not out:
        return out
    shas = {sha for _kind, sha in out}
    rows = db.execute(select(DocumentStorageRecord.kind, DocumentStorageRecord.sha256, DocumentStorageRecord.state).where(DocumentStorageRecord.sha256.in_(shas))).all()
    by_storage_kind = {v: k for k, v in DOC_KIND_TO_STORAGE.items()}
    for storage_kind, sha, state in rows:
        doc_kind = by_storage_kind.get(storage_kind)
        if doc_kind and (doc_kind, sha) in out:
            out[(doc_kind, sha)] = _STATE_LABELS.get(state, FAILED)
    return out


# --------------------------------------------------------------------------------------------------------------------------------------------- uploading


@dataclass
class StoredResult:
    label: str  # STORED | PENDING | FAILED | ARCHIVED | MISSING_EXTERNAL
    record: DocumentStorageRecord | None
    message: str | None = None  # safe to show
    retryable: bool = True


def store_document(db: Session, doc_kind: str, content: bytes, filename: str, user_id: int | None) -> StoredResult:
    """Persists the original in Drive through the existing StorageManager (which reserves the id, records the intent, uploads, verifies size and md5, and only then
    marks the record STORED). Idempotent: the same bytes never create a second record or a second Drive file. Raises CentralUnavailable when central storage as a whole
    is not usable; returns a StoredResult otherwise (a failed attempt leaves a PENDING/FAILED record that the retry machinery finishes)."""
    mgr = manager()
    try:
        result = mgr.store(db, kind=DOC_KIND_TO_STORAGE[doc_kind], content=content, original_filename=filename, user_id=user_id)
    except StorageUploadError as exc:
        label = label_for(exc.record)
        if exc.record is None:
            return StoredResult(PENDING, None, GENERIC_UNAVAILABLE, retryable=True)
        return StoredResult(label, exc.record, GENERIC_UNAVAILABLE if exc.retryable else "Central storage could not accept this file.", retryable=exc.retryable)
    except (DriveError, DriveAuthError) as exc:
        raise map_drive_error(exc) from None
    record = settle(db, result.record)
    if record.state == sr.ARCHIVED:  # the same bytes were archived earlier (the document was deleted): an authorised re-upload brings the central copy back
        try:
            record = mgr.restore(db, record.id, user_id=user_id)
        except StorageStateError as exc:
            return StoredResult(ARCHIVED, record, str(exc)[:300], retryable=False)
        except (DriveError, DriveAuthError) as exc:
            raise map_drive_error(exc) from None
    if record.state == sr.MISSING_EXTERNAL:
        return StoredResult(MISSING_EXTERNAL, record, "The central copy of this document is missing. Contact an administrator.", retryable=False)
    return StoredResult(label_for(record), record, None if record.state == sr.STORED else GENERIC_UNAVAILABLE, retryable=record.state != sr.FAILED)


CONVERGE_WAIT_SECONDS = 20.0  # how long a second uploader of the SAME bytes waits for the first one's in-flight Drive upload before answering "not yet"
CONVERGE_POLL_SECONDS = 0.25


def settle(db: Session, record: DocumentStorageRecord, *, timeout: float | None = None) -> DocumentStorageRecord:
    """If the same bytes are being uploaded RIGHT NOW by another request (a live lease), wait for that upload instead of reporting a false failure: two PCs sending the
    same file at the same moment converge on the one canonical Drive file, and the loser is told it was a duplicate. Nothing is started twice (the lease belongs to the
    other request); an UPLOADING record nobody holds (waiting for its schedule) is returned at once."""
    deadline = time.monotonic() + (CONVERGE_WAIT_SECONDS if timeout is None else timeout)
    while True:
        db.expire(record)
        db.refresh(record)
        held = record.lease_owner is not None and record.lease_expires_at is not None and as_utc(record.lease_expires_at) > datetime.now(timezone.utc)
        if record.state != sr.UPLOADING or not held or time.monotonic() >= deadline:
            return record
        time.sleep(CONVERGE_POLL_SECONDS)


EXPORT_KIND = "excel"  # the Drive folder "Excel Exports"


def store_export(db: Session, content: bytes, filename: str, user_id: int | None) -> str:
    """Keeps a copy of a generated Excel workbook in the Drive folder "Excel Exports" (one Drive file per distinct content; an unchanged re-export adds nothing).
    The workbook itself is generated, previewed and downloaded by the CENTRAL backend, so every client already sees the same one - this adds the durable,
    backed-up copy. Best effort and never raises: returns the state label (STORED / PENDING / FAILED), or NOT_CENTRAL when central storage is off."""
    if not config().active:
        return NOT_CENTRAL
    try:
        result = manager().store(db, kind=EXPORT_KIND, content=content, original_filename=filename, user_id=user_id)
        return label_for(settle(db, result.record))
    except CentralUnavailable:
        return FAILED
    except (StorageUploadError, DriveError, DriveAuthError):
        return PENDING
    except Exception:  # noqa: BLE001 - archiving an export must never break the workbook the user just generated
        log.exception("Storing the Excel export centrally failed")
        return FAILED


def retain_duplicate_copy(db: Session, content: bytes, filename: str, user_id: int | None) -> DocumentStorageRecord | None:
    """When the retention policy is `copy`: keeps the duplicate's bytes in the Drive "Duplicates" folder (one retained copy per distinct bytes, however many
    times they are re-uploaded). Best effort: any failure returns None and the attempt is still recorded (as metadata). Never touches the canonical original."""
    try:
        mgr = manager()
        result = mgr.store(db, kind=DUPLICATE_KIND, content=content, original_filename=filename, user_id=user_id)
        return result.record if result.record.state == sr.STORED else None
    except (CentralUnavailable, StorageUploadError, DriveError, DriveAuthError):
        return None
    except Exception:  # noqa: BLE001 - a duplicate's retention must never break the upload that is being answered
        log.exception("Retaining a duplicate copy failed")
        return None


# --------------------------------------------------------------------------------------------------------------------------------------------- serving


def serve_response(db: Session, doc_kind: str, row, range_header: str | None) -> StreamingResponse:
    """The verified PDF of a document, from its central record only. Every failure is an explicit CentralUnavailable (404 / 410 / 502 / 503); a local copy is NEVER
    substituted. Authorisation must already have happened. The whole file is downloaded and verified against the RECORDED checksum and size before the first byte is sent."""
    record = lookup(db, doc_kind, row.file_hash)
    if record is None:
        raise CentralUnavailable("This document is not stored in central storage.", status=404, retryable=False)
    if record.state in (sr.ARCHIVED, sr.MISSING_EXTERNAL):
        raise CentralUnavailable("This document is not available in central storage (archived or missing). Contact an administrator.", status=410, retryable=False)
    if record.state != sr.STORED:
        raise CentralUnavailable("The central copy of this document is not ready yet.", status=503, retryable=True)
    mgr = manager()
    context = mgr.drive.open_stream(record.location, expected_md5=record.md5, expected_size=record.size, range_header=range_header)
    try:
        download = context.__enter__()
    except (DriveError, DriveAuthError) as exc:
        raise map_drive_error(exc) from None

    def body():
        try:
            yield from download.iter_bytes()
        finally:
            context.__exit__(None, None, None)

    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for name in ("Content-Range", "Content-Length"):
        for key, value in download.headers.items():
            if key.lower() == name.lower():
                headers[name] = value
    return StreamingResponse(body(), status_code=download.status, media_type="application/pdf", headers=headers)


# ----------------------------------------------------------------------------------------------------------------- local working copy for OCR / extraction

_hydrate_guard = threading.Lock()
_hydrate_locks: dict[tuple[str, int], threading.Lock] = {}


def _lock_for(key: tuple[str, int]) -> threading.Lock:
    with _hydrate_guard:
        return _hydrate_locks.setdefault(key, threading.Lock())


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def local_file_for(db: Session, doc_kind: str, row) -> Path:
    """The PDF as a LOCAL file for libraries that need a real path (OCR, PyMuPDF), obtained through the storage layer instead of assuming that this machine holds it.
      off      the recorded local path, exactly as before (FileNotFoundError if it is gone);
      shadow   the local original (authoritative); if it is gone and Drive holds a verified copy, that copy is brought back;
      serve    the local working copy only if it matches the recorded SHA-256; otherwise it is rebuilt from the verified Drive copy.
    A failure to obtain the file raises FileNotFoundError (off) or CentralUnavailable (central modes) - the caller classifies it as retryable or permanent."""
    cfg = config()
    path = Path(row.source_path) if row.source_path else None
    if not cfg.active:
        if path is None or not path.exists():
            raise FileNotFoundError("Stored PDF file could not be found.")
        return path
    if path is not None and path.exists():
        if cfg.mode == SHADOW:
            return path
        try:
            if _sha256_of(path) == row.file_hash:
                return path
        except OSError:
            pass  # unreadable cache: rebuilt below
    return _hydrate(db, doc_kind, row, path)


def _hydrate(db: Session, doc_kind: str, row, path: Path | None) -> Path:
    key = (doc_kind, row.id)
    with _lock_for(key):
        if path is not None and path.exists() and _sha256_of(path) == row.file_hash:
            return path  # another thread rebuilt it while this one waited
        record = lookup(db, doc_kind, row.file_hash)
        if record is None or record.state != sr.STORED:
            raise CentralUnavailable("The central copy of this document is not available.", status=404 if record is None else 503, retryable=record is not None and record.state == sr.UPLOADING)
        mgr = manager()
        try:
            data = mgr.drive.retrieve(record.location, expected_md5=record.md5, expected_size=record.size)
        except (DriveError, DriveAuthError) as exc:
            raise map_drive_error(exc) from None
        if hashlib.sha256(data).hexdigest() != row.file_hash:
            raise CentralUnavailable("The stored file failed verification and was not used.", status=502, retryable=False)
        target = path
        try:
            if target is None:
                raise OSError("no recorded path")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                temporary.write_bytes(data)
                os.replace(temporary, target)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        except OSError:  # the recorded location is not usable on THIS machine (e.g. the database was moved): rebuild in this machine's document folder
            from app.services.storage_backend import storage as local_storage

            row.source_path = local_storage.save(DOC_KIND_TO_STORAGE[doc_kind], row.file_hash, row.original_filename or "document.pdf", data)
            db.commit()
            target = Path(row.source_path)
        return target


# ----------------------------------------------------------------------------------------------------------------------------------------- delete / archive


def archive_document(db: Session, doc_kind: str, sha256: str | None, user_id: int | None) -> str:
    """Archives (reversibly moves) the central copy of a document that is being deleted. Returns the new label. Raises CentralUnavailable if Drive refuses; a document with no
    central record is a no-op (NOT_CENTRAL)."""
    record = lookup(db, doc_kind, sha256)
    if record is None or record.state in (sr.ARCHIVED, sr.MISSING_EXTERNAL):
        return label_for(record)
    mgr = manager()
    try:
        return label_for(mgr.archive(db, record.id, user_id=user_id))
    except StorageStateError as exc:
        raise CentralUnavailable(str(exc)[:300], status=409, retryable=False) from None
    except (DriveError, DriveAuthError) as exc:
        raise map_drive_error(exc) from None


def restore_document(db: Session, doc_kind: str, sha256: str | None, user_id: int | None) -> None:
    record = lookup(db, doc_kind, sha256)
    if record is not None and record.state == sr.ARCHIVED:
        try:
            manager().restore(db, record.id, user_id=user_id)
        except (StorageStateError, DriveError, DriveAuthError, CentralUnavailable):
            pass
