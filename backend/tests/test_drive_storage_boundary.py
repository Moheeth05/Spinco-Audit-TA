"""Phase 3A.2-E - the Shared Drive boundary of exists() / get_metadata(), and honest MISSING_EXTERNAL guidance.

  * exists() is True, and get_metadata() reports a size, ONLY for a live document that Drive's own metadata puts in the expected Shared Drive. A file in
    another Shared Drive, in a My Drive, with no (or an empty) driveId, trashed, deleted or moved out is "not there": exists=False, size 0, nothing about
    it is reported. Drive errors are never swallowed (an outage reads neither as 'missing' nor as 'available').
  * The message an administrator gets for a MISSING_EXTERNAL document no longer promises that re-uploading repairs it (it cannot: the same bytes are
    recognised as the same document and the record is returned unchanged). Repair itself is NOT implemented here (Phase 3B).
  * The local filesystem storage is unchanged.

All of it runs against the FAKE Drive (httpx.MockTransport) and a temporary database: no call to Google, no credential."""

import ast
import hashlib
from dataclasses import asdict
from pathlib import Path

import pytest

from app.database import Base, SessionLocal, engine
from app.models import storage_record as sr
from app.services.drive.client import DriveError, DrivePermissionError, DriveTransientError
from app.services.drive.errors import DriveIdentityMismatch, DriveNotADocument
from app.services.drive.lifecycle import DESTINATION_MISSING_TEXT, MISSING_EXTERNAL_GUIDANCE, StorageStateError
from app.services.drive.storage import parse_location
from app.services.storage_backend import DocumentMetadata, LocalFilesystemStorage

import boundary_scan
from drive_testkit import make_stack, pdf_bytes
from test_drive_guard import KIND, OTHER, SHARED, guarded, sha, snapshot, writes

APP_DIR = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def saved(stack, marker="doc"):
    content = pdf_bytes(marker)
    location = stack.storage.save(KIND, sha(content), f"{marker}.pdf", content)
    return location, parse_location(location), content


def media_requests(stack, start):
    return [r for r in stack.fake.requests[start:] if r["query"].get("alt") == "media"]


NOT_THERE = DocumentMetadata(size_bytes=0, exists=False)


# ====================================================================================================================== the boundary


def test_a_live_file_in_the_expected_shared_drive_exists_and_reports_its_size(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    assert stack.storage.exists(location) is True
    assert stack.storage.get_metadata(location) == DocumentMetadata(size_bytes=len(content), exists=True)


def test_a_file_in_a_different_shared_drive_is_not_a_document(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    stack.fake.files[file_id]["driveId"] = OTHER
    before, start = snapshot(stack), len(stack.fake.requests)
    assert stack.storage.exists(location) is False
    assert stack.storage.get_metadata(location) == NOT_THERE
    assert snapshot(stack) == before and writes(stack, start) == [] and media_requests(stack, start) == []  # looked at, never touched or downloaded


@pytest.mark.parametrize("drive_id", [None, ""], ids=["no driveId (a My Drive item)", "empty driveId"])
def test_a_file_without_a_verifiable_drive_id_fails_closed(tmp_path, drive_id):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    if drive_id is None:
        stack.fake.files[file_id].pop("driveId", None)
    else:
        stack.fake.files[file_id]["driveId"] = drive_id
    assert stack.storage.exists(location) is False
    assert stack.storage.get_metadata(location) == NOT_THERE


@pytest.mark.parametrize("destination", [OTHER, None], ids=["another Shared Drive", "a My Drive"])
def test_a_file_moved_outside_the_expected_drive_stops_existing_and_returns_when_moved_back(tmp_path, destination):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    assert stack.storage.exists(location)
    stack.fake.external_move_to_drive(file_id, destination)
    assert stack.storage.exists(location) is False and stack.storage.get_metadata(location) == NOT_THERE
    stack.fake.external_move_to_drive(file_id, SHARED)
    assert stack.storage.exists(location) is True and stack.storage.get_metadata(location).size_bytes == len(content)


def test_gone_trashed_and_invalid_locations_are_not_there(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    stack.fake.external_trash(file_id)
    assert stack.storage.exists(location) is False and stack.storage.get_metadata(location) == NOT_THERE  # a trashed file reports no size either
    stack.fake.external_untrash(file_id)
    assert stack.storage.exists(location) is True
    stack.fake.external_delete(file_id)
    assert stack.storage.exists(location) is False and stack.storage.get_metadata(location) == NOT_THERE
    for bad in ("gdrive:neverexisted0001", "not-a-location", "", "gdrive:", "/some/local/path.pdf"):
        assert stack.storage.exists(bad) is False and stack.storage.get_metadata(bad) == NOT_THERE


def test_foreign_file_metadata_is_never_returned(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack, "big-" + "x" * 40)
    assert len(content) > 0
    stack.fake.files[file_id]["driveId"] = OTHER
    meta = stack.storage.get_metadata(location)
    assert asdict(meta) == {"size_bytes": 0, "exists": False}  # no size, and no other field to carry anything about the file
    assert str(len(content)) not in repr(meta)


def test_a_drive_error_while_verifying_is_never_swallowed_in_either_direction(tmp_path):
    stack, _ = guarded(tmp_path, max_attempts=2)
    location, file_id, content = saved(stack)
    stack.fake.fail_next(50, 503)
    with pytest.raises(DriveTransientError):  # an outage reads neither as "missing" nor as "available"
        stack.storage.exists(location)
    with pytest.raises(DriveTransientError):
        stack.storage.get_metadata(location)
    stack.fake.faults.clear()
    stack.fake.fail_next(50, 403, "forbidden")  # a permission refusal on the file itself: fail closed too
    with pytest.raises(DrivePermissionError):
        stack.storage.exists(location)
    with pytest.raises(DrivePermissionError):
        stack.storage.get_metadata(location)
    stack.fake.faults.clear()
    stack.fake.fail_next(1, 503)  # one hiccup is absorbed by the client's own retry and the file is then verified normally
    assert stack.storage.exists(location) is True


def test_a_malformed_drive_answer_never_yields_a_true(tmp_path):
    stack, _ = guarded(tmp_path, max_attempts=2)
    location, file_id, content = saved(stack)
    stack.fake.fail_next(50, 200, "ok", body={"unexpected": "shape"})  # a 200 without the fields: no id, no driveId, no trashed flag
    with pytest.raises(DriveError) as caught:  # a typed, retryable error - not a crash, and certainly not "True"
        stack.storage.exists(location)
    assert caught.value.reason == "malformed" and caught.value.retryable
    with pytest.raises(DriveError):
        stack.storage.get_metadata(location)
    stack.fake.faults.clear()
    assert stack.storage.exists(location) is True  # and nothing is remembered: the next answer is judged on its own


def test_the_raw_accessor_still_shows_a_foreign_file_so_that_reconciliation_can_mark_it(tmp_path, db):
    stack, _ = guarded(tmp_path)
    result = stack.manager.store(db, kind=KIND, content=pdf_bytes("recon"), original_filename="r.pdf")
    file_id = parse_location(result.record.location)
    stack.fake.external_move_to_drive(file_id, OTHER)
    raw = stack.storage.inspect_unchecked(result.record.location)
    assert raw.drive_id == OTHER
    assert not hasattr(stack.storage, "info")  # 3A.2-E.1: the old alias is GONE (it was the easiest accidental bypass), not merely fenced
    assert stack.storage.exists(result.record.location) is False  # while the checked accessors refuse it
    report = stack.manager.reconcile(db)
    db.refresh(result.record)
    assert report.foreign_drive == 1 and result.record.state == sr.MISSING_EXTERNAL  # reconciliation still works on the raw view


def test_an_unguarded_harness_client_has_no_expected_drive_and_keeps_its_old_behaviour(tmp_path):
    # Only a harness or a fake builds an unguarded client (and only when the environment is explicitly dev or staging). With no expected drive there is
    # nothing to compare against, so nothing changes for it; the application's runtime always builds a guarded one.
    stack = make_stack(tmp_path)
    location, file_id, content = saved(stack)
    assert stack.client.expected_drive_id is None
    assert stack.storage.exists(location) is True and stack.storage.get_metadata(location).size_bytes == len(content)


def test_no_application_code_outside_the_drive_package_can_reach_the_unchecked_metadata_the_client_reads_or_the_raw_downloads():
    assert boundary_scan.scan_tree(APP_DIR) == []  # the whole application tree, by every rule of tests/boundary_scan.py (names, strings, imports, dynamics)


def test_inside_the_drive_package_each_sensitive_name_is_confined_to_the_files_that_need_it():
    offenders = []
    for path in sorted((APP_DIR / "services" / "drive").glob("*.py")):
        offenders += boundary_scan.scan_inside(path.read_text(encoding="utf-8"), f"services/drive/{path.name}")
    assert offenders == []
    used = {}
    for path in (APP_DIR / "services" / "drive").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in boundary_scan.INSIDE_FENCE:
                used.setdefault(node.attr, set()).add(path.name)
    assert used["inspect_unchecked"] == {"storage.py", "lifecycle.py"}  # the fences are tight: each allowed file really uses its name


def test_the_unchecked_alias_does_not_exist_anywhere_in_the_drive_package():
    from app.services.drive.storage import GoogleDriveStorage

    assert not hasattr(GoogleDriveStorage, "info") and hasattr(GoogleDriveStorage, "inspect_unchecked")
    assert "def info" not in (APP_DIR / "services" / "drive" / "storage.py").read_text(encoding="utf-8")


# The twelve ways found by the independent review (and a few siblings) that an application module could have reached the unchecked view, the
# client-level reads or the raw downloads. Each is written the way a developer might write it, into a module that is NOT part of the Drive package.
BYPASSES = {
    "1 direct inspect_unchecked": ("api/leak.py", "def f(s, loc):\n    return s.inspect_unchecked(loc)\n"),
    "2 client open_download": ("api/leak.py", "def f(s, i):\n    return s.client.open_download(i)\n"),
    "3 client download_bytes": ("api/leak.py", "def f(s, i):\n    return s.client.download_bytes(i)\n"),
    "4 the old info alias": ("api/leak.py", "def f(s, loc):\n    return s.info(loc)\n"),
    "5 client get_file": ("api/leak.py", "def f(s, i):\n    return s.client.get_file(i)\n"),
    "6 client get_raw": ("api/leak.py", "def f(s, i):\n    return s.client.get_raw(i, 'id,driveId')\n"),
    "7 client _request": ("api/leak.py", "def f(s, i):\n    return s.client._request('GET', 'https://x/files/' + i)\n"),
    "8 dynamic getattr with a folded name": ("api/leak.py", "def f(s, loc):\n    return getattr(s, 'inspect_' + 'unchecked')(loc)\n"),
    "9 dynamic getattr of info": ("api/leak.py", "def f(storage, loc):\n    return getattr(storage, 'info')(loc)\n"),
    "10 class __dict__ lookup": ("api/leak.py", "from app.services.drive.storage import GoogleDriveStorage\nfn = GoogleDriveStorage.__dict__['inspect_unchecked']\n"),
    "11 importlib plus attribute access": ("api/leak.py", "import importlib\nG = importlib.import_module('app.services.drive.storage').GoogleDriveStorage\nfn = G.info\n"),
    "12 subclass overriding the safe check": ("api/leak.py", "from app.services.drive.storage import GoogleDriveStorage\nclass Mine(GoogleDriveStorage):\n    def exists(self, location):\n        return True\n"),
    # siblings
    "13 f-string built name": ("services/leak.py", "def f(s, loc):\n    part = 'unchecked'\n    return getattr(s, f'inspect_unchecked')(loc)\n"),
    "14 importing the drive package from an ordinary module": ("services/leak.py", "from app.services.drive import lifecycle\n"),
    "15 from app.services import drive": ("services/leak.py", "from app.services import drive\n"),
    "16 importing the client": ("services/leak.py", "from app.services.drive.client import DriveClient\n"),
    "17 __import__ of the package": ("services/leak.py", "m = __import__('app.services.drive.client')\n"),
    "18 attrgetter": ("services/leak.py", "import operator\nf = operator.attrgetter('get_' + 'file')\n"),
    "19 dynamic access inside the ONE importing module": ("api/storage.py", "from app.services.drive.lifecycle import StorageManager\ndef f(m, loc):\n    return getattr(m.drive, 'inspect_' + 'unchecked')(loc)\n"),
    "20 __dict__ inside the ONE importing module": ("api/storage.py", "from app.services.drive.lifecycle import StorageManager\ndef f(m):\n    return m.drive.__dict__\n"),
}


@pytest.mark.parametrize("label", list(BYPASSES))
def test_every_known_bypass_pattern_is_detected(label):
    relative, source = BYPASSES[label]
    assert boundary_scan.scan_source(source, relative), f"the scan did not notice: {label}"


def test_the_scan_has_no_false_alarm_on_ordinary_code():
    clean = {
        "api/other.py": "import logging\nlogger = logging.getLogger('x')\ndef f():\n    logger.info('hello')\n    self_logger = logger\n    self_logger.info('x')\n",
        "services/other.py": "class A:\n    def __init__(self):\n        self.logger = None\n    def go(self):\n        self.logger.info('x')\n        return self.info_text\n",
        "api/storage.py": "from app.services.drive.client import DriveError, DriveIntegrityError, DriveNotFound\nfrom app.services.drive.lifecycle import StorageManager\n",
    }
    for relative, source in clean.items():
        assert boundary_scan.scan_source(source, relative) == [], relative


def test_the_one_module_that_may_import_the_drive_package_still_may_not_use_the_forbidden_names():
    assert boundary_scan.scan_source("from app.services.drive.lifecycle import StorageManager\ndef f(m, i):\n    return m.drive.client.get_file(i)\n", "api/storage.py")
    assert boundary_scan.scan_source("from app.services.drive.storage import GoogleDriveStorage\n", "api/storage.py")  # even it may not import the storage module


# ====================================================================================================================== MISSING_EXTERNAL guidance


@pytest.mark.parametrize("how", ["deleted", "moved to another Shared Drive", "moved to a My Drive"])
def test_the_restore_message_for_a_missing_document_is_accurate(tmp_path, db, how):
    stack, _ = guarded(tmp_path)
    result = stack.manager.store(db, kind=KIND, content=pdf_bytes("msg-" + how[:4]), original_filename="m.pdf")
    file_id = parse_location(result.record.location)
    if how == "deleted":
        stack.fake.external_delete(file_id)
    else:
        stack.fake.external_move_to_drive(file_id, OTHER if "Shared" in how else None)
    stack.manager.reconcile(db)
    db.refresh(result.record)
    assert result.record.state == sr.MISSING_EXTERNAL
    with pytest.raises(StorageStateError) as caught:
        stack.manager.restore(db, result.record.id, user_id=None)
    text = str(caught.value)
    assert "must be re-uploaded" not in text and "re-uploaded" not in text  # the old, false promise
    assert "Uploading the same file again does not replace it" in text and "automatic re-creation is not supported yet" in text
    assert "Drive administrator" in text and "reconciliation" in text
    assert MISSING_EXTERNAL_GUIDANCE in text
    assert "gdrive:" not in text and file_id not in text and SHARED not in text and OTHER not in text  # no ids in a user-facing message
    db.refresh(result.record)
    assert result.record.state == sr.MISSING_EXTERNAL  # nothing was changed by the refused restore


def test_uploading_the_same_file_again_really_is_not_a_repair_so_the_message_stays_true(tmp_path, db):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("same-again")
    first = stack.manager.store(db, kind=KIND, content=content, original_filename="s.pdf")
    stack.fake.external_delete(parse_location(first.record.location))
    stack.manager.reconcile(db)
    db.refresh(first.record)
    assert first.record.state == sr.MISSING_EXTERNAL
    files_before, location_before = stack.fake.file_count(), first.record.location
    again = stack.manager.store(db, kind=KIND, content=content, original_filename="s.pdf")
    db.refresh(first.record)
    assert again.duplicate is True and again.record.id == first.record.id  # recognised as the same document ...
    assert first.record.state == sr.MISSING_EXTERNAL and first.record.location == location_before and stack.fake.file_count() == files_before  # ... and nothing was repaired


def test_a_trashed_document_is_still_restorable_so_it_gets_no_such_message(tmp_path, db):
    stack, _ = guarded(tmp_path)
    result = stack.manager.store(db, kind=KIND, content=pdf_bytes("trash-ok"), original_filename="t.pdf")
    stack.fake.external_trash(parse_location(result.record.location))
    stack.manager.reconcile(db)
    stack.manager.restore(db, result.record.id, user_id=None)  # the supported repair for a trashed file is unchanged
    db.refresh(result.record)
    assert result.record.state == sr.STORED


def test_the_failed_upload_message_that_does_say_upload_again_is_still_true(tmp_path, db):
    stack, _ = guarded(tmp_path)  # a pending upload whose bytes were lost: only the user can supply them again, and then the same bytes DO finish it
    stack.storage.reserve_id()
    content = pdf_bytes("lost-spool")
    from test_drive_guard_corrections import pending_record  # the shared helper that builds an old UPLOADING record with its own spool

    record = pending_record(db, stack, content, stack.storage.reserve_id())
    stack.manager._unspool(record)
    stack.manager.initialise_structure()
    stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.FAILED and "uploaded again" in record.last_error
    again = stack.manager.store(db, kind=KIND, content=content, original_filename="p.pdf")
    db.refresh(record)
    assert again.duplicate and record.state == sr.STORED and stack.fake.file_count() == 1


# ====================================================================================================================== local storage unchanged


def test_the_local_filesystem_storage_behaves_exactly_as_before(tmp_path, monkeypatch):
    import app.services.storage_backend as backend

    monkeypatch.setitem(backend._KIND_DIRS, "expense_report", tmp_path / "expense")
    local = LocalFilesystemStorage()
    content = b"%PDF-1.4 local"
    location = local.save("expense_report", "a" * 64, "My Bill.pdf", content)
    assert Path(location).parent == tmp_path / "expense" and Path(location).name.startswith("a" * 12 + "_")
    assert local.exists(location) is True and local.retrieve(location) == content
    assert local.get_metadata(location) == DocumentMetadata(size_bytes=len(content), exists=True)
    assert local.local_path(location) == Path(location)
    local.delete(location)
    local.delete(location)  # never raises when already gone
    assert local.exists(location) is False and local.get_metadata(location) == DocumentMetadata(size_bytes=0, exists=False)
    assert local.exists("") is False
    # a file that merely LOOKS like another backend's location is just a path to it: the local backend knows no drive boundary
    assert not hasattr(local, "inspect_unchecked") and not hasattr(local, "expected_drive_id")


def test_the_default_process_storage_is_still_the_local_backend():
    from app.services.storage_backend import storage

    assert isinstance(storage, LocalFilesystemStorage)  # Drive is not wired into the application by this phase


# ====================================================================================================================== 3A.2-E.1: documents only, identity, destination messaging

NOT_DOCUMENT_TYPES = [
    "application/vnd.google-apps.folder",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
    "",
]


@pytest.mark.parametrize("mime", NOT_DOCUMENT_TYPES, ids=lambda m: m.rsplit(".", 1)[-1] or "blank")
def test_a_folder_a_shortcut_or_a_google_docs_item_is_not_an_uploaded_document(tmp_path, mime):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    stack.fake.files[file_id]["mimeType"] = mime
    start = len(stack.fake.requests)
    assert stack.storage.exists(location) is False and stack.storage.get_metadata(location) == NOT_THERE
    checksum = hashlib.md5(content).hexdigest()
    with pytest.raises(DriveNotADocument):
        stack.storage.retrieve(location)
    with pytest.raises(DriveNotADocument):
        with stack.storage.open_stream(location, expected_md5=checksum):
            pass
    with pytest.raises(DriveNotADocument):
        with stack.storage.local_copy(location):
            pass
    with pytest.raises(DriveNotADocument):
        stack.storage.archive(location, KIND)
    with pytest.raises(DriveNotADocument):
        stack.storage.restore(location, KIND)
    assert media_requests(stack, start) == [] and writes(stack, start) == []


@pytest.mark.parametrize("mime", ["application/pdf", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/octet-stream", "image/png"])
def test_ordinary_uploaded_files_of_any_normal_type_are_still_documents(tmp_path, mime):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    stack.fake.files[file_id]["mimeType"] = mime
    assert stack.storage.exists(location) is True and stack.storage.get_metadata(location).size_bytes == len(content)


def described_as(file_id, **overrides):
    body = {"id": file_id, "name": "x.pdf", "mimeType": "application/pdf", "driveId": SHARED, "trashed": False, "size": "10", "parents": ["ROOTFOLDER0000001"]}
    body.update(overrides)
    return body


def answer_metadata_with(stack, file_id, body):
    stack.fake.fail_next(50, 200, "ok", body=body, match=lambda r: r.method == "GET" and r.url.path.endswith(f"/files/{file_id}") and "alt" not in dict(r.url.params))


def test_an_answer_that_describes_a_different_file_is_refused_everywhere(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    answer_metadata_with(stack, file_id, described_as("SOMEOTHERFILEID01"))
    start = len(stack.fake.requests)
    for call in (stack.storage.exists, stack.storage.get_metadata, stack.storage.inspect_unchecked, stack.storage.retrieve):
        with pytest.raises(DriveIdentityMismatch):
            call(location)
    with pytest.raises(DriveIdentityMismatch):
        stack.client.get_file(file_id)
    assert media_requests(stack, start) == []
    stack.fake.faults.clear()
    assert stack.storage.exists(location) is True  # a true answer is judged on its own: nothing is remembered


def test_reconciliation_does_not_judge_a_record_from_an_answer_about_another_file(tmp_path, db):
    stack, _ = guarded(tmp_path)
    result = stack.manager.store(db, kind=KIND, content=pdf_bytes("recon-id"), original_filename="r.pdf")
    file_id = parse_location(result.record.location)
    answer_metadata_with(stack, file_id, described_as("SOMEOTHERFILEID01", trashed=True))  # would otherwise read as "trashed"
    report = stack.manager.reconcile(db)
    db.refresh(result.record)
    assert result.record.state == sr.STORED and report.missing_external == 0 and any("DriveIdentityMismatch" in e for e in report.errors)


def test_reconciliation_marks_a_record_whose_item_became_a_folder_or_shortcut_and_never_serves_it(tmp_path, db):
    stack, _ = guarded(tmp_path)
    result = stack.manager.store(db, kind=KIND, content=pdf_bytes("became-folder"), original_filename="f.pdf")
    file_id = parse_location(result.record.location)
    stack.fake.files[file_id]["mimeType"] = "application/vnd.google-apps.shortcut"
    report = stack.manager.reconcile(db)
    db.refresh(result.record)
    assert result.record.state == sr.MISSING_EXTERNAL and report.not_documents == 1
    assert len(events(db, result.record, "EXTERNAL_NOT_A_DOCUMENT")) == 1
    for _ in range(3):  # repeated passes add nothing but the single "recovery rejected" note
        again = stack.manager.reconcile(db)
        assert again.not_documents == 0
    assert len(events(db, result.record, "RECOVERY_REJECTED")) == 1 and len(events(db, result.record, "EXTERNAL_NOT_A_DOCUMENT")) == 1
    stack.fake.files[file_id]["mimeType"] = "application/pdf"  # the real file is back
    stack.manager.reconcile(db)
    db.refresh(result.record)
    assert result.record.state == sr.STORED


def stored_and_archived(stack, db, marker):
    result = stack.manager.store(db, kind=KIND, content=pdf_bytes(marker), original_filename=f"{marker}.pdf")
    stack.manager.archive(db, result.record.id, user_id=None)
    return result.record


def test_a_deleted_destination_folder_is_reported_as_the_folder_not_the_document_and_the_retry_works(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record = stored_and_archived(stack, db, "dest-restore")
    file_id = parse_location(record.location)
    stack.fake.external_delete(stack.folders.active(KIND))  # an administrator removes "Expense Reports"; the process still holds its cached id
    with pytest.raises(StorageStateError) as caught:
        stack.manager.restore(db, record.id, user_id=None)
    text = str(caught.value)
    assert DESTINATION_MISSING_TEXT in text and "folder" in text and "not touched" in text
    assert "permanently deleted" not in text and "was not found in Drive" not in text and "re-uploaded" not in text  # NOT the document's story
    assert "gdrive:" not in text and file_id not in text and SHARED not in text
    db.refresh(record)
    assert record.state == sr.ARCHIVED and file_id in stack.fake.files and not stack.fake.files[file_id]["trashed"]  # nothing changed anywhere
    stack.manager.restore(db, record.id, user_id=None)  # the cache was dropped, so the next attempt looks the folder up again (and re-creates it)
    db.refresh(record)
    assert record.state == sr.STORED and stack.fake.files[file_id]["parents"] == [stack.folders.active(KIND)]


def test_a_deleted_archive_folder_is_reported_the_same_way_when_archiving(tmp_path, db):
    stack, _ = guarded(tmp_path)
    first = stored_and_archived(stack, db, "dest-first")  # creates and caches the archive folders
    second = stack.manager.store(db, kind=KIND, content=pdf_bytes("dest-second"), original_filename="s.pdf").record
    stack.fake.external_delete(stack.folders.archive(KIND))
    with pytest.raises(StorageStateError) as caught:
        stack.manager.archive(db, second.id, user_id=None)
    assert DESTINATION_MISSING_TEXT in str(caught.value) and "not archived" in str(caught.value) and "no longer exists" not in str(caught.value)
    db.refresh(second)
    assert second.state == sr.STORED and first.state == sr.ARCHIVED
    stack.manager.archive(db, second.id, user_id=None)
    db.refresh(second)
    assert second.state == sr.ARCHIVED


def test_a_document_that_really_is_gone_still_gets_the_document_not_found_message(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record = stored_and_archived(stack, db, "really-gone")
    stack.fake.external_delete(parse_location(record.location))
    with pytest.raises(StorageStateError) as caught:
        stack.manager.restore(db, record.id, user_id=None)
    assert "was not found in Drive" in str(caught.value) and DESTINATION_MISSING_TEXT not in str(caught.value) and MISSING_EXTERNAL_GUIDANCE in str(caught.value)


def test_the_guidance_distinguishes_the_original_file_from_a_new_copy():
    assert "ORIGINAL file" in MISSING_EXTERNAL_GUIDANCE and "new copy" in MISSING_EXTERNAL_GUIDANCE
    assert "does not replace it" in MISSING_EXTERNAL_GUIDANCE and "automatic re-creation is not supported yet" in MISSING_EXTERNAL_GUIDANCE


def events(db, record, event_type):
    from sqlalchemy import select

    from app.models.storage_record import StorageEvent

    return db.execute(select(StorageEvent).where(StorageEvent.record_id == record.id, StorageEvent.event_type == event_type)).scalars().all()
