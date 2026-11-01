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
from dataclasses import dataclass
from pathlib import Path

from app.services.drive.client import (
    DriveClient,
    DriveConflict,
    DriveError,
    DriveIntegrityError,
    DriveNotFound,
    DriveObject,
)
from app.services.storage import sanitize_filename
from app.services.storage_backend import DocumentMetadata, DocumentStorage

LOCATION_PREFIX = "gdrive:"
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,100}$")

KIND_FOLDER_NAMES = {"expense_report": "Expense Reports", "trip_report": "Trip Reports", "excel": "Excel Exports"}
ARCHIVE_FOLDER_NAME = "Archive"
QUARANTINE_FOLDER_NAME = "_quarantine"


class InvalidLocation(ValueError):
    """The string is not a gdrive:<id> reference."""


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
        self._client = client
        self.root_id = root_id
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], str] = {}

    def _ensure(self, name: str, parent_id: str, role: str) -> str:
        key = (parent_id, name)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            matches = self._client.find_children(parent_id, name=name, folders_only=True)
            if matches:
                chosen = sorted(matches, key=lambda f: (f.created_time or "", f.id))[0]  # deterministic if a human made duplicates
                folder_id = chosen.id
            else:
                new_id = self._client.generate_ids()[0]
                try:
                    folder_id = self._client.create_folder(name, parent_id, file_id=new_id, app_properties={"spinco_role": role}).id
                except DriveConflict:
                    folder_id = self._client.get_file(new_id).id
            self._cache[key] = folder_id
            return folder_id

    def active(self, kind: str) -> str:
        return self._ensure(KIND_FOLDER_NAMES[kind], self.root_id, f"active:{kind}")

    def archive_root(self) -> str:
        return self._ensure(ARCHIVE_FOLDER_NAME, self.root_id, "archive")

    def archive(self, kind: str) -> str:
        return self._ensure(KIND_FOLDER_NAMES[kind], self.archive_root(), f"archive:{kind}")

    def quarantine(self) -> str:
        return self._ensure(QUARANTINE_FOLDER_NAME, self.root_id, "quarantine")

    def ensure_structure(self) -> dict[str, str]:
        structure = {"root": self.root_id, "archive": self.archive_root(), "quarantine": self.quarantine()}
        for kind in KIND_FOLDER_NAMES:
            structure[f"active:{kind}"] = self.active(kind)
            structure[f"archive:{kind}"] = self.archive(kind)
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

    def upload(self, kind: str, file_hash: str, original_filename: str, content: bytes, *, reserved_id: str | None = None) -> UploadOutcome:
        if kind not in KIND_FOLDER_NAMES:
            raise ValueError(f"unknown storage kind {kind!r}")
        md5 = hashlib.md5(content).hexdigest()  # Drive's own checksum algorithm; not used for security
        size = len(content)
        folder_id = self.folders.active(kind)

        existing = self.client.find_children(folder_id, app_property=("sha256", file_hash))
        if existing:
            found = sorted(existing, key=lambda f: (f.created_time or "", f.id))[0]
            self._verify(found, md5, size)
            return UploadOutcome(found, adopted=False, deduplicated=True)

        file_id = reserved_id or self.reserve_id()
        name = f"{file_hash[:12]}_{sanitize_filename(original_filename)}"
        properties = {"sha256": file_hash, "kind": kind, "spinco": "1"}
        try:
            created = self.client.upload_file(file_id=file_id, name=name, parent_id=folder_id, content=content, app_properties=properties)
            adopted = False
        except DriveConflict:
            created = self.client.get_file(file_id)  # an earlier, ambiguous attempt succeeded: verify it, then adopt it
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

    def info(self, location: str) -> DriveObject:
        return self.client.get_file(parse_location(location))

    def retrieve(self, location: str) -> bytes:
        file_id = parse_location(location)
        meta = self.client.get_file(file_id)
        if meta.trashed:
            raise DriveNotFound("The document is in the Drive trash.", status=404, reason="trashed")
        data = self.client.download_bytes(file_id)
        if meta.md5 and hashlib.md5(data).hexdigest() != meta.md5:
            raise DriveIntegrityError("Downloaded bytes do not match Drive's checksum.", reason="integrity")
        return data

    def open_stream(self, location: str, *, range_header: str | None = None):
        """Context manager yielding a DriveDownload (status, headers, iter_bytes). The caller must authorise BEFORE calling this."""
        return self.client.open_download(parse_location(location), range_header=range_header)

    def exists(self, location: str) -> bool:
        """False only when the file is genuinely gone or trashed. A Drive outage raises: an outage must never read as 'missing'."""
        try:
            return not self.info(location).trashed
        except (DriveNotFound, InvalidLocation):
            return False

    def get_metadata(self, location: str) -> DocumentMetadata:
        try:
            obj = self.info(location)
        except (DriveNotFound, InvalidLocation):
            return DocumentMetadata(size_bytes=0, exists=False)
        return DocumentMetadata(size_bytes=obj.size or 0, exists=not obj.trashed)

    def delete(self, location: str) -> None:
        """Safe-deletion semantics: this ARCHIVES (a reversible move). Nothing in this class can destroy a file."""
        try:
            self.archive(location)
        except (DriveNotFound, InvalidLocation):
            return  # already gone: the interface promises never to raise for that

    # ------------------------------------------------------------------ archive / restore

    def _kind_of(self, obj: DriveObject, kind: str | None) -> str:
        chosen = kind or obj.app_properties.get("kind") or "expense_report"
        if chosen not in KIND_FOLDER_NAMES:
            raise ValueError(f"unknown storage kind {chosen!r}")
        return chosen

    def archive(self, location: str, kind: str | None = None) -> DriveObject:
        obj = self.info(location)
        target = self.folders.archive(self._kind_of(obj, kind))
        if obj.parents == (target,) and not obj.trashed:
            return obj  # idempotent
        removing = ",".join(p for p in obj.parents if p != target) or None
        return self.client.update_file(obj.id, add_parents=target, remove_parents=removing, trashed=False if obj.trashed else None)

    def restore(self, location: str, kind: str | None = None) -> DriveObject:
        obj = self.info(location)
        target = self.folders.active(self._kind_of(obj, kind))
        if obj.parents == (target,) and not obj.trashed:
            return obj  # idempotent
        removing = ",".join(p for p in obj.parents if p != target) or None
        return self.client.update_file(obj.id, add_parents=target, remove_parents=removing, trashed=False if obj.trashed else None)

    # ------------------------------------------------------------------ processing copy

    @contextmanager
    def local_copy(self, location: str):
        """Downloads to an ISOLATED per-call temporary directory, verifies the checksum, yields the path, and always removes it."""
        file_id = parse_location(location)
        meta = self.client.get_file(file_id)
        if meta.trashed:
            raise DriveNotFound("The document is in the Drive trash.", status=404, reason="trashed")
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
