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
import random
import re
import secrets
import socket
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import storage_record as sr
from app.models.storage_record import DocumentStorageRecord, DriveSyncState, StorageEvent
from app.services import audit
from app.services.drive import failures as fl
from app.services.drive.auth import DriveAuthError, DriveAuthTransientError
from app.services.drive.client import DriveChangeTokenExpired, DriveError, DriveIntegrityError, DriveNotFound, DriveObject
from app.services.drive.errors import DriveDestinationMissing, DriveForeignObject, DriveNotADocument, DriveObjectTrashed
from app.services.drive.guard import DriveTargetError
from app.services.drive.storage import DriveFolderMissing, GoogleDriveStorage, is_ordinary_document, make_location, parse_location
from app.services.storage import sanitize_filename

STALE_UPLOAD_SECONDS = 300
DEFAULT_MAX_UPLOAD_ATTEMPTS = 10
DEFAULT_RETRY_BACKOFF_SECONDS = 60.0
DEFAULT_RETRY_BACKOFF_CAP_SECONDS = 3600.0
# One worker owns one pending upload at a time (a lease on its record). FIXED at 30 minutes: longer than the worst-case upload (6 HTTP tries of up to
# 120 s), so a live worker is never overtaken; short enough that a crashed worker's record is picked up the same hour (Phase 3A.2-B, decision U-6).
UPLOAD_LEASE_SECONDS = 1800.0
_HOST = (re.sub(r"[^A-Za-z0-9]", "", socket.gethostname()) or "host")[:12]
CHANGES_STATE = "changes"  # the name the change token was stored under before it was scoped to a Shared Drive (kept, never deleted)
# What an administrator is told about a document whose file is no longer available. It states only what the system really does today: submitting the
# same file again is NOT a repair (the system recognises it as the same document and returns the record unchanged), and re-creating a missing file
# automatically is not supported yet (planned for Phase 3B). Ids and paths never appear in it.
MISSING_EXTERNAL_GUIDANCE = (
    "Ask a Drive administrator whether the file can be recovered or moved back; once the ORIGINAL file is back in the expected Shared Drive, run "
    "reconciliation and the system will pick it up again (a new copy of the file is a different Drive item and is not picked up). Uploading the same file "
    "again does not replace it (the system recognises it as the same document and leaves this record unchanged), and automatic re-creation is not supported yet."
)
# A not-found while archiving/restoring that concerns a DESTINATION folder (the document itself was read fine a moment earlier).
DESTINATION_MISSING_TEXT = (
    "A Drive folder that this operation needs was not found (it may have been deleted or renamed in Drive). The document itself was not touched. "
    "Ask a Drive administrator to restore the folder if it should exist, then try again: the system looks the folder up again on the next attempt."
)
NOT_A_DOCUMENT_TEXT = "The Drive item this record points to is not an ordinary document (it is a folder, a shortcut or a Google Docs item). Contact an administrator."


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
    foreign_drive: int = 0  # documents found outside the expected Shared Drive (marked MISSING_EXTERNAL, never served, never touched in Drive)
    not_documents: int = 0  # records whose Drive item is a folder, a shortcut or a Google Docs item (marked MISSING_EXTERNAL, never served)
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
    rescoped: bool = False  # an unscoped token from before was replaced by one scoped to the expected Shared Drive, after a full reconciliation
    reconcile: dict | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _naive(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


# A filesystem path in an error text is nobody's business but the operator's. Messages are written without paths at their source (see oauth.py); this is the
# second line of defence for everything that is PERSISTED (last_error, event details): Windows drive and UNC paths and POSIX absolute paths become <path>.
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/][^\r\n'\"<>|]*)|(?:\\\\[^\s'\"<>|]+)|(?<![\w/:.])/(?:[\w.\-~]+/)+[\w.\-~]*")


def _scrub(text: str) -> str:
    return _PATH_RE.sub("<path>", text)


def _safe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {_scrub(str(exc)[:200])}"

@dataclass
class _Lease:
    """One claim of one pending upload. `owner` is unique to THIS claim (not to the process), so a worker whose lease expired and was taken over can
    never record an outcome as if it still owned the record. `settled` is set once the lease has been ended, so it is ended exactly once."""

    record_id: int
    owner: str
    settled: bool = False


class _LeaseRenewer:
    """Keeps a lease alive while its holder is legitimately working (review F12): every third of the lease it extends `lease_expires_at` with the same
    owner-checked compare-and-swap that every other statement uses, on its OWN session. If the lease was lost (taken over, or already settled) the
    update matches nothing, the renewer stops, and nothing is overwritten. A holder that really DIES stops renewing, so the lease still expires and
    the record is recovered exactly as before."""

    def __init__(self, manager: "StorageManager", bind, lease: _Lease):
        self._manager, self._bind, self._lease = manager, bind, lease
        self._interval = max(manager._lease_seconds / 3.0, 0.05)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="drive-lease-renewer", daemon=True)
        self.renewals = 0

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with Session(bind=self._bind) as session:
                    now = self._manager._nnow()
                    result = session.execute(
                        update(DocumentStorageRecord).where(
                            DocumentStorageRecord.id == self._lease.record_id, DocumentStorageRecord.lease_owner == self._lease.owner,
                            DocumentStorageRecord.state == sr.UPLOADING,
                        ).values(lease_expires_at=now + timedelta(seconds=self._manager._lease_seconds)).execution_options(synchronize_session=False)
                    )
                    session.commit()
                if result.rowcount != 1:
                    return  # the lease is gone: never fight for it
                self.renewals += 1
            except Exception:  # noqa: BLE001 - a failed renewal (e.g. a locked database) is retried at the next interval; the lease simply is not extended meanwhile
                continue

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=10)
        return False


@dataclass(frozen=True)
class _Outcome:
    """What recording a failed attempt decided."""

    failure_class: str
    retryable: bool  # the caller may try again later (or the system will): False for FAILED and for a parked (revoked-authorization) record
    failed: bool  # the record is FAILED now
    message: str
    lease_lost: bool = False


def _new_owner() -> str:
    return f"{_HOST[:8]}-{os.getpid()}-{secrets.token_hex(8)}"[:40]  # 64 random bits per claim; host and pid only help an operator read the table


class StorageManager:
    """Every method that changes a record takes a Session that IT then owns for the duration of the call: it commits (several times, each step is durable
    before the next begins) and may roll back. Callers must not keep uncommitted work of their own in the session they pass in."""

    def __init__(
        self,
        drive: GoogleDriveStorage,
        *,
        spool_dir: Path | None = None,
        now=_now,
        max_upload_attempts: int = DEFAULT_MAX_UPLOAD_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        retry_backoff_cap_seconds: float = DEFAULT_RETRY_BACKOFF_CAP_SECONDS,
        lease_seconds: float = UPLOAD_LEASE_SECONDS,
        rng=random.random,
    ):
        if max_upload_attempts < 1 or retry_backoff_seconds < 0 or retry_backoff_cap_seconds < 0 or lease_seconds <= 0:
            raise ValueError("invalid upload retry settings")
        self.drive = drive
        self._spool_dir = Path(spool_dir) if spool_dir else None
        self._now = now
        self._max_attempts = max_upload_attempts
        self._backoff = retry_backoff_seconds
        self._backoff_cap = retry_backoff_cap_seconds
        self._lease_seconds = float(lease_seconds)
        self._rng = rng  # jitter source: returns a float in [0, 1)

    def _nnow(self) -> datetime:
        return _naive(self._now())

    # ------------------------------------------------------------------ retry accounting (Phase 3A.2-B)
    #
    # Every change of a pending upload's retry state is ONE atomic UPDATE ... WHERE (compare-and-swap), so correctness never depends on in-process locks:
    # several API processes, duplicate requests and crashed workers are all handled by the database deciding, per statement, who wins.
    #   claim      UPLOADING + lease free/expired + due + budget left  ->  lease held by a fresh owner token, attempts + 1 (the attempt is CHARGED up front,
    #              so a worker that crashes without recording an outcome cannot loop forever on a poison document)
    #   settle     owner-checked: success and deferrals REFUND the charge, a genuine transient failure KEEPS it, permanent/integrity failures keep it and
    #              end in FAILED; a worker that lost its lease records nothing
    #   reset      FAILED -> UPLOADING with a fresh budget (manual retry / the same file submitted again); exactly one concurrent request wins

    def _claim(self, db: Session, record_id: int, *, manual: bool, commit: bool = True) -> _Lease | None:
        """Takes the record's lease for one attempt, or returns None. `manual` (an administrator, or the same file submitted again) skips the schedule and
        the parked state - never the lease, the state check or the budget."""
        now = self._nnow()
        owner = _new_owner()
        conditions = [
            DocumentStorageRecord.id == record_id,
            DocumentStorageRecord.state == sr.UPLOADING,
            DocumentStorageRecord.attempts < self._max_attempts,
            or_(DocumentStorageRecord.lease_expires_at.is_(None), DocumentStorageRecord.lease_expires_at < now),  # free, or expired: may be taken over
        ]
        if not manual:
            conditions.append(or_(DocumentStorageRecord.next_attempt_at.is_(None), DocumentStorageRecord.next_attempt_at <= now))
            conditions.append(or_(DocumentStorageRecord.failure_class.is_(None), DocumentStorageRecord.failure_class != fl.AUTH_PERMANENT))
        result = db.execute(
            update(DocumentStorageRecord).where(*conditions).values(
                lease_owner=owner, lease_expires_at=now + timedelta(seconds=self._lease_seconds), last_attempt_at=now, attempts=DocumentStorageRecord.attempts + 1
            ).execution_options(synchronize_session=False)
        )
        if commit:
            db.commit()  # the lease is durable BEFORE any byte moves
        return _Lease(record_id, owner) if result.rowcount == 1 else None

    def _unclaimed_reason(self, db: Session, record: DocumentStorageRecord) -> str:
        """Why a claim did not succeed: not_pending | busy | exhausted | parked | not_due."""
        db.refresh(record)
        now = self._nnow()
        if record.state != sr.UPLOADING:
            return "not_pending"
        if record.lease_expires_at is not None and record.lease_expires_at >= now:
            return "busy"
        if record.attempts >= self._max_attempts:
            return "exhausted"
        if record.failure_class == fl.AUTH_PERMANENT:
            return "parked"
        if record.next_attempt_at is not None and record.next_attempt_at > now:
            return "not_due"
        return "busy"  # lost a race that finished before we looked

    def _abandon_exhausted(self, db: Session, record: DocumentStorageRecord) -> bool:
        """The retry budget is used up and nobody holds the record: it ends in FAILED (bytes, history and spool kept). One winner per record."""
        db.refresh(record)
        attempts, last = record.attempts, record.last_error or "unknown"
        text = f"Upload abandoned after {attempts} failed attempts (limit {self._max_attempts}); last error: {last}"[:500]
        result = db.execute(
            update(DocumentStorageRecord).where(
                DocumentStorageRecord.id == record.id, DocumentStorageRecord.state == sr.UPLOADING, DocumentStorageRecord.attempts >= self._max_attempts,
                or_(DocumentStorageRecord.lease_expires_at.is_(None), DocumentStorageRecord.lease_expires_at < self._nnow()),
            ).values(state=sr.FAILED, failure_class=fl.TRANSIENT, last_error=text, next_attempt_at=None, lease_owner=None, lease_expires_at=None)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            return False
        self._event_once(db, record, "UPLOAD_RETRY_LIMIT_REACHED", self._limit_detail(db, record, attempts))
        db.commit()
        return True

    def _reset_failed(self, db: Session, record_id: int, *, commit: bool = True) -> bool:
        """FAILED -> UPLOADING with a fresh budget, due now. Only while nobody holds the record; exactly one of several simultaneous requests wins."""
        now = self._nnow()
        result = db.execute(
            update(DocumentStorageRecord).where(
                DocumentStorageRecord.id == record_id, DocumentStorageRecord.state == sr.FAILED,
                or_(DocumentStorageRecord.lease_expires_at.is_(None), DocumentStorageRecord.lease_expires_at < now),
            ).values(state=sr.UPLOADING, attempts=0, deferrals=0, failure_class=None, deferred_since=None, next_attempt_at=now, lease_owner=None, lease_expires_at=None)
            .execution_options(synchronize_session=False)
        )
        if commit:
            db.commit()
        return result.rowcount == 1

    def _settle(self, db: Session, lease: _Lease, record: DocumentStorageRecord, values: dict) -> bool:
        """Ends the lease with an owner-checked UPDATE. False (and nothing recorded but a note) if the lease was lost: the record has been taken over, so
        this worker's outcome no longer describes it."""
        if lease.settled:
            return False
        result = db.execute(
            update(DocumentStorageRecord).where(DocumentStorageRecord.id == lease.record_id, DocumentStorageRecord.lease_owner == lease.owner)
            .values(lease_owner=None, lease_expires_at=None, **values).execution_options(synchronize_session=False)
        )
        lease.settled = True  # ended either way: nothing more may be written under it
        if result.rowcount == 1:
            return True
        db.rollback()
        self._event(db, record, "UPLOAD_LEASE_LOST", "this attempt's outcome was not recorded because the upload was taken over by another worker")
        db.commit()
        return False

    def _record_success(self, db: Session, lease: _Lease, record: DocumentStorageRecord, event_type: str, detail: str | None = None, *, location: str | None = None) -> bool:
        now = self._now()
        db.refresh(record)
        values = {
            "state": sr.STORED, "last_error": None, "stored_at": now, "last_verified_at": now, "failure_class": None, "deferrals": 0, "deferred_since": None,
            "next_attempt_at": None, "attempts": max(0, record.attempts - 1),  # the claim's charge is refunded: `attempts` keeps counting FAILED attempts only
        }
        if location is not None:
            values["location"] = location
        if not self._settle(db, lease, record, values):
            return False
        self._event(db, record, event_type, detail)
        db.commit()
        return True

    def _record_failure(
        self, db: Session, lease: _Lease, record: DocumentStorageRecord, exc: BaseException | None = None, *, failure_class: str | None = None,
        text: str | None = None, event_type: str | None = None,
    ) -> _Outcome:
        """Classifies a failed attempt and applies the retry policy in ONE owner-checked UPDATE (see failures.py for the rules)."""
        cls = failure_class or fl.classify(exc)
        policy = fl.policy_for(cls)
        text = text if text is not None else _safe(exc)
        db.refresh(record)
        now = self._nnow()
        attempts, deferrals = record.attempts, record.deferrals
        values: dict = {"failure_class": cls, "last_error": text[:500]}
        failed, limit_reached = policy.terminal, False
        if policy.counts_budget:  # a real failed attempt: the claim's charge stays, any deferral episode is over
            values.update(deferrals=0, deferred_since=None)
            if not failed and attempts >= self._max_attempts:
                failed = limit_reached = True
                values["last_error"] = f"Upload abandoned after {attempts} failed attempts (limit {self._max_attempts}); last error: {text}"[:500]
            if failed:
                values.update(state=sr.FAILED, next_attempt_at=None)
            else:
                wait = fl.backoff_seconds(cls, attempts=attempts, deferrals=deferrals, transient_base=self._backoff, transient_cap=self._backoff_cap)
                values["next_attempt_at"] = now + timedelta(seconds=fl.jittered(wait, self._rng))
        else:  # not this upload's fault: the claim's charge is refunded and the wait is counted as a deferral
            values.update(attempts=max(0, attempts - 1), deferrals=deferrals + 1, deferred_since=record.deferred_since or now)
            if policy.parked:
                values["next_attempt_at"] = None  # no automatic retry until an administrator acts (the sweep and the claim both exclude this class)
            else:
                wait = fl.backoff_seconds(cls, attempts=attempts, deferrals=deferrals + 1, transient_base=self._backoff, transient_cap=self._backoff_cap)
                values["next_attempt_at"] = now + timedelta(seconds=fl.jittered(wait, self._rng))
        episode = f"[episode since {values['deferred_since']:%Y-%m-%d %H:%M:%S}]" if values.get("deferred_since") else ""  # a NEW deferral episode (after a success or a counted failure) is noted again
        message = "Drive did not confirm the uploaded bytes; the file was quarantined." if isinstance(exc, DriveIntegrityError) else "The upload to Drive did not complete."
        outcome = _Outcome(cls, retryable=not failed and not policy.parked, failed=failed, message=message)
        if not self._settle(db, lease, record, values):
            return _Outcome(cls, retryable=True, failed=False, message="The upload's outcome was not recorded because another worker took the upload over.", lease_lost=True)
        if event_type is not None:
            self._event(db, record, event_type, text)
        elif cls == fl.AUTH_DEFERRAL:
            self._event_once(db, record, "UPLOAD_DEFERRED_AUTH", f"{text} {episode}")  # once per distinct reason per deferral episode, not once per pass
        elif cls == fl.AUTH_PERMANENT:
            self._event_once(db, record, "UPLOAD_PARKED_AUTH", f"{text} {episode}")
        elif policy.deferral:
            self._event_once(db, record, "UPLOAD_DEFERRED", f"{cls}: {text} {episode}")
        elif isinstance(exc, DriveIntegrityError):
            self._event(db, record, "UPLOAD_INTEGRITY_FAILED", text)
        elif isinstance(exc, DriveTargetError) and not exc.retryable:  # an existing object outside the expected drive is never adopted: retrying cannot change that
            self._event(db, record, "UPLOAD_ADOPTION_REJECTED", text)
        else:
            self._event(db, record, "UPLOAD_ATTEMPT_FAILED", text)
        if limit_reached:
            self._event_once(db, record, "UPLOAD_RETRY_LIMIT_REACHED", self._limit_detail(db, record, attempts))
        db.commit()
        return outcome

    # ------------------------------------------------------------------ small helpers

    def _event(self, db: Session, record: DocumentStorageRecord | None, event_type: str, detail: str | None = None, actor_id: int | None = None) -> None:
        db.add(StorageEvent(record_id=record.id if record is not None else None, event_type=event_type, detail=(detail or "")[:1000] or None, actor_id=actor_id))
        db.flush()

    def _limit_detail(self, db: Session, record: DocumentStorageRecord, attempts: int) -> str:
        """The text of the limit-reached note. It carries the EPISODE: how many times this record has been given a fresh budget (a retry request or the
        same file submitted again). Repeated processing of one episode therefore dedupes, while a second, separate episode is noted again."""
        resets = db.execute(select(func.count()).select_from(StorageEvent).where(StorageEvent.record_id == record.id, StorageEvent.event_type == "UPLOAD_RETRY_REQUESTED")).scalar() or 0
        return f"{attempts} failed attempts (limit {self._max_attempts}); episode {resets}"

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
        # A temp name that belongs to THIS call alone: two writers of the same record can never clobber each other's temp file, and the cleanup below
        # can only ever remove the file this call created (never another upload's).
        temporary = path.with_name(f"{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)  # a failed or partial write, or a failed rename, leaves nothing behind
            raise

    SPOOL_TEMP_MAX_AGE_SECONDS = 3600.0

    def sweep_stale_spool_temp_files(self) -> int:
        """Removes `*.tmp` files in the spool folder older than an hour: what a process that was killed mid-write (no chance to clean up) leaves behind.
        Younger files are never touched, so another upload's in-flight write is safe. Returns how many were removed."""
        if self._spool_dir is None or not self._spool_dir.is_dir():
            return 0
        cutoff = self._now().timestamp() - self.SPOOL_TEMP_MAX_AGE_SECONDS
        removed = 0
        for path in self._spool_dir.glob("*.tmp"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

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
        """Stores the bytes in Drive.
          * `created=True` is returned ONLY when THIS call created the record AND its upload completed and was recorded (STORED).
          * A file already known is a duplicate (`created=False`): the record comes back as it stands. If an unfinished upload of it is held by another
            worker, or is waiting for its schedule, or is parked for an administrator, nothing is started a second time and `record.state` says so.
          * A failed attempt raises StorageUploadError (the record then shows UPLOADING for a retry/reconcile, or FAILED).
        `user_id` is the actor, when known; it is recorded as given and never invented (None stays None)."""
        sha = hashlib.sha256(content).hexdigest()
        existing = self._get(db, kind, sha)
        if existing is not None:
            return self._from_existing(db, existing, content, user_id)

        try:
            reserved = self.drive.reserve_id()
        except (DriveError, DriveAuthError) as exc:
            retryable = isinstance(exc, DriveAuthTransientError) or (isinstance(exc, DriveError) and exc.retryable)
            raise StorageUploadError("Drive could not be reached to accept the upload; nothing was recorded.", None, retryable=retryable) from exc
        # The record is INSERTED ALREADY CLAIMED: its lease (owner, expiry, the charged first attempt) is part of the same row and the same commit as the
        # intent. There is no moment at which the record is visible but unclaimed, so a duplicate caller that finds it can never take the upload away from
        # the caller that created it (it sees a live lease and gets the "in progress" answer): exactly one caller creates, and only that caller can report
        # `created=True`. The retry copy is written right after, before anyone else can act on the record - so the creation race leaves no stray spool file.
        now = self._nnow()
        lease = _Lease(0, _new_owner())
        record = DocumentStorageRecord(
            kind=kind, sha256=sha, original_filename=(original_filename or "document.pdf")[:500], size=len(content),
            md5=hashlib.md5(content).hexdigest(), location=make_location(reserved), state=sr.UPLOADING, created_by_id=user_id,
            attempts=1, last_attempt_at=now, lease_owner=lease.owner, lease_expires_at=now + timedelta(seconds=self._lease_seconds),
        )
        db.add(record)
        try:
            db.commit()  # the intent AND the claim are durable BEFORE any byte is sent
        except IntegrityError:
            db.rollback()
            winner = self._get(db, kind, sha)
            if winner is None:
                raise
            return self._from_existing(db, winner, content, user_id)  # lost the creation race: nothing was spooled, nothing is uploaded here
        lease.record_id = record.id
        try:
            self._spool(record, content)
            self._event(db, record, "UPLOAD_STARTED", actor_id=user_id)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - the lease must not be left dangling by a disk or database fault; the charged attempt keeps it bounded
            db.rollback()
            self._record_failure(db, lease, record, exc)
            raise
        if self._upload_under_lease(db, record, lease, content, create_folders=True):
            return StoreResult(record, duplicate=False, created=True)  # uploaded, verified and recorded by THIS call
        # This call's success could not be recorded because its lease was taken over (it ran past the lease): it did not complete the creation and says so.
        db.refresh(record)
        return StoreResult(record, duplicate=True, created=False)

    def _from_existing(self, db: Session, record: DocumentStorageRecord, content: bytes, user_id: int | None) -> StoreResult:
        """The same bytes again. Only an UNFINISHED upload (UPLOADING / FAILED) is touched; a record in any other state - including MISSING_EXTERNAL, whose
        file left the expected Shared Drive - is returned UNCHANGED (submitting the file again is not a repair path, and a plain duplicate is not audited).
        EVERY resubmission of an unfinished upload is audited, with the actor when there is one ("actor=unknown" otherwise: an identity is never invented):
          * FAILED: the established same-file recovery. The record gets a fresh retry budget (the event history keeps every earlier failure) and an attempt
            is made now. Audited as STORAGE_RETRY_ACCEPTED (via=resubmission).
          * UPLOADING: an attempt is made only if the record is DUE and nobody holds it, exactly like the automatic retry. A resubmission is NOT an
            administrator's retry, so it can neither skip the backoff schedule nor release a record parked for an administrator (revoked authorization);
            that is `retry_upload`'s job. Audited as STORAGE_RESUBMITTED with what happened.
        Nothing is ever started twice: a held upload comes back as it is (UPLOADING)."""
        if record.state not in (sr.UPLOADING, sr.FAILED):
            return StoreResult(record, duplicate=True, created=False)
        actor = f"actor={user_id}" if user_id is not None else "actor=unknown"
        reset = False
        if record.state == sr.FAILED:
            before = f"attempts={record.attempts}; failure_class={record.failure_class}"
            if self._reset_failed(db, record.id, commit=False):
                reset = True
                self._audit_retry(db, record, user_id, audit.STORAGE_RETRY_ACCEPTED, f"via=resubmission; from=FAILED; budget reset; {before}; {actor}")
                self._event(db, record, "UPLOAD_RETRY_REQUESTED", "the same file was submitted again", actor_id=user_id)
                db.commit()
        lease = self._claim(db, record.id, manual=False, commit=False)  # due now after a reset; otherwise only if it is due, free and not parked
        if lease is None:
            reason = self._unclaimed_reason(db, record)
            self._audit_retry(db, record, user_id, audit.STORAGE_RESUBMITTED, f"state={record.state}; no attempt started ({reason}); {actor}")
            db.commit()  # the audit row is durable whatever happens next
            if reason == "exhausted":
                self._abandon_exhausted(db, record)
                db.refresh(record)
                raise StorageUploadError("The upload was given up after its retry limit.", record, retryable=False)
            return StoreResult(record, duplicate=True, created=False)
        if not reset:
            self._audit_retry(db, record, user_id, audit.STORAGE_RESUBMITTED, f"state=UPLOADING; attempt started; {actor}")
        db.commit()  # the lease (and the audit row) are durable BEFORE any byte moves
        self._upload_under_lease(db, record, lease, content, create_folders=True)
        return StoreResult(record, duplicate=True, created=False)

    def _upload_now(self, db: Session, record: DocumentStorageRecord, content: bytes, *, create_folders: bool = True, manual: bool = False) -> bool:
        """Claims the record and uploads `content` for it. Returns True ONLY when the upload completed and its success was recorded; False when nothing was
        started (another worker holds the upload, it is not due, it is parked, or it is no longer pending) or when this call lost its lease before it could
        record the success. Raises StorageUploadError when the attempt failed (the record then shows where it stands)."""
        lease = self._claim(db, record.id, manual=manual)
        if lease is None:
            reason = self._unclaimed_reason(db, record)
            if reason == "exhausted":
                self._abandon_exhausted(db, record)
                db.refresh(record)
                raise StorageUploadError("The upload was given up after its retry limit.", record, retryable=False)
            return False
        return self._upload_under_lease(db, record, lease, content, create_folders=create_folders)

    def _upload_under_lease(self, db: Session, record: DocumentStorageRecord, lease: _Lease, content: bytes, *, create_folders: bool) -> bool:
        """True when the upload completed and its success was recorded under this lease; False when the lease was lost first. Raises on a failed attempt."""
        db.refresh(record)
        try:
            with _LeaseRenewer(self, db.get_bind(), lease):  # the lease stays alive for as long as this (possibly slow) upload legitimately runs
                outcome = self.drive.upload(
                    record.kind, record.sha256, record.original_filename, content, reserved_id=parse_location(record.location), create_folders=create_folders
                )
        except (DriveError, DriveAuthError) as exc:
            failure = self._record_failure(db, lease, record, exc)
            raise StorageUploadError(failure.message, record, retryable=failure.retryable) from exc
        except Exception as exc:  # unexpected: the lease must not be left dangling and the charged attempt keeps a poison document bounded; the error itself propagates unchanged
            self._record_failure(db, lease, record, exc)
            raise
        kind = "DEDUP_ADOPTED" if outcome.deduplicated else ("ADOPTED_EARLIER_ATTEMPT" if outcome.adopted else "STORED")
        # `outcome.location` differs from the reserved id only when identical bytes already existed in Drive.
        if self._record_success(db, lease, record, kind, location=outcome.location):
            self._unspool(record)  # only the lease holder releases the retry copy; a worker that lost its lease leaves it for the one that took over
            return True
        return False

    def _audit_retry(self, db: Session, record: DocumentStorageRecord, user_id: int | None, action: str, detail: str) -> None:
        audit.log_action(db, user_id=user_id, action=action, entity_type="DocumentStorage", entity_id=str(record.id), details=f"kind={record.kind}; {detail}"[:1000])

    def _reject_retry(self, db: Session, record: DocumentStorageRecord, user_id: int | None, detail: str, message: str) -> StorageStateError:
        """A retry request that cannot be honoured is audited too (who asked, why it was refused), then reported to the caller."""
        db.rollback()
        db.refresh(record)
        self._audit_retry(db, record, user_id, audit.STORAGE_RETRY_REJECTED, f"state={record.state}; {detail}")
        db.commit()
        return StorageStateError(message)

    def retry_upload(self, db: Session, record_id: int, *, user_id: int | None) -> DocumentStorageRecord:
        """The authorised, AUDITED manual retry of an upload that is not progressing by itself. Two cases:
          * FAILED (given up): needs the spooled bytes; gets a FRESH retry budget (attempts and deferrals reset) and is tried once now;
          * UPLOADING (waiting for its schedule, or parked for an administrator, e.g. after a revoked authorization): is tried now; its budget is NOT
            reset and a parked record is released from its parked state.
        Every request is audited (accepted or rejected, with the requester). Nothing is deleted; earlier failures stay in the event history. A rejection
        about the destination (a foreign object under the reserved id, an integrity mismatch) fails again the same way, so this cannot be used to get
        around those checks. Duplicate and concurrent requests are safe: exactly one wins the compare-and-swap, the others are refused."""
        record = db.get(DocumentStorageRecord, record_id)
        if record is None:
            raise LookupError("storage record not found")
        if record.state == sr.FAILED:
            content = self._spooled(record)
            if content is None:
                raise self._reject_retry(db, record, user_id, "no retry copy was kept", "No copy of the file was kept, so the upload cannot be retried; the file must be uploaded again.")
            before = f"attempts={record.attempts}; failure_class={record.failure_class}"
            if not self._reset_failed(db, record.id, commit=False):
                raise self._reject_retry(db, record, user_id, "another retry or upload already holds this record", "This upload is already being retried.")
            self._audit_retry(db, record, user_id, audit.STORAGE_RETRY_ACCEPTED, f"from=FAILED; budget reset; {before}")
            self._event(db, record, "UPLOAD_RETRY_REQUESTED", "an administrator asked for a retry", actor_id=user_id)
            db.commit()
            self._upload_now(db, record, content, manual=True)
            db.refresh(record)
            return record
        if record.state != sr.UPLOADING:
            raise self._reject_retry(db, record, user_id, "nothing to retry", f"A document in state {record.state} has no pending or failed upload to retry.")
        before = f"attempts={record.attempts}; deferrals={record.deferrals}; failure_class={record.failure_class}"
        lease = self._claim(db, record.id, manual=True, commit=False)
        if lease is None:
            raise self._reject_retry(db, record, user_id, "the upload is in progress or out of attempts", "This upload is already in progress, or has used up its retry budget.")
        self._audit_retry(db, record, user_id, audit.STORAGE_RETRY_ACCEPTED, f"from=UPLOADING; budget kept; {before}")
        self._event(db, record, "UPLOAD_RETRY_REQUESTED", "an administrator asked for an immediate retry", actor_id=user_id)
        db.commit()
        report = ReconcileReport()
        try:
            self._complete_stale_upload(db, record, report, lease=lease)
        except (DriveError, DriveAuthError):
            pass  # already recorded on the record by the settle; reported through its state below
        db.refresh(record)
        if record.state == sr.STORED:
            return record
        raise StorageUploadError(record.last_error or "The upload did not complete.", record, retryable=(record.state == sr.UPLOADING and record.failure_class != fl.AUTH_PERMANENT))

    def initialise_structure(self) -> None:
        """Creates any MISSING structural folder, on purpose. A write, so it passes through the destination guard like every other. reconcile and
        sync never do this implicitly; they only report what is missing."""
        self.drive.folders.ensure_structure()

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
        try:
            self.drive.archive(record.location, record.kind)  # a failure here changes nothing in the database
        except DriveObjectTrashed:
            # Archiving never un-trashes: someone outside the application deleted this file. Reconciliation will record that; an explicit restore may bring it back.
            raise StorageStateError("The file is in the Drive trash, so it was not archived. Run reconciliation, and restore it explicitly if it should be used again.") from None
        except DriveForeignObject:
            raise StorageStateError("The file is no longer in the expected Shared Drive, so it was not archived. Run reconciliation.") from None
        except DriveNotADocument:
            raise StorageStateError(NOT_A_DOCUMENT_TEXT + " It was not archived.") from None
        except DriveDestinationMissing:
            raise StorageStateError("It was not archived. " + DESTINATION_MISSING_TEXT) from None
        except DriveNotFound:
            raise StorageStateError("The file no longer exists in Drive, so it was not archived. Run reconciliation.") from None
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
        except DriveForeignObject:
            raise StorageStateError(
                "This document is no longer in the expected Shared Drive (it was moved elsewhere or its access changed), so it cannot be restored from here. "
                + MISSING_EXTERNAL_GUIDANCE
            ) from None
        except DriveNotADocument:
            raise StorageStateError(NOT_A_DOCUMENT_TEXT + " It was not restored.") from None
        except DriveDestinationMissing:
            # The document IS in Drive; a folder it should be moved into was not found. Saying "the document was not found" here would be wrong.
            raise StorageStateError("It was not restored. " + DESTINATION_MISSING_TEXT) from None
        except DriveNotFound:
            raise StorageStateError(
                "This document was not found in Drive (it may have been permanently deleted or its access changed), so it cannot be restored from here. "
                + MISSING_EXTERNAL_GUIDANCE
            ) from None
        record.state, record.restored_at, record.restored_by_id, record.missing_detected_at = sr.STORED, self._now(), user_id, None
        self._event(db, record, "RESTORED", actor_id=user_id)
        audit.log_action(db, user_id=user_id, action=audit.DOCUMENT_RESTORED, entity_type="DocumentStorage", entity_id=str(record.id), details=f"kind={record.kind}")
        db.commit()
        return record

    # ------------------------------------------------------------------ reconciliation

    def _roles(self) -> dict:
        """The structural folders that EXIST. Read-only by design: reconcile and sync must never create a folder (a missing one is simply absent)."""
        structure = self.drive.folders.resolve_structure()
        kinds = [k.split(":", 1)[1] for k in structure if k.startswith("active:")]
        return {
            "active": {kind: structure[f"active:{kind}"] for kind in kinds if structure[f"active:{kind}"]},
            "archive": {kind: structure[f"archive:{kind}"] for kind in kinds if structure[f"archive:{kind}"]},
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
        if record.state in (sr.STORED, sr.ARCHIVED):
            try:
                self.drive.client.confirm_adoptable(replace(obj, trashed=False))  # still in the expected Shared Drive? (trashed was judged above)
            except DriveTargetError as foreign:
                # The file now belongs to a different drive (or to somebody's My Drive). It is no longer served; nothing is deleted or altered, in Drive
                # or here: the record, its extraction/exception/verification history and the audit trail all stay as they are.
                record.state, record.missing_detected_at, record.last_verified_at = sr.MISSING_EXTERNAL, now, now
                self._event(db, record, "EXTERNAL_MOVE_OUT_OF_DRIVE", foreign.reason)
                report.foreign_drive += 1
                return
            if not is_ordinary_document(obj):
                # The id now describes a folder, a shortcut or a Google Docs item: never a stored expense document. Marked like any other unavailable
                # file (nothing is changed in Drive); a person decides what happened.
                record.state, record.missing_detected_at, record.last_verified_at = sr.MISSING_EXTERNAL, now, now
                self._event(db, record, "EXTERNAL_NOT_A_DOCUMENT", "the Drive item is a folder, a shortcut or a Google Docs item")
                report.not_documents += 1
                return
        in_archive = bool(set(obj.parents) & (set(roles["archive"].values()) | {roles["archive_root"]}))
        in_active = roles["active"].get(record.kind) in obj.parents
        if record.state == sr.MISSING_EXTERNAL:
            try:
                self.drive.client.confirm_adoptable(obj)  # a "recovered" file is adopted again only if it is in the expected drive
            except DriveTargetError as rejected:
                self._event_once(db, record, "RECOVERY_REJECTED", rejected.reason)
                record.last_verified_at = now
                return  # stays MISSING_EXTERNAL
            if not is_ordinary_document(obj):
                self._event_once(db, record, "RECOVERY_REJECTED", "not_a_document")
                record.last_verified_at = now
                return  # a folder / shortcut / Google Docs item is never "the document that came back"
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

    def _complete_stale_upload(self, db: Session, record: DocumentStorageRecord, report: ReconcileReport, *, lease: _Lease | None = None) -> None:
        """Finishes a pending upload that no live worker is on: under a LEASE, so two reconcilers (or a reconciler and a manual retry) never work on the
        same record. `lease` is passed by a caller that already claimed it (the manual retry); otherwise the record is claimed here (automatic schedule)."""
        if lease is None:
            lease = self._claim(db, record.id, manual=False)
            if lease is None:
                if self._unclaimed_reason(db, record) == "exhausted" and self._abandon_exhausted(db, record):
                    report.failed_uploads += 1
                return  # held by another worker, not due yet, parked, or already finished: not ours to touch
        try:
            self._complete_under_lease(db, record, lease, report)
        except (DriveError, DriveAuthError) as exc:
            self._record_failure(db, lease, record, exc)  # a no-op if the attempt was already settled
            raise
        except Exception as exc:  # never leave a lease dangling; the charged attempt bounds a poison document
            self._record_failure(db, lease, record, exc)
            raise

    def _complete_under_lease(self, db: Session, record: DocumentStorageRecord, lease: _Lease, report: ReconcileReport) -> None:
        db.refresh(record)
        # 1. Did the interrupted attempt actually create the file? (look it up by the reserved id)
        try:
            obj = self.drive.inspect_unchecked(record.location)  # raw on purpose: the adoption check below decides what to do with a foreign object
        except DriveNotFound:
            obj = None
        if obj is not None and not obj.trashed:
            try:
                self.drive.client.confirm_adoptable(obj)  # never adopt a file that is not in the expected drive
            except DriveTargetError as rejected:
                # A rejected ADOPTION is permanent (the pending record is kept FAILED, visible, spooled bytes retained, never marked STORED); a refusal
                # that is only an outage or a configuration problem waits and is retried, without using the budget.
                outcome = self._record_failure(db, lease, record, rejected)
                if outcome.failed:
                    report.failed_uploads += 1
                return
            if obj.md5 == record.md5 and obj.size == record.size:
                if self._record_success(db, lease, record, "ADOPTED_AFTER_INTERRUPTION", "verified checksum and size"):
                    self._unspool(record)
                    report.adopted_uploads += 1
            else:
                text = "A file exists under the reserved id but its checksum/size do not match."
                self._record_failure(db, lease, record, failure_class=fl.INTEGRITY, text=text, event_type="UPLOAD_INTEGRITY_FAILED")
                report.failed_uploads += 1
            return
        # 2. Not in Drive: retry from the spooled copy if there is one, otherwise it can only be re-uploaded by the user.
        content = self._spooled(record)
        if content is None:
            text = "The upload never reached Drive and no copy was kept; it must be uploaded again."
            self._record_failure(db, lease, record, failure_class=fl.PERMANENT, text=text, event_type="UPLOAD_ABANDONED")
            report.failed_uploads += 1
            return
        try:
            self._upload_under_lease(db, record, lease, content, create_folders=False)  # reconcile re-sends the bytes but never creates a folder
            report.reuploaded += 1
        except StorageUploadError as exc:
            if exc.record is not None and exc.record.state == sr.FAILED:
                report.failed_uploads += 1
            elif isinstance(exc.__cause__, DriveFolderMissing):  # still waiting, nothing lost: say what is needed instead of creating folders implicitly
                report.errors.append(f"record {record.id}: the destination folder does not exist; create the structure explicitly (initialise_structure) or upload the file again")

    def _due_uploads(self, db: Session, now: datetime, stale_after_seconds: float) -> list[DocumentStorageRecord]:
        """The sweep: pending uploads that are DUE (their own schedule, not a guess from `updated_at`), that nobody holds (no live lease), that are not
        parked for an administrator and that are old enough not to belong to a caller still inside store(). Served by the existing ix_document_storage_state (there is deliberately no index on the retry columns: it was measured and never chosen).
        The claim inside _complete_stale_upload re-checks all of it atomically, so a record that changed since this list was read is simply skipped."""
        return list(db.execute(
            select(DocumentStorageRecord).where(
                DocumentStorageRecord.state == sr.UPLOADING,
                DocumentStorageRecord.created_at <= now - timedelta(seconds=stale_after_seconds),
                or_(DocumentStorageRecord.next_attempt_at.is_(None), DocumentStorageRecord.next_attempt_at <= now),
                or_(DocumentStorageRecord.lease_expires_at.is_(None), DocumentStorageRecord.lease_expires_at < now),
                or_(DocumentStorageRecord.failure_class.is_(None), DocumentStorageRecord.failure_class != fl.AUTH_PERMANENT),
            ).order_by(DocumentStorageRecord.id)
        ).scalars().all())

    def reconcile(self, db: Session, *, stale_after_seconds: int = STALE_UPLOAD_SECONDS) -> ReconcileReport:
        report = ReconcileReport()
        try:
            self.sweep_stale_spool_temp_files()  # leftovers of a process that died mid-write; never touches a young (in-flight) file
        except OSError:
            pass
        now = _naive(self._now())
        try:
            roles = self._roles()
        except (DriveError, DriveAuthError) as exc:  # cannot even see the folder structure: judge nothing, say so, and stop
            report.errors.append(f"folder structure: {_safe(exc)}")
            audit.log_action(db, user_id=None, action=audit.STORAGE_RECONCILED, entity_type="DocumentStorage", details=str(report.as_dict())[:900])
            db.commit()
            return report

        for record in self._due_uploads(db, now, stale_after_seconds):
            try:
                self._complete_stale_upload(db, record, report)
            except Exception as exc:  # a Drive/sign-in problem, or a bug triggered by ONE record: it is already recorded on that record (charged and released); the others still get their turn
                report.errors.append(f"record {record.id}: {_safe(exc)}")
                db.rollback()

        for record in db.execute(select(DocumentStorageRecord).where(DocumentStorageRecord.state.in_((sr.STORED, sr.ARCHIVED, sr.MISSING_EXTERNAL)))).scalars().all():
            try:
                try:
                    obj = self.drive.inspect_unchecked(record.location)  # raw on purpose: _apply is what notices a file that left the expected drive
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

    def _changes_state_name(self) -> str:
        """The change token is only meaningful for the scope it was issued for, so it is stored under a name that includes the expected Shared Drive.
        A token for any other scope is never read as this one's (no existing column or migration is needed: the name is the key)."""
        expected = self.drive.client.expected_drive_id
        if not expected:
            return CHANGES_STATE
        return f"{CHANGES_STATE}:{hashlib.sha256(expected.encode()).hexdigest()[:16]}"

    def sync_changes(self, db: Session) -> ChangesReport:
        report = ChangesReport()
        name = self._changes_state_name()
        state = db.get(DriveSyncState, name)
        client = self.drive.client
        if (state is None or not state.page_token) and name != CHANGES_STATE:
            legacy = db.get(DriveSyncState, CHANGES_STATE)
            if legacy is not None and legacy.page_token:
                return self._rescope_changes(db, name, report)  # an unscoped token cannot be trusted for this drive: do not start over silently
        if state is None or not state.page_token:
            db.merge(DriveSyncState(name=name, page_token=client.get_start_page_token()))
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
            self._resync(db, name, report, "CHANGE_TOKEN_EXPIRED_RECOVERED", "full reconciliation performed and a new change token stored")
        return report

    def _resync(self, db: Session, name: str, report: ChangesReport, done_event: str, done_detail: str) -> bool:
        """Replaces a change token that can no longer be used (expired, or issued for another scope) WITHOUT skipping anything: the new token is taken
        BEFORE the full pass (a change made while it runs is then delivered by the next sync), and it is stored only if the pass completed. If the pass
        could not judge everything (an outage), the old token stays, so the next sync does the whole thing again instead of starting from 'now'."""
        new_token = self.drive.client.get_start_page_token()
        result = self.reconcile(db)
        report.reconcile = result.as_dict()
        if result.errors:
            self._event_once(db, None, "CHANGE_TOKEN_RESET_DEFERRED", "the full reconciliation could not judge everything; the change token was not replaced")
            db.commit()
            return False
        db.merge(DriveSyncState(name=name, page_token=new_token))
        self._event(db, None, done_event, done_detail)
        db.commit()
        return True

    def _rescope_changes(self, db: Session, name: str, report: ChangesReport) -> ChangesReport:
        report.rescoped = self._resync(db, name, report, "CHANGE_TOKEN_RESCOPED", "an unscoped change token was replaced by one scoped to the expected Shared Drive after a full reconciliation")
        return report
