"""Storage lifecycle for documents held in Google Drive: reliable store, archive, restore and reconciliation.

Invariants (each is covered by tests):
  * A document is never reported STORED before Drive has confirmed the bytes (checksum AND size verified).
  * The intent record (state UPLOADING, Drive id reserved) is committed BEFORE the upload, so a crash leaves a trace the reconciler acts on.
  * The same bytes never create a second Drive file: the database unique key, the reserved id and a SHA-256 lookup all converge.
  * Nothing here deletes anything. Archive and restore are reversible moves; an external deletion only flips the state to
    MISSING_EXTERNAL; extraction, exception, verification and audit history are never touched (they live in other tables).
  * Every inconsistency becomes a visible state or an event; none disappears silently, and none is "fixed" by guessing.
"""

import hashlib
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import storage_record as sr
from app.models.storage_record import DocumentStorageRecord, DriveSyncState, StorageEvent
from app.services import audit
from app.services.drive.auth import DriveAuthError
from app.services.drive.client import DriveChangeTokenExpired, DriveError, DriveIntegrityError, DriveNotFound, DriveObject
from app.services.drive.storage import GoogleDriveStorage, make_location, parse_location
from app.services.storage import sanitize_filename

STALE_UPLOAD_SECONDS = 300


class StorageStateError(Exception):
    """The requested operation is not valid for the document's current storage state."""


class StorageUploadError(Exception):
    """The upload did not complete. `record` shows where it stands (UPLOADING = recoverable, FAILED = needs attention). `record` is
    None only when Drive could not even reserve a file id: then nothing at all was recorded or sent, and the caller simply retries."""

    def __init__(self, message: str, record: DocumentStorageRecord | None, retryable: bool):
        super().__init__(message)
        self.record = record
        self.retryable = retryable


@dataclass
class StoreResult:
    record: DocumentStorageRecord
    duplicate: bool = False  # the bytes were already known: no second Drive file was created
    created: bool = False


@dataclass
class ReconcileReport:
    adopted_uploads: int = 0
    reuploaded: int = 0
    failed_uploads: int = 0
    missing_external: int = 0
    recovered_externally: int = 0
    moved_externally: int = 0
    renamed_externally: int = 0
    content_changed_externally: int = 0
    orphans: int = 0
    checked: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChangesReport:
    baseline: bool = False
    changes_seen: int = 0
    tracked_changes: int = 0
    token_expired: bool = False
    reconcile: dict | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _naive(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


def _safe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


class StorageManager:
    def __init__(self, drive: GoogleDriveStorage, *, spool_dir: Path | None = None, now=_now):
        self.drive = drive
        self._spool_dir = Path(spool_dir) if spool_dir else None
        self._now = now

    # ------------------------------------------------------------------ small helpers

    def _event(self, db: Session, record: DocumentStorageRecord | None, event_type: str, detail: str | None = None, actor_id: int | None = None) -> None:
        db.add(StorageEvent(record_id=record.id if record is not None else None, event_type=event_type, detail=(detail or "")[:1000] or None, actor_id=actor_id))
        db.flush()

    def _event_once(self, db: Session, record: DocumentStorageRecord | None, event_type: str, detail: str) -> bool:
        query = select(StorageEvent.id).where(StorageEvent.event_type == event_type, StorageEvent.detail == detail)
        query = query.where(StorageEvent.record_id == (record.id if record is not None else None)) if record is not None else query.where(StorageEvent.record_id.is_(None))
        if db.execute(query.limit(1)).first() is not None:
            return False
        self._event(db, record, event_type, detail)
        return True

    @staticmethod
    def _get(db: Session, kind: str, sha: str) -> DocumentStorageRecord | None:
        return db.execute(select(DocumentStorageRecord).where(DocumentStorageRecord.kind == kind, DocumentStorageRecord.sha256 == sha)).scalars().first()

    def _spool_path(self, record: DocumentStorageRecord) -> Path | None:
        return self._spool_dir / f"{record.kind}_{record.sha256}.part" if self._spool_dir else None

    def _spool(self, record: DocumentStorageRecord, content: bytes) -> None:
        path = self._spool_path(record)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(content)
        os.replace(temporary, path)

    def _unspool(self, record: DocumentStorageRecord) -> None:
        path = self._spool_path(record)
        if path is not None:
            path.unlink(missing_ok=True)

    def _spooled(self, record: DocumentStorageRecord) -> bytes | None:
        path = self._spool_path(record)
        if path is not None and path.is_file():
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() == record.sha256:
                return data
        return None

    # ------------------------------------------------------------------ store

    def store(self, db: Session, *, kind: str, content: bytes, original_filename: str, user_id: int | None = None) -> StoreResult:
        """Stores the bytes in Drive. Returns only when the record is STORED or a duplicate of one that exists; raises
        StorageUploadError otherwise (the record then shows UPLOADING for a retry/reconcile, or FAILED)."""
        sha = hashlib.sha256(content).hexdigest()
        existing = self._get(db, kind, sha)
        if existing is not None:
            return self._from_existing(db, existing, content)

        try:
            reserved = self.drive.reserve_id()
        except (DriveError, DriveAuthError) as exc:
            raise StorageUploadError("Drive could not be reached to accept the upload; nothing was recorded.", None, retryable=isinstance(exc, DriveError) and exc.retryable) from exc
        record = DocumentStorageRecord(
            kind=kind, sha256=sha, original_filename=(original_filename or "document.pdf")[:500], size=len(content),
            md5=hashlib.md5(content).hexdigest(), location=make_location(reserved), state=sr.UPLOADING, created_by_id=user_id,
        )
        db.add(record)
        try:
            db.commit()  # the intent is durable BEFORE any byte is sent
        except IntegrityError:
            db.rollback()
            winner = self._get(db, kind, sha)
            if winner is None:
                raise
            return self._from_existing(db, winner, content)
        self._spool(record, content)
        self._event(db, record, "UPLOAD_STARTED", actor_id=user_id)
        db.commit()
        self._upload_now(db, record, content)
        return StoreResult(record, duplicate=False, created=True)

    def _from_existing(self, db: Session, record: DocumentStorageRecord, content: bytes) -> StoreResult:
        if record.state in (sr.UPLOADING, sr.FAILED):
            # Same bytes, earlier attempt unfinished: finish it (idempotent: same reserved id, so no second file can result).
            record.state = sr.UPLOADING
            db.commit()
            self._upload_now(db, record, content)
        return StoreResult(record, duplicate=True, created=False)

    def _upload_now(self, db: Session, record: DocumentStorageRecord, content: bytes) -> None:
        try:
            outcome = self.drive.upload(record.kind, record.sha256, record.original_filename, content, reserved_id=parse_location(record.location))
        except DriveIntegrityError as exc:
            record.state, record.last_error, record.attempts = sr.FAILED, _safe(exc), record.attempts + 1
            self._event(db, record, "UPLOAD_INTEGRITY_FAILED", _safe(exc))
            db.commit()
            raise StorageUploadError("Drive did not confirm the uploaded bytes; the file was quarantined.", record, retryable=False) from exc
        except (DriveError, DriveAuthError) as exc:
            retryable = isinstance(exc, DriveError) and exc.retryable
            record.attempts += 1
            record.last_error = _safe(exc)
            if not retryable:
                record.state = sr.FAILED
            self._event(db, record, "UPLOAD_ATTEMPT_FAILED", _safe(exc))
            db.commit()
            raise StorageUploadError("The upload to Drive did not complete.", record, retryable=retryable) from exc
        record.location = outcome.location  # differs from the reserved id only when identical bytes already existed in Drive
        record.state, record.last_error = sr.STORED, None
        record.stored_at = record.last_verified_at = self._now()
        self._event(db, record, "DEDUP_ADOPTED" if outcome.deduplicated else ("ADOPTED_EARLIER_ATTEMPT" if outcome.adopted else "STORED"))
        db.commit()
        self._unspool(record)

    def link_document(self, db: Session, record_id: int, document_kind: str, document_pk: int) -> None:
        record = db.get(DocumentStorageRecord, record_id)
        if record is None:
            raise LookupError("storage record not found")
        record.document_kind, record.document_pk = document_kind, document_pk
        db.commit()

    # ------------------------------------------------------------------ archive / restore

    def archive(self, db: Session, record_id: int, *, user_id: int | None) -> DocumentStorageRecord:
        record = db.get(DocumentStorageRecord, record_id)
        if record is None:
            raise LookupError("storage record not found")
        if record.state == sr.ARCHIVED:
            return record  # idempotent
        if record.state != sr.STORED:
            raise StorageStateError(f"A document in state {record.state} cannot be archived.")
        self.drive.archive(record.location, record.kind)  # a failure here changes nothing in the database
        record.state, record.archived_at, record.archived_by_id = sr.ARCHIVED, self._now(), user_id
        self._event(db, record, "ARCHIVED", actor_id=user_id)
        audit.log_action(db, user_id=user_id, action=audit.DOCUMENT_ARCHIVED, entity_type="DocumentStorage", entity_id=str(record.id), details=f"kind={record.kind}")
        db.commit()
        return record

    def restore(self, db: Session, record_id: int, *, user_id: int | None) -> DocumentStorageRecord:
        record = db.get(DocumentStorageRecord, record_id)
        if record is None:
            raise LookupError("storage record not found")
        if record.state == sr.STORED:
            return record  # idempotent
        if record.state not in (sr.ARCHIVED, sr.MISSING_EXTERNAL):
            raise StorageStateError(f"A document in state {record.state} cannot be restored.")
        try:
            self.drive.restore(record.location, record.kind)
        except DriveNotFound:
            raise StorageStateError("The file no longer exists in Drive, so it cannot be restored. It must be re-uploaded.") from None
        record.state, record.restored_at, record.restored_by_id, record.missing_detected_at = sr.STORED, self._now(), user_id, None
        self._event(db, record, "RESTORED", actor_id=user_id)
        audit.log_action(db, user_id=user_id, action=audit.DOCUMENT_RESTORED, entity_type="DocumentStorage", entity_id=str(record.id), details=f"kind={record.kind}")
        db.commit()
        return record

    # ------------------------------------------------------------------ reconciliation

    def _roles(self) -> dict:
        structure = self.drive.folders.ensure_structure()
        kinds = [k for k in structure if k.startswith("active:")]
        return {
            "active": {k.split(":", 1)[1]: structure[k] for k in kinds},
            "archive": {k.split(":", 1)[1]: structure[f"archive:{k.split(':', 1)[1]}"] for k in kinds},
            "archive_root": structure["archive"],
        }

    def _apply(self, db: Session, record: DocumentStorageRecord, obj: DriveObject | None, roles: dict, report: ReconcileReport) -> None:
        now = self._now()
        report.checked += 1
        if obj is None or obj.trashed:
            if record.state in (sr.STORED, sr.ARCHIVED):
                record.state, record.missing_detected_at = sr.MISSING_EXTERNAL, now
                self._event(db, record, "EXTERNAL_DELETE_DETECTED", "trashed in Drive" if obj is not None else "no longer found in Drive")
                report.missing_external += 1
            record.last_verified_at = now
            return
        in_archive = bool(set(obj.parents) & (set(roles["archive"].values()) | {roles["archive_root"]}))
        in_active = roles["active"].get(record.kind) in obj.parents
        if record.state == sr.MISSING_EXTERNAL:
            record.state, record.missing_detected_at = (sr.ARCHIVED if in_archive else sr.STORED), None
            self._event(db, record, "RECOVERED_EXTERNALLY", "the file is available in Drive again")
            report.recovered_externally += 1
        if record.state == sr.STORED and not in_active:
            if self._event_once(db, record, "EXTERNAL_MOVE_DETECTED", "an active document is no longer in its active folder"):
                report.moved_externally += 1
        if record.state == sr.ARCHIVED and not in_archive:
            if self._event_once(db, record, "EXTERNAL_MOVE_DETECTED", "an archived document is no longer in the Archive folder"):
                report.moved_externally += 1
        if obj.md5 and obj.md5 != record.md5:
            if self._event_once(db, record, "CONTENT_CHANGED_EXTERNALLY", "the file's checksum differs from what was stored"):
                report.content_changed_externally += 1
        expected_name = f"{record.sha256[:12]}_{sanitize_filename(record.original_filename)}"
        if obj.name and obj.name != expected_name:
            if self._event_once(db, record, "NAME_DRIFT", "the file was renamed outside the application"):
                report.renamed_externally += 1
        record.last_verified_at = now

    def _complete_stale_upload(self, db: Session, record: DocumentStorageRecord, report: ReconcileReport) -> None:
        # 1. Did the interrupted attempt actually create the file? (look it up by the reserved id)
        try:
            obj = self.drive.info(record.location)
        except DriveNotFound:
            obj = None
        if obj is not None and not obj.trashed:
            if obj.md5 == record.md5 and obj.size == record.size:
                record.state, record.last_error, record.stored_at, record.last_verified_at = sr.STORED, None, self._now(), self._now()
                self._event(db, record, "ADOPTED_AFTER_INTERRUPTION", "verified checksum and size")
                db.commit()
                self._unspool(record)
                report.adopted_uploads += 1
            else:
                record.state, record.last_error = sr.FAILED, "A file exists under the reserved id but its checksum/size do not match."
                self._event(db, record, "UPLOAD_INTEGRITY_FAILED", record.last_error)
                db.commit()
                report.failed_uploads += 1
            return
        # 2. Not in Drive: retry from the spooled copy if there is one, otherwise it can only be re-uploaded by the user.
        content = self._spooled(record)
        if content is None:
            record.state, record.last_error = sr.FAILED, "The upload never reached Drive and no copy was kept; it must be uploaded again."
            self._event(db, record, "UPLOAD_ABANDONED", record.last_error)
            db.commit()
            report.failed_uploads += 1
            return
        try:
            self._upload_now(db, record, content)
            report.reuploaded += 1
        except StorageUploadError as exc:
            if not exc.retryable:
                report.failed_uploads += 1

    def reconcile(self, db: Session, *, stale_after_seconds: int = STALE_UPLOAD_SECONDS) -> ReconcileReport:
        report = ReconcileReport()
        now = _naive(self._now())
        try:
            roles = self._roles()
        except (DriveError, DriveAuthError) as exc:  # cannot even see the folder structure: judge nothing, say so, and stop
            report.errors.append(f"folder structure: {_safe(exc)}")
            audit.log_action(db, user_id=None, action=audit.STORAGE_RECONCILED, entity_type="DocumentStorage", details=str(report.as_dict())[:900])
            db.commit()
            return report

        stale = [r for r in db.execute(select(DocumentStorageRecord).where(DocumentStorageRecord.state == sr.UPLOADING)).scalars().all() if (now - _naive(r.created_at)).total_seconds() >= stale_after_seconds]
        for record in stale:
            try:
                self._complete_stale_upload(db, record, report)
            except (DriveError, DriveAuthError) as exc:
                report.errors.append(f"record {record.id}: {_safe(exc)}")
                db.rollback()

        for record in db.execute(select(DocumentStorageRecord).where(DocumentStorageRecord.state.in_((sr.STORED, sr.ARCHIVED, sr.MISSING_EXTERNAL)))).scalars().all():
            try:
                try:
                    obj = self.drive.info(record.location)
                except DriveNotFound:
                    obj = None
                self._apply(db, record, obj, roles, report)
                db.commit()
            except (DriveError, DriveAuthError) as exc:  # an outage must not read as "missing": the record is simply not judged this round
                report.errors.append(f"record {record.id}: {_safe(exc)}")
                db.rollback()

        known = {(r.kind, r.sha256) for r in db.execute(select(DocumentStorageRecord)).scalars().all()}
        for kind, folder_id in list(roles["active"].items()) + [(k, v) for k, v in roles["archive"].items()]:
            try:
                for obj in self.drive.client.find_children(folder_id):
                    sha = obj.app_properties.get("sha256")
                    if sha and (obj.app_properties.get("kind", kind), sha) not in known:
                        if self._event_once(db, None, "ORPHAN_FILE_FOUND", f"kind={kind}, sha256={sha[:12]}"):
                            report.orphans += 1
            except (DriveError, DriveAuthError) as exc:
                report.errors.append(f"orphan scan {kind}: {_safe(exc)}")
        audit.log_action(db, user_id=None, action=audit.STORAGE_RECONCILED, entity_type="DocumentStorage", details=str(report.as_dict())[:900])
        db.commit()
        return report

    # ------------------------------------------------------------------ change feed

    def sync_changes(self, db: Session) -> ChangesReport:
        report = ChangesReport()
        state = db.get(DriveSyncState, "changes")
        client = self.drive.client
        if state is None or not state.page_token:
            db.merge(DriveSyncState(name="changes", page_token=client.get_start_page_token()))
            db.commit()
            report.baseline = True
            return report
        roles = self._roles()
        tracked = {parse_location(r.location): r for r in db.execute(select(DocumentStorageRecord).where(DocumentStorageRecord.state.in_((sr.STORED, sr.ARCHIVED, sr.MISSING_EXTERNAL)))).scalars().all()}
        token = state.page_token
        inner = ReconcileReport()
        try:
            while True:
                page = client.list_changes(token)
                for change in page.get("changes", []):
                    report.changes_seen += 1
                    record = tracked.get(change.get("fileId"))
                    if record is None:
                        continue
                    report.tracked_changes += 1
                    obj = None if change.get("removed") or not change.get("file") else DriveObject.from_api(change["file"])
                    self._apply(db, record, obj, roles, inner)
                token = page.get("nextPageToken")
                if not token:
                    state.page_token = page.get("newStartPageToken") or state.page_token
                    break
            db.commit()
        except DriveChangeTokenExpired:
            db.rollback()
            report.token_expired = True
            report.reconcile = self.reconcile(db).as_dict()  # a full pass replaces what the lost feed would have told us
            state = db.get(DriveSyncState, "changes")
            state.page_token = client.get_start_page_token()
            self._event(db, None, "CHANGE_TOKEN_EXPIRED_RECOVERED", "full reconciliation performed and a new change token stored")
            db.commit()
        return report
