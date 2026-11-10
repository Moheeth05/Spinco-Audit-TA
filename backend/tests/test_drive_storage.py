"""GoogleDriveStorage (the DocumentStorage adapter). FAKE-DRIVE tests of OUR logic; see fake_drive.py for what that does and does not prove."""

import hashlib
import threading

import httpx
import pytest

from app.services.drive.client import DriveConflict, DriveIntegrityError, DriveNotFound, DriveTransientError
from app.services.drive.storage import GoogleDriveStorage, InvalidLocation, is_drive_location, make_location, parse_location
from app.services.storage_backend import DocumentStorage, LocalFilesystemStorage

from drive_testkit import make_stack, pdf_bytes

KIND = "expense_report"


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def folder_named(stack, name, parent=None):
    found = [f for f in stack.fake.by_name(name) if parent is None or parent in f["parents"]]
    assert len(found) == 1, f"expected exactly one folder {name!r}, found {len(found)}"
    return found[0]


# ----------------------------------------------------------------------------------------------------------------- locations


def test_locations_are_opaque_gdrive_references():
    assert make_location("fakeid000000000001") == "gdrive:fakeid000000000001"
    assert parse_location("gdrive:fakeid000000000001") == "fakeid000000000001"
    assert is_drive_location("gdrive:fakeid000000000001") and not is_drive_location("D:\\storage\\a.pdf") and not is_drive_location(None)
    for bad in ("", "gdrive:", "gdrive:short", "gdrive:../../etc/passwd", "gdrive:has space0000", "D:\\x.pdf", "http://drive.google.com/file/d/abc", None, 5):
        with pytest.raises(InvalidLocation):
            parse_location(bad)


def test_the_adapter_is_a_drop_in_documentstorage_and_the_local_backend_is_untouched():
    assert issubclass(GoogleDriveStorage, DocumentStorage)
    from app.services import storage_backend

    assert isinstance(storage_backend.storage, LocalFilesystemStorage)  # the global backend was NOT switched


# ----------------------------------------------------------------------------------------------------------------- folder structure


def test_the_folder_structure_is_created_once_under_the_root_and_never_duplicated(tmp_path):
    stack = make_stack(tmp_path)
    structure = stack.folders.ensure_structure()
    again = stack.folders.ensure_structure()
    assert structure == again
    root = stack.config.root_folder_id
    top_level = sorted(f["name"] for f in stack.fake.files.values() if root in f["parents"])
    assert top_level == ["Archive", "Duplicates", "Excel Exports", "Expense Reports", "Trip Reports", "_quarantine"]
    assert len([f for f in stack.fake.files.values() if f["name"] == "Archive"]) == 1
    assert len([f for f in stack.fake.files.values() if f["name"] == "Expense Reports" and root in f["parents"]]) == 1
    archive = folder_named(stack, "Archive")
    assert len([f for f in stack.fake.files.values() if f["name"] == "Trip Reports" and archive["id"] in f["parents"]]) == 1


def test_existing_company_folders_are_adopted_not_replaced_and_duplicates_resolve_deterministically(tmp_path):
    stack = make_stack(tmp_path)
    root = stack.config.root_folder_id
    first = stack.fake._add("humanfolder0000001", "Expense Reports", "application/vnd.google-apps.folder", [root])
    stack.fake._add("humanfolder0000002", "Expense Reports", "application/vnd.google-apps.folder", [root])  # a human made a second one
    assert stack.folders.active(KIND) == first["id"]  # the oldest wins, nothing is created, nothing is renamed or deleted
    assert len([f for f in stack.fake.files.values() if f["name"] == "Expense Reports" and root in f["parents"]]) == 2


def test_concurrent_first_use_creates_each_folder_once(tmp_path):
    stack = make_stack(tmp_path)
    barrier = threading.Barrier(5)
    ids = []

    def go():
        barrier.wait(timeout=10)
        ids.append(stack.folders.active(KIND))

    threads = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert len(set(ids)) == 1
    assert len([f for f in stack.fake.files.values() if f["name"] == "Expense Reports"]) == 1


# ----------------------------------------------------------------------------------------------------------------- save / retrieve


def test_save_stores_a_verified_file_in_the_active_folder_with_integrity_metadata(tmp_path):
    stack = make_stack(tmp_path)
    content = pdf_bytes("one")
    location = stack.storage.save(KIND, sha(content), "ER-86005 report.pdf", content)
    assert location.startswith("gdrive:")
    obj = stack.storage.inspect_unchecked(location)
    assert obj.parents == (stack.folders.active(KIND),)
    assert obj.name == f"{sha(content)[:12]}_ER-86005 report.pdf"
    assert obj.size == len(content) and obj.md5 == hashlib.md5(content).hexdigest()
    assert obj.app_properties == {"sha256": sha(content), "kind": KIND, "spinco": "1"}
    assert stack.storage.retrieve(location) == content
    assert sha(stack.storage.retrieve(location)) == sha(content)  # SHA-256 round trip


def test_identical_bytes_never_create_a_second_drive_file(tmp_path):
    stack = make_stack(tmp_path)
    content = pdf_bytes("dup")
    first = stack.storage.save(KIND, sha(content), "a.pdf", content)
    second = stack.storage.save(KIND, sha(content), "renamed copy.pdf", content)
    assert first == second and stack.fake.file_count() == 1


def test_the_same_bytes_may_exist_as_an_expense_and_a_trip_report_like_today(tmp_path):
    stack = make_stack(tmp_path)
    content = pdf_bytes("both")
    a = stack.storage.save("expense_report", sha(content), "a.pdf", content)
    b = stack.storage.save("trip_report", sha(content), "a.pdf", content)
    assert a != b and stack.fake.file_count() == 2


def test_an_ambiguous_failure_is_recovered_by_adopting_the_file_the_earlier_attempt_created(tmp_path):
    stack = make_stack(tmp_path, max_attempts=1)  # surface the lost response instead of retrying inside the client
    content = pdf_bytes("lost")
    reserved = stack.storage.reserve_id()
    stack.fake.lose_response_after_next_upload = True
    with pytest.raises(DriveTransientError):
        stack.storage.upload(KIND, sha(content), "a.pdf", content, reserved_id=reserved)
    assert stack.fake.file_count() == 1  # the upload really did succeed in Drive
    outcome = stack.storage.upload(KIND, sha(content), "a.pdf", content, reserved_id=reserved)
    assert stack.fake.file_count() == 1  # no duplicate
    # (the dedupe-by-hash lookup finds it first; both routes lead to the same single file)
    assert outcome.obj.id == reserved


def test_a_conflicting_reserved_id_is_verified_before_it_is_adopted(tmp_path):
    stack = make_stack(tmp_path)
    content = pdf_bytes("adopt")
    reserved = stack.storage.reserve_id()
    folder = stack.folders.active(KIND)
    stack.fake._add(reserved, "pre-existing.pdf", "application/pdf", [folder], content=content)  # an earlier attempt, with NO sha property
    outcome = stack.storage.upload(KIND, sha(content), "a.pdf", content, reserved_id=reserved)
    assert outcome.adopted and not outcome.deduplicated and stack.fake.file_count() == 1
    other = pdf_bytes("different")
    stack.fake._add(stack.storage.reserve_id(), "x.pdf", "application/pdf", [folder], content=other)  # unrelated, different id
    clash = stack.storage.reserve_id()
    stack.fake._add(clash, "clash.pdf", "application/pdf", [folder], content=b"completely different bytes")
    with pytest.raises(DriveIntegrityError):  # same id but different bytes: never silently adopted
        stack.storage.upload(KIND, sha(other), "b.pdf", other, reserved_id=clash)


def test_a_checksum_mismatch_is_quarantined_and_never_reported_stored(tmp_path):
    stack = make_stack(tmp_path)
    content = pdf_bytes("corrupt")
    stack.fake.corrupt_next_upload = True
    with pytest.raises(DriveIntegrityError):
        stack.storage.save(KIND, sha(content), "a.pdf", content)
    quarantine = stack.folders.quarantine()
    in_quarantine = [f for f in stack.fake.files.values() if quarantine in f["parents"]]
    assert len(in_quarantine) == 1  # moved aside, not deleted
    assert not [f for f in stack.fake.files.values() if stack.folders.active(KIND) in f["parents"]]


def test_a_download_that_does_not_match_the_checksum_is_refused(tmp_path):
    stack = make_stack(tmp_path)
    content = pdf_bytes("bits")
    location = stack.storage.save(KIND, sha(content), "a.pdf", content)
    stack.fake.files[parse_location(location)]["md5Checksum"] = "1" * 32
    with pytest.raises(DriveIntegrityError):
        stack.storage.retrieve(location)


def test_streaming_supports_ranges(tmp_path):
    stack = make_stack(tmp_path)
    content = b"%PDF-" + bytes(range(256)) * 4
    location = stack.storage.save(KIND, sha(content), "a.pdf", content)
    checksum = hashlib.md5(content).hexdigest()  # 3A.2-E.1: a document is served only against its RECORDED checksum; ranges are cut from the verified copy
    with stack.storage.open_stream(location, expected_md5=checksum, expected_size=len(content), range_header="bytes=0-9") as download:
        assert download.status == 206 and b"".join(download.iter_bytes()) == content[:10]
    with stack.storage.open_stream(location, expected_md5=checksum, expected_size=len(content)) as download:
        assert download.status == 200 and b"".join(download.iter_bytes()) == content


# ----------------------------------------------------------------------------------------------------------------- exists / metadata


def test_exists_is_false_only_when_the_file_is_gone_or_trashed_never_on_an_outage(tmp_path):
    stack = make_stack(tmp_path, max_attempts=2)
    content = pdf_bytes("e")
    location = stack.storage.save(KIND, sha(content), "a.pdf", content)
    file_id = parse_location(location)
    assert stack.storage.exists(location) and stack.storage.get_metadata(location).size_bytes == len(content)
    stack.fake.external_trash(file_id)
    assert not stack.storage.exists(location) and not stack.storage.get_metadata(location).exists
    stack.fake.external_untrash(file_id)
    assert stack.storage.exists(location)
    stack.fake.external_delete(file_id)
    assert not stack.storage.exists(location)
    assert not stack.storage.exists("gdrive:neverexisted0001")
    assert not stack.storage.exists("not-a-location")
    live = stack.storage.save(KIND, sha(pdf_bytes("e2")), "b.pdf", pdf_bytes("e2"))
    stack.fake.fail_next(50, 503)
    with pytest.raises(DriveTransientError):  # an outage must NEVER read as "missing"
        stack.storage.exists(live)


# ----------------------------------------------------------------------------------------------------------------- archive / restore / safe delete


def test_archive_and_restore_are_reversible_idempotent_moves(tmp_path):
    stack = make_stack(tmp_path)
    content = pdf_bytes("move")
    location = stack.storage.save(KIND, sha(content), "a.pdf", content)
    file_id = parse_location(location)
    active, archive = stack.folders.active(KIND), stack.folders.archive(KIND)
    stack.storage.archive(location)
    assert stack.fake.files[file_id]["parents"] == [archive] and not stack.fake.files[file_id]["trashed"]
    stack.storage.archive(location)  # idempotent
    assert stack.fake.files[file_id]["parents"] == [archive]
    stack.storage.restore(location)
    assert stack.fake.files[file_id]["parents"] == [active]
    stack.storage.restore(location)  # idempotent
    assert stack.fake.files[file_id]["parents"] == [active]
    assert stack.storage.retrieve(location) == content  # bytes unchanged by the round trip


def test_delete_archives_and_nothing_can_destroy_a_file(tmp_path):
    stack = make_stack(tmp_path)
    content = pdf_bytes("safe")
    location = stack.storage.save(KIND, sha(content), "a.pdf", content)
    stack.storage.delete(location)  # the interface's remove operation
    file_id = parse_location(location)
    assert file_id in stack.fake.files and not stack.fake.files[file_id]["trashed"]
    assert stack.fake.files[file_id]["parents"] == [stack.folders.archive(KIND)]
    assert {r["method"] for r in stack.fake.requests} <= {"GET", "POST", "PATCH", "PUT"}  # no DELETE was ever sent to Drive
    for forbidden in ("permanent_delete", "purge", "destroy", "empty_trash", "delete_file"):
        assert not hasattr(stack.storage, forbidden) and not hasattr(stack.client, forbidden)


def test_missing_files_do_not_break_delete_but_do_fail_archive_loudly(tmp_path):
    stack = make_stack(tmp_path)
    content = pdf_bytes("gone")
    location = stack.storage.save(KIND, sha(content), "a.pdf", content)
    stack.fake.external_delete(parse_location(location))
    stack.storage.delete(location)  # promised never to raise for an already-gone document
    with pytest.raises(DriveNotFound):
        stack.storage.archive(location)
    stack.storage.delete("not-a-gdrive-location")  # also quietly tolerated


def test_restore_brings_back_a_file_that_was_trashed_in_drive(tmp_path):
    stack = make_stack(tmp_path)
    content = pdf_bytes("trashed")
    location = stack.storage.save(KIND, sha(content), "a.pdf", content)
    file_id = parse_location(location)
    stack.fake.external_trash(file_id)
    stack.storage.restore(location)
    assert not stack.fake.files[file_id]["trashed"] and stack.fake.files[file_id]["parents"] == [stack.folders.active(KIND)]


# ----------------------------------------------------------------------------------------------------------------- processing copy


def test_local_copy_is_isolated_verified_and_always_cleaned_up(tmp_path):
    stack = make_stack(tmp_path)
    content = pdf_bytes("copy")
    location = stack.storage.save(KIND, sha(content), "a.pdf", content)
    with stack.storage.local_copy(location) as path:
        assert path.read_bytes() == content and path.parent.name.startswith("spinco_drive_")
        directory = path.parent
    assert not directory.exists()
    with pytest.raises(RuntimeError):
        with stack.storage.local_copy(location) as path:
            directory = path.parent
            raise RuntimeError("processing failed")
    assert not directory.exists()  # cleaned up after a failure too
    stack.fake.files[parse_location(location)]["md5Checksum"] = "2" * 32
    with pytest.raises(DriveIntegrityError):
        with stack.storage.local_copy(location):
            pass
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("spinco_drive_")]  # nothing left behind


def test_a_trashed_document_cannot_be_retrieved_or_copied(tmp_path):
    stack = make_stack(tmp_path)
    content = pdf_bytes("t")
    location = stack.storage.save(KIND, sha(content), "a.pdf", content)
    stack.fake.external_trash(parse_location(location))
    with pytest.raises(DriveNotFound):
        stack.storage.retrieve(location)
    with pytest.raises(DriveNotFound):
        with stack.storage.local_copy(location):
            pass


def test_unknown_kinds_are_rejected_before_anything_is_sent(tmp_path):
    stack = make_stack(tmp_path)
    before = len(stack.fake.requests)
    with pytest.raises(ValueError):
        stack.storage.save("not_a_kind", "0" * 64, "a.pdf", b"x")
    assert len(stack.fake.requests) == before


def test_conflict_error_type_is_what_a_duplicate_id_raises_at_the_client(tmp_path):
    stack = make_stack(tmp_path)
    file_id = stack.client.generate_ids(1)[0]
    stack.client.upload_file(file_id=file_id, name="a.pdf", parent_id=stack.config.root_folder_id, content=b"x")
    with pytest.raises(DriveConflict):
        stack.client.upload_file(file_id=file_id, name="a.pdf", parent_id=stack.config.root_folder_id, content=b"x")


def test_httpx_is_the_only_network_library_in_use():
    import app.services.drive.client as client_module

    assert client_module.httpx is httpx
