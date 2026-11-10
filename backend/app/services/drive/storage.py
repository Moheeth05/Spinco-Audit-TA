"""Google Drive implementation of the DocumentStorage interface (app/services/storage_backend.py).

Locations are opaque references of the form `gdrive:<file id>`; the Drive id never leaves the backend. Safe-deletion semantics: there is
NO permanent-delete operation anywhere in this class. `delete()` (the interface's remove operation) ARCHIVES the file - a reversible
move into the Archive folder. Uploads use a pre-generated file id so that a retry after an ambiguous failure adopts the file the
earlier attempt created instead of creating a duplicate, and every upload is verified (checksum and size) before it is reported stored.
"""

import hashlib
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from app.services.drive.client import (
    DriveClient,
    DriveConflict,
    DriveError,
    DriveIntegrityError,
    DriveNotFound,
    DriveObject,
)
from app.services.drive.errors import DriveDestinationMissing, DriveForeignObject, DriveNotADocument, DriveObjectTrashed, DriveRootMismatch
from app.services.drive.guard import DriveTargetError
from app.services.storage import sanitize_filename
from app.services.storage_backend import DocumentMetadata, DocumentStorage

LOCATION_PREFIX = "gdrive:"
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,100}$")

KIND_MIME = {"excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}  # every other kind is a PDF (the default)
KIND_FOLDER_NAMES = {"expense_report": "Expense Reports", "trip_report": "Trip Reports", "excel": "Excel Exports", "duplicate": "Duplicates"}  # "duplicate": retained copies of exact-duplicate uploads (central storage phase)
ARCHIVE_FOLDER_NAME = "Archive"
QUARANTINE_FOLDER_NAME = "_quarantine"


class InvalidLocation(ValueError):
    """The string is not a gdrive:<id> reference."""


class DriveFolderMissing(DriveError):
    """A structural folder does not exist and this operation is not allowed to create it (reconcile and sync never create folders)."""

    def __init__(self, role: str):
        # Retryable: an upload recorded while Drive was unreachable (before any folder existed) must stay UPLOADING, never be lost. It completes
        # once the structure has been created on purpose (StorageManager.initialise_structure) or the same bytes are uploaded again.
        super().__init__(f"The Drive folder for '{role}' does not exist and this operation may not create it.", reason="folder_missing", retryable=True)


_GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."  # folders, shortcuts, Docs, Sheets, Slides, ... - never ordinary uploaded files


def is_ordinary_document(obj: DriveObject) -> bool:
    """An uploaded expense document is an ordinary file. A missing/blank MIME type cannot be vouched for (fail closed); anything under Google's own
    `application/vnd.google-apps.` family is a folder, a shortcut or a Google Docs item, never an uploaded document."""
    mime = (obj.mime_type or "").strip().lower()
    return bool(mime) and not mime.startswith(_GOOGLE_APPS_MIME_PREFIX)


def _select_range(header: str | None, total: int) -> tuple[int, dict, int, int]:
    """(status, headers, first byte, last byte) for a Range header over `total` bytes of VERIFIED content. Only a single byte range is honoured; a
    malformed or multi-range header is ignored (a full 200, as HTTP allows); an unsatisfiable one is a 416. last < first means 'no body'."""
    full = (200, {"Content-Length": str(total)}, 0, total - 1)
    spec = (header or "").strip()
    if not spec.lower().startswith("bytes=") or "," in spec:
        return full
    match = re.fullmatch(r"(\d*)-(\d*)", spec[6:].strip())
    if not match or (match.group(1) == "" and match.group(2) == ""):
        return full
    first_text, last_text = match.groups()
    if first_text == "":  # suffix range: the last N bytes
        count = int(last_text)
        if count == 0 or total == 0:
            return (416, {"Content-Range": f"bytes */{total}", "Content-Length": "0"}, 0, -1)
        first, last = max(total - count, 0), total - 1
    else:
        first = int(first_text)
        last = int(last_text) if last_text else total - 1
        if last_text and last < first:
            return full  # syntactically invalid: ignored
        if first >= total:
            return (416, {"Content-Range": f"bytes */{total}", "Content-Length": "0"}, 0, -1)
        last = min(last, total - 1)
    return (206, {"Content-Range": f"bytes {first}-{last}/{total}", "Content-Length": str(last - first + 1)}, first, last)


class VerifiedDownload:
    """A document whose bytes were ALREADY downloaded in full, hashed and tied to the verified file and to the recorded checksum (see
    GoogleDriveStorage._download_verified). Same surface as client.DriveDownload (status, headers, iter_bytes, close); it reads from a private temporary
    copy, so nothing here talks to Drive and nothing unverified can come out of it."""

    def __init__(self, path: Path, total: int, range_header: str | None = None):
        self._path = path
        self.status, self.headers, self._first, self._last = _select_range(range_header, total)

    def iter_bytes(self, chunk_size: int = 256 * 1024):
        remaining = self._last - self._first + 1
        if remaining <= 0:
            return
        with open(self._path, "rb") as handle:
            handle.seek(self._first)
            while remaining > 0:
                chunk = handle.read(min(chunk_size, remaining))
                if not chunk:
                    raise DriveIntegrityError("The verified copy of the document is shorter than expected.", reason="size_mismatch")
                remaining -= len(chunk)
                yield chunk

    def close(self) -> None:  # the temporary copy is removed by open_stream(); nothing to release here
        return None


def make_location(file_id: str) -> str:
    if not _FILE_ID_RE.match(file_id or ""):
        raise InvalidLocation("not a Drive file id")
    return f"{LOCATION_PREFIX}{file_id}"


def parse_location(location: str) -> str:
    if not isinstance(location, str) or not location.startswith(LOCATION_PREFIX):
        raise InvalidLocation("not a gdrive: location")
    file_id = location[len(LOCATION_PREFIX):]
    if not _FILE_ID_RE.match(file_id):
        raise InvalidLocation("malformed gdrive: location")
    return file_id


def is_drive_location(location: str | None) -> bool:
    return isinstance(location, str) and location.startswith(LOCATION_PREFIX)


class DriveFolders:
    """Finds or creates the folder structure under the configured root, never reorganising or deleting anything that exists.

        <root>/Expense Reports, Trip Reports, Excel Exports          active documents
        <root>/Archive/<kind folder>                                  archived (reversible)
        <root>/_quarantine                                            integrity failures, never auto-deleted

    Creation uses pre-generated ids, so two processes racing to create the same folder cannot create two of them silently."""

    def __init__(self, client: DriveClient, root_id: str):
        if client.guarded_root is not None and client.guarded_root != root_id:
            # The structure would be built under one folder while the guard verifies another: writes could be verified against one destination
            # and sent to a different one. Refused outright.
            raise DriveRootMismatch("The folder structure root is not the root that the client's destination guard verifies.")
        self._client = client
        self.root_id = root_id
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], str] = {}

    def invalidate(self) -> None:
        """Forgets every cached folder id. A folder that was deleted or renamed in Drive leaves a stale id here for the life of the process; after a
        'not found' for a destination the next lookup must ask Drive again."""
        with self._lock:
            self._cache.clear()

    def _lookup(self, name: str, parent_id: str) -> str | None:
        """The existing folder, if there is one. READ-ONLY (a cached id is reused; nothing is ever created here)."""
        key = (parent_id, name)
        if key in self._cache:
            return self._cache[key]
        matches = self._client.find_children(parent_id, name=name, folders_only=True)
        if not matches:
            return None
        chosen = sorted(matches, key=lambda f: (f.created_time or "", f.id))[0]  # deterministic if a human made duplicates
        self._cache[key] = chosen.id
        return chosen.id

    def _ensure(self, name: str, parent_id: str, role: str, *, create: bool = True) -> str:
        with self._lock:
            found = self._lookup(name, parent_id)
            if found:
                return found
            if not create:
                raise DriveFolderMissing(role)
            new_id = self._client.generate_ids()[0]
            try:
                folder_id = self._client.create_folder(name, parent_id, file_id=new_id, app_properties={"spinco_role": role}).id
            except DriveConflict:  # a folder with this id exists already (an earlier attempt): adopt it ONLY if it belongs to the expected drive
                folder_id = self._client.confirm_adoptable(self._client.get_file(new_id)).id
            self._cache[(parent_id, name)] = folder_id
            return folder_id

    def active(self, kind: str, *, create: bool = True) -> str:
        return self._ensure(KIND_FOLDER_NAMES[kind], self.root_id, f"active:{kind}", create=create)

    def archive_root(self, *, create: bool = True) -> str:
        return self._ensure(ARCHIVE_FOLDER_NAME, self.root_id, "archive", create=create)

    def archive(self, kind: str, *, create: bool = True) -> str:
        return self._ensure(KIND_FOLDER_NAMES[kind], self.archive_root(create=create), f"archive:{kind}", create=create)

    def quarantine(self, *, create: bool = True) -> str:
        return self._ensure(QUARANTINE_FOLDER_NAME, self.root_id, "quarantine", create=create)

    def ensure_structure(self) -> dict[str, str]:
        """Finds or CREATES every structural folder. A write operation: only for explicit initialisation, never for reconcile or sync."""
        structure = {"root": self.root_id, "archive": self.archive_root(), "quarantine": self.quarantine()}
        for kind in KIND_FOLDER_NAMES:
            structure[f"active:{kind}"] = self.active(kind)
            structure[f"archive:{kind}"] = self.archive(kind)
        return structure

    def resolve_structure(self) -> dict[str, str | None]:
        """Finds the structural folders that EXIST and creates nothing: a missing folder is None. Used by reconcile and sync, which must never write."""
        with self._lock:
            archive_root = self._lookup(ARCHIVE_FOLDER_NAME, self.root_id)
            structure: dict[str, str | None] = {"root": self.root_id, "archive": archive_root, "quarantine": self._lookup(QUARANTINE_FOLDER_NAME, self.root_id)}
            for kind, folder_name in KIND_FOLDER_NAMES.items():
                structure[f"active:{kind}"] = self._lookup(folder_name, self.root_id)
                structure[f"archive:{kind}"] = self._lookup(folder_name, archive_root) if archive_root else None
            return structure


@dataclass(frozen=True)
class UploadOutcome:
    obj: DriveObject
    adopted: bool  # an earlier attempt had already created this file (same id) and it was verified, not re-uploaded
    deduplicated: bool  # a file with the same SHA-256 already existed in the active folder; nothing new was created

    @property
    def location(self) -> str:
        return make_location(self.obj.id)


class GoogleDriveStorage(DocumentStorage):
    ID_POOL_SIZE = 20

    def __init__(self, client: DriveClient, folders: DriveFolders, *, temp_root: Path | None = None):
        if folders._client is not client or (client.guarded_root is not None and folders.root_id != client.guarded_root):
            raise DriveRootMismatch("The storage and its folder structure must share one client and the one root that its destination guard verifies.")
        self.client = client
        self.folders = folders
        self._temp_root = Path(temp_root) if temp_root else None
        self._id_pool: list[str] = []
        self._pool_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _verify(obj: DriveObject, md5: str, size: int) -> None:
        if obj.md5 != md5 or obj.size != size:
            raise DriveIntegrityError("Checksum or size in Drive does not match the uploaded content.", reason="integrity")

    def _quarantine(self, obj: DriveObject) -> None:
        try:
            self.client.update_file(obj.id, add_parents=self.folders.quarantine(), remove_parents=",".join(p for p in obj.parents if p != self.folders.quarantine()) or None)
        except DriveError:
            pass  # best effort: the integrity error below is what the caller acts on

    def reserve_id(self) -> str:
        """A Drive-generated file id, reserved in advance. Ids come from a small pool refilled in batches, so a brief Drive outage does
        not stop uploads from being RECORDED (with their id) and queued for retry; unused ids are harmless. Only when the pool is empty
        AND Drive is unreachable does this raise."""
        with self._pool_lock:
            if not self._id_pool:
                self._id_pool.extend(self.client.generate_ids(self.ID_POOL_SIZE))
            return self._id_pool.pop()

    # ------------------------------------------------------------------ write

    def upload(self, kind: str, file_hash: str, original_filename: str, content: bytes, *, reserved_id: str | None = None, create_folders: bool = True) -> UploadOutcome:
        if kind not in KIND_FOLDER_NAMES:
            raise ValueError(f"unknown storage kind {kind!r}")
        md5 = hashlib.md5(content).hexdigest()  # Drive's own checksum algorithm; not used for security
        size = len(content)
        folder_id = self.folders.active(kind, create=create_folders)  # reconcile re-uploads with create_folders=False: it never creates a folder

        existing = self.client.find_children(folder_id, app_property=("sha256", file_hash))
        if existing:
            found = self.client.confirm_adoptable(sorted(existing, key=lambda f: (f.created_time or "", f.id))[0])  # defence in depth: it must be in the expected drive
            self._verify(found, md5, size)
            return UploadOutcome(found, adopted=False, deduplicated=True)

        file_id = reserved_id or self.reserve_id()
        name = f"{file_hash[:12]}_{sanitize_filename(original_filename)}"
        properties = {"sha256": file_hash, "kind": kind, "spinco": "1"}
        try:
            created = self.client.upload_file(file_id=file_id, name=name, parent_id=folder_id, content=content, mime_type=KIND_MIME.get(kind, "application/pdf"), app_properties=properties)
            adopted = False
        except DriveConflict:
            # An earlier, ambiguous attempt may have succeeded: adopt that object ONLY if it belongs to the expected drive (and is live), then verify its
            # checksum and size. An object with our reserved id in a My Drive or another Shared Drive is never ours, however often this is retried.
            created = self.client.confirm_adoptable(self.client.get_file(file_id))
            adopted = True
        try:
            self._verify(created, md5, size)
        except DriveIntegrityError:
            self._quarantine(created)
            raise
        return UploadOutcome(created, adopted=adopted, deduplicated=False)

    # ------------------------------------------------------------------ DocumentStorage interface

    def save(self, kind: str, file_hash: str, original_filename: str, content: bytes) -> str:
        return self.upload(kind, file_hash, original_filename, content).location

    def inspect_unchecked(self, location: str) -> DriveObject:
        """The file's RAW Drive metadata, with NO Shared Drive boundary check: a file that was moved to another drive, or into somebody's My Drive,
        is returned like any other (the answer is still required to describe the file that was asked for). Reserved for reconciliation (which must see
        exactly that in order to mark the record) and for this class's own checks. Application code must never use it to decide that a document is
        available or to show its metadata - use exists() / get_metadata() / retrieve() / open_stream(), which apply the boundary. There is deliberately
        no alias under another name: tests/test_drive_storage_boundary.py scans the application for this method, the client-level reads and the
        imports that would lead to them."""
        return self.client.get_file(parse_location(location))

    def _destination_ready(self) -> None:
        """A write operation first needs a verified destination; that refusal is the one a caller sees, whatever is wrong with the individual file."""
        guard = self.client.write_guard
        if guard is not None:
            guard.assert_ready()

    def _require_in_drive(self, obj: DriveObject) -> None:
        """A document is served, archived or restored only while Drive says it is (1) still in the expected Shared Drive and (2) an ORDINARY stored
        document - not a folder, a Drive shortcut or a Google Docs/Sheets/Slides item. The check needs no network call (it reads the metadata just
        fetched) and never changes the file: something that is not ours or not a document is simply not ours to hand out."""
        try:
            self.client.confirm_adoptable(replace(obj, trashed=False))  # trashed is judged separately by each caller
        except DriveTargetError:
            raise DriveForeignObject() from None
        if not is_ordinary_document(obj):
            raise DriveNotADocument()

    def _tie_to_record(self, meta: DriveObject, expected_md5: str, expected_size: int | None) -> None:
        """Before a single byte is downloaded: Drive's OWN checksum (and size) of the verified file must equal the ones RECORDED for the document. A file
        that was replaced since it was stored fails here; a file with no checksum at all cannot be tied to anything and is refused as well."""
        if not meta.md5:
            raise DriveIntegrityError("The document has no checksum in Drive and cannot be verified, so it was not served.", reason="unverifiable")
        if meta.md5.lower() != expected_md5.lower():
            raise DriveIntegrityError("The document no longer matches its recorded checksum, so it was not served.", reason="checksum_mismatch")
        if expected_size is not None and meta.size is not None and meta.size != expected_size:
            raise DriveIntegrityError("The document no longer matches its recorded size, so it was not served.", reason="size_mismatch")

    def _download_verified(self, location: str, expected_md5: str, expected_size: int | None) -> tuple[Path, Path, int]:
        """Downloads the WHOLE document into a private temporary folder and returns (folder, file, size) only if every link of the chain holds:
          1. Drive's answer describes the file that was asked for (client.get_file), which is live, in the expected Shared Drive and an ordinary document;
          2. Drive's own checksum and size equal the RECORDED ones (so a replaced file is refused before it is downloaded);
          3. the downloaded bytes, hashed here, equal the recorded checksum and size;
          4. after the download the same file is read again and must still be live, still in the expected drive and unchanged (so a move or a
             replacement DURING the download is caught).
        The identity of the content is therefore established by hashing the bytes themselves; NO header of the download response is relied on (what
        real Google sends in those headers has not been validated). Any failure raises a typed error, removes the temporary folder and leaves the caller
        with nothing: no partial and no unverified byte is ever handed out. The caller owns the returned folder and must remove it."""
        if not isinstance(expected_md5, str) or not expected_md5.strip():
            raise DriveIntegrityError("There is no recorded checksum to verify the document against, so it was not served.", reason="unverifiable")
        file_id = parse_location(location)
        meta = self.client.get_file(file_id)
        if meta.trashed:
            raise DriveNotFound("The document is in the Drive trash.", status=404, reason="trashed")
        self._require_in_drive(meta)
        self._tie_to_record(meta, expected_md5, expected_size)
        directory = Path(tempfile.mkdtemp(prefix="spinco_drive_", dir=str(self._temp_root) if self._temp_root else None))
        try:
            target = directory / "document.bin"
            digest, size = hashlib.md5(), 0
            with self.client.open_download(file_id) as download, open(target, "wb") as handle:
                if download.status != 200:
                    raise DriveIntegrityError("Drive answered with a partial document, so it was not served.", reason="unverifiable")
                for chunk in download.iter_bytes():
                    digest.update(chunk)
                    handle.write(chunk)
                    size += len(chunk)
            if digest.hexdigest().lower() != expected_md5.lower():
                raise DriveIntegrityError("The downloaded document does not match its recorded checksum, so it was not served.", reason="checksum_mismatch")
            if (expected_size is not None and size != expected_size) or (meta.size is not None and size != meta.size):
                raise DriveIntegrityError("The downloaded document does not match its recorded size, so it was not served.", reason="size_mismatch")
            after = self.client.get_file(file_id)
            if after.trashed:
                raise DriveNotFound("The document is in the Drive trash.", status=404, reason="trashed")
            self._require_in_drive(after)  # moved out of the expected drive while it was being downloaded -> refused
            if after.md5 != meta.md5 or after.size != meta.size:
                raise DriveIntegrityError("The document changed while it was being downloaded, so it was not served.", reason="changed_during_download")
            return directory, target, size
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def retrieve(self, location: str, *, expected_md5: str | None = None, expected_size: int | None = None) -> bytes:
        """The document's bytes. Without a recorded checksum they are verified against Drive's own checksum of the file; WITH one
        (`expected_md5`, and optionally `expected_size`) the full chain of _download_verified applies."""
        if expected_md5 is not None:
            directory, target, _size = self._download_verified(location, expected_md5, expected_size)
            try:
                return target.read_bytes()
            finally:
                shutil.rmtree(directory, ignore_errors=True)
        file_id = parse_location(location)
        meta = self.client.get_file(file_id)
        if meta.trashed:
            raise DriveNotFound("The document is in the Drive trash.", status=404, reason="trashed")
        self._require_in_drive(meta)
        data = self.client.download_bytes(file_id)
        if meta.md5 and hashlib.md5(data).hexdigest() != meta.md5:
            raise DriveIntegrityError("Downloaded bytes do not match Drive's checksum.", reason="integrity")
        return data

    @contextmanager
    def open_stream(self, location: str, *, expected_md5: str, expected_size: int | None = None, range_header: str | None = None):
        """Context manager yielding a VerifiedDownload (status, headers, iter_bytes). The caller must authorise BEFORE calling this.

        Policy: a document is served only when its content can be tied to the exact file that was verified AND to the RECORDED checksum. The whole file
        is therefore downloaded first (see _download_verified) and the bytes are handed out only after every check has passed; `range_header` is
        applied to the VERIFIED copy (200/206/416), never forwarded to Drive. `expected_md5` is required: with nothing to verify against, nothing is
        served. Any verification failure raises a typed error on entry (DriveIntegrityError, DriveForeignObject, DriveNotADocument, DriveNotFound,
        DriveTransientError ...) and no byte is exposed."""
        directory, target, size = self._download_verified(location, expected_md5, expected_size)
        try:
            yield VerifiedDownload(target, size, range_header)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def _available(self, location: str) -> DriveObject | None:
        """The file's metadata if - and ONLY if - it is a live document of the expected Shared Drive; otherwise None (not a valid location, gone,
        in the trash, in another Shared Drive, in a My Drive, or reporting no drive at all). Fail closed: the answer is "available" only when Drive's own
        metadata says so. A Drive error (outage, quota, permission, a refused sign-in) is NOT swallowed - it propagates, so it can never read as
        "available" and never as "missing" either."""
        try:
            obj = self.inspect_unchecked(location)
        except (DriveNotFound, InvalidLocation):
            return None
        except (KeyError, TypeError, ValueError, AttributeError):
            # Drive answered, but not with a file description we can read: ownership cannot be verified. A typed, retryable error (never a True, never a crash).
            raise DriveError("Drive returned a file description that could not be understood.", reason="malformed", retryable=True) from None
        if obj.trashed:
            return None
        try:
            self._require_in_drive(obj)
        except (DriveForeignObject, DriveNotADocument):  # not in the expected drive, or a folder / shortcut / Google Docs item: not a document of ours
            return None
        return obj

    def exists(self, location: str) -> bool:
        """True only for a live document that belongs to the expected Shared Drive. False when it is gone, trashed or not ours (another Shared Drive,
        a My Drive, or no verifiable driveId): a foreign file is never a valid application document. A Drive outage or refusal raises - it must never
        read as 'missing' (or as 'there')."""
        return self._available(location) is not None

    def get_metadata(self, location: str) -> DocumentMetadata:
        """The size of a live document of the expected Shared Drive. For anything else - gone, trashed, foreign, unverifiable - `exists=False` with
        size 0: nothing about a file we do not own (or cannot vouch for) is ever reported. Drive errors propagate, as for exists()."""
        obj = self._available(location)
        if obj is None:
            return DocumentMetadata(size_bytes=0, exists=False)
        return DocumentMetadata(size_bytes=obj.size or 0, exists=True)

    def delete(self, location: str) -> None:
        """Safe-deletion semantics: this ARCHIVES (a reversible move). Nothing in this class can destroy a file."""
        try:
            self.archive(location)
        except (DriveNotFound, DriveObjectTrashed, InvalidLocation):
            return  # already gone (missing, in the trash, or no longer ours): the interface promises never to raise for that, and nothing is touched

    # ------------------------------------------------------------------ archive / restore

    def _kind_of(self, obj: DriveObject, kind: str | None) -> str:
        chosen = kind or obj.app_properties.get("kind") or "expense_report"
        if chosen not in KIND_FOLDER_NAMES:
            raise ValueError(f"unknown storage kind {chosen!r}")
        return chosen

    def archive(self, location: str, kind: str | None = None) -> DriveObject:
        """A reversible move into Archive. It NEVER changes the trashed flag: a file somebody put in the trash is refused (DriveObjectTrashed), because
        archiving must not silently undo a deliberate external deletion. Only restore() may bring a file back."""
        self._destination_ready()
        obj = self.inspect_unchecked(location)
        if obj.trashed:
            raise DriveObjectTrashed()
        self._require_in_drive(obj)
        try:
            target = self.folders.archive(self._kind_of(obj, kind))
            if obj.parents == (target,):
                return obj  # idempotent
            removing = ",".join(p for p in obj.parents if p != target) or None
            return self.client.update_file(obj.id, add_parents=target, remove_parents=removing)
        except DriveNotFound:
            # The DOCUMENT was read a moment ago; what was not found now is a folder this move needed (its destination, most often a cached folder that
            # was deleted or renamed in Drive). Forget the cached folders so that the next attempt looks them up again.
            self.folders.invalidate()
            raise DriveDestinationMissing() from None

    def restore(self, location: str, kind: str | None = None) -> DriveObject:
        """The ONLY operation that may take a file out of the trash, and only because an authorised person asked for this restore."""
        self._destination_ready()
        obj = self.inspect_unchecked(location)
        self._require_in_drive(obj)
        try:
            target = self.folders.active(self._kind_of(obj, kind))
            if obj.parents == (target,) and not obj.trashed:
                return obj  # idempotent
            removing = ",".join(p for p in obj.parents if p != target) or None
            return self.client.update_file(obj.id, add_parents=target, remove_parents=removing, trashed=False if obj.trashed else None)
        except DriveNotFound:
            self.folders.invalidate()  # see archive(): a not-found HERE concerns a destination folder, not the document
            raise DriveDestinationMissing() from None

    # ------------------------------------------------------------------ processing copy

    @contextmanager
    def local_copy(self, location: str):
        """Downloads to an ISOLATED per-call temporary directory, verifies the checksum, yields the path, and always removes it."""
        file_id = parse_location(location)
        meta = self.client.get_file(file_id)
        if meta.trashed:
            raise DriveNotFound("The document is in the Drive trash.", status=404, reason="trashed")
        self._require_in_drive(meta)
        directory = Path(tempfile.mkdtemp(prefix="spinco_drive_", dir=str(self._temp_root) if self._temp_root else None))
        try:
            target = directory / "document.pdf"
            digest = hashlib.md5()
            with self.client.open_download(file_id) as download, open(target, "wb") as handle:
                for chunk in download.iter_bytes():
                    digest.update(chunk)
                    handle.write(chunk)
            if meta.md5 and digest.hexdigest() != meta.md5:
                raise DriveIntegrityError("Downloaded bytes do not match Drive's checksum.", reason="integrity")
            yield target
        finally:
            shutil.rmtree(directory, ignore_errors=True)
