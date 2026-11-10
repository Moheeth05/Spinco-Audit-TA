"""Phase 3A.2-E.1 - the open_stream policy.

A document is served only when its content can be tied to the exact file that was verified AND to the RECORDED document checksum:

  1. Drive's answer describes the file that was asked for (id compared), which is live, in the expected Shared Drive and an ordinary document;
  2. Drive's own checksum / size equal the recorded ones - BEFORE any byte is downloaded;
  3. the downloaded bytes, hashed here, equal the recorded checksum and size;
  4. after the download the same file is read again: still live, still in the expected drive, unchanged.

Only then does the first byte leave. Every failure is a typed error raised on entry; no partial or unverified content is exposed; the temporary copy is
always removed. Ranges are cut from the VERIFIED copy and are never forwarded to Drive. Nothing here claims anything about the headers real Google sends
for a download: the identity of the content is established by hashing the bytes.

FAKE-DRIVE tests of OUR logic (httpx.MockTransport): no call to Google, no credential."""

import hashlib
from contextlib import contextmanager

import httpx
import pytest

from app.services.drive.client import DriveDownload, DriveIntegrityError, DriveTransientError
from app.services.drive.errors import DriveForeignObject, DriveIdentityMismatch, DriveNotADocument
from app.services.drive.storage import VerifiedDownload, parse_location

from drive_testkit import pdf_bytes
from test_drive_guard import KIND, OTHER, SHARED, guarded, sha


def saved(stack, marker="doc", content=None):
    content = content if content is not None else b"%PDF-1.4 " + pdf_bytes(marker) + bytes(range(256)) * 8
    location = stack.storage.save(KIND, sha(content), f"{marker}.pdf", content)
    return location, parse_location(location), content


def md5(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


def media(stack, start=0):
    return [r for r in stack.fake.requests[start:] if r["query"].get("alt") == "media"]


def metadata_reads(stack, file_id, start=0):
    return [r for r in stack.fake.requests[start:] if r["method"] == "GET" and r["path"].endswith(f"/files/{file_id}") and r["query"].get("alt") != "media"]


def temp_dirs(stack):
    return list(stack.storage._temp_root.glob("spinco_drive_*"))


def serve(stack, location, content, **kw):
    kw.setdefault("expected_md5", md5(content))
    kw.setdefault("expected_size", len(content))
    with stack.storage.open_stream(location, **kw) as download:
        return download.status, dict(download.headers), b"".join(download.iter_bytes())


def hook_on(stack, predicate, action):
    """Runs `action` once, the first time a request matching `predicate` arrives (inside the fake, before it answers)."""
    done = []

    def match(request):
        if not done and predicate(request):
            done.append(1)
            action()
        return False

    stack.fake.faults.append({"times": 10**6, "kind": "none", "match": match})


def is_media(file_id):
    return lambda r: r.url.path.endswith(f"/files/{file_id}") and dict(r.url.params).get("alt") == "media"


# ====================================================================================================================== the verified path


def test_a_verified_document_is_served_whole_and_nothing_is_left_behind(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    start = len(stack.fake.requests)
    status, headers, body = serve(stack, location, content)
    assert status == 200 and body == content and headers["Content-Length"] == str(len(content))
    assert len(media(stack, start)) == 1 and len(metadata_reads(stack, file_id, start)) >= 2  # checked before AND after the download
    assert temp_dirs(stack) == []


@pytest.mark.parametrize(
    ("header", "status", "slice_", "content_range"),
    [
        ("bytes=0-9", 206, slice(0, 10), "bytes 0-9/{n}"),
        ("bytes=10-", 206, slice(10, None), "bytes 10-{last}/{n}"),
        ("bytes=-16", 206, slice(-16, None), "bytes {first_of_suffix}-{last}/{n}"),
        ("bytes=5-1000000", 206, slice(5, None), "bytes 5-{last}/{n}"),
        ("bytes=0-0", 206, slice(0, 1), "bytes 0-0/{n}"),
    ],
)
def test_ranges_are_cut_from_the_verified_copy_and_never_forwarded_to_drive(tmp_path, header, status, slice_, content_range):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    start = len(stack.fake.requests)
    code, headers, body = serve(stack, location, content, range_header=header)
    n = len(content)
    expected = content[slice_]
    assert code == status and body == expected and headers["Content-Length"] == str(len(expected))
    assert headers["Content-Range"] == content_range.format(n=n, last=n - 1, first_of_suffix=n - 16)
    assert all(r["range"] is None for r in media(stack, start))  # the whole file is always what is requested, so that it can be verified


@pytest.mark.parametrize("header", ["bytes=999999-", "bytes=-0"])
def test_an_unsatisfiable_range_is_a_416_with_no_body(tmp_path, header):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    code, headers, body = serve(stack, location, content, range_header=header)
    assert code == 416 and body == b"" and headers["Content-Range"] == f"bytes */{len(content)}"


@pytest.mark.parametrize("header", ["bytes=0-1,5-6", "bytes=abc", "items=0-5", "bytes=9-3", ""])
def test_a_malformed_or_multi_range_header_is_ignored_and_the_whole_verified_document_is_served(tmp_path, header):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    code, headers, body = serve(stack, location, content, range_header=header)
    assert code == 200 and body == content


# ====================================================================================================================== identity, move, replacement


def test_an_answer_about_a_different_file_is_refused_before_anything_is_downloaded(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    other = {"id": "SOMEOTHERFILEID01", "name": "x.pdf", "mimeType": "application/pdf", "driveId": SHARED, "trashed": False, "md5Checksum": md5(content), "size": str(len(content))}
    stack.fake.fail_next(50, 200, "ok", body=other, match=lambda r: r.method == "GET" and r.url.path.endswith(f"/files/{file_id}") and "alt" not in dict(r.url.params))
    start = len(stack.fake.requests)
    with pytest.raises(DriveIdentityMismatch):
        serve(stack, location, content)
    assert media(stack, start) == [] and temp_dirs(stack) == []


def test_a_file_moved_out_before_the_check_is_refused_without_a_download(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    stack.fake.external_move_to_drive(file_id, OTHER)
    start = len(stack.fake.requests)
    with pytest.raises(DriveForeignObject):
        serve(stack, location, content)
    assert media(stack, start) == []


def test_a_file_moved_out_DURING_the_download_is_refused_and_nothing_is_served(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    hook_on(stack, is_media(file_id), lambda: stack.fake.external_move_to_drive(file_id, OTHER))
    served = []
    with pytest.raises(DriveForeignObject):
        with stack.storage.open_stream(location, expected_md5=md5(content), expected_size=len(content)) as download:  # the error is raised on ENTRY
            served.append(download)
    assert served == [] and temp_dirs(stack) == []


def test_a_file_replaced_before_the_check_fails_the_recorded_checksum_without_a_download(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    stack.fake.external_modify(file_id, b"%PDF-1.4 SOMETHING ELSE ENTIRELY")
    start = len(stack.fake.requests)
    with pytest.raises(DriveIntegrityError) as caught:
        serve(stack, location, content)
    assert caught.value.reason == "checksum_mismatch" and media(stack, start) == []


def test_a_file_replaced_DURING_the_download_serves_nothing(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    hook_on(stack, is_media(file_id), lambda: stack.fake.external_modify(file_id, b"%PDF-1.4 REPLACED WHILE DOWNLOADING"))
    with pytest.raises(DriveIntegrityError) as caught:
        serve(stack, location, content)
    assert caught.value.reason == "checksum_mismatch" and temp_dirs(stack) == []  # the bytes that arrived are not the recorded ones


def test_a_file_replaced_right_after_the_download_is_caught_by_the_second_look(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    seen = []

    def second_metadata_read(request):
        if request.method == "GET" and request.url.path.endswith(f"/files/{file_id}") and dict(request.url.params).get("alt") != "media" and "fields" in dict(request.url.params):
            seen.append(1)
            return len(seen) == 2
        return False

    done = []

    def match(request):
        if second_metadata_read(request) and not done:
            done.append(1)
            stack.fake.external_modify(file_id, b"%PDF-1.4 CHANGED AFTER THE BYTES CAME")
        return False

    stack.fake.faults.append({"times": 10**6, "kind": "none", "match": match})
    with pytest.raises(DriveIntegrityError) as caught:
        serve(stack, location, content)
    assert caught.value.reason == "changed_during_download" and temp_dirs(stack) == []


# ====================================================================================================================== the recorded checksum


def test_a_checksum_that_does_not_match_the_record_is_refused_before_downloading(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    start = len(stack.fake.requests)
    with pytest.raises(DriveIntegrityError) as caught:
        serve(stack, location, content, expected_md5="0" * 32)
    assert caught.value.reason == "checksum_mismatch" and media(stack, start) == []


def test_a_size_that_does_not_match_the_record_is_refused(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    with pytest.raises(DriveIntegrityError) as caught:
        serve(stack, location, content, expected_size=len(content) + 1)
    assert caught.value.reason == "size_mismatch"


def test_the_checks_after_the_download_stand_on_their_own_even_if_the_checks_before_it_were_skipped(tmp_path):
    """Defence in depth: the recorded checksum and size are compared TWICE (Drive's metadata before the download, the hashed bytes after it). Switch the
    first comparison off and the second still refuses."""
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    stack.storage._tie_to_record = lambda meta, expected_md5, expected_size: None
    with pytest.raises(DriveIntegrityError) as caught:
        serve(stack, location, content, expected_md5="2" * 32)
    assert caught.value.reason == "checksum_mismatch"
    with pytest.raises(DriveIntegrityError) as caught:
        serve(stack, location, content, expected_size=len(content) + 7)
    assert caught.value.reason == "size_mismatch"
    assert temp_dirs(stack) == []


def test_the_recorded_checksum_comparison_ignores_letter_case(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    assert serve(stack, location, content, expected_md5=md5(content).upper())[2] == content


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_with_no_recorded_checksum_nothing_is_served(tmp_path, missing):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    start = len(stack.fake.requests)
    with pytest.raises(DriveIntegrityError) as caught:
        serve(stack, location, content, expected_md5=missing)
    assert caught.value.reason == "unverifiable" and media(stack, start) == []


def test_the_checksum_is_a_required_argument(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    with pytest.raises(TypeError):
        stack.storage.open_stream(location)  # there is no unverified way to open a stream any more


def test_a_file_for_which_drive_has_no_checksum_cannot_be_tied_to_the_record_and_is_refused(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    stack.fake.files[file_id].pop("md5Checksum")
    start = len(stack.fake.requests)
    with pytest.raises(DriveIntegrityError) as caught:
        serve(stack, location, content)
    assert caught.value.reason == "unverifiable" and media(stack, start) == []


@pytest.mark.parametrize("mime", ["application/vnd.google-apps.folder", "application/vnd.google-apps.shortcut", "application/vnd.google-apps.document", ""])
def test_a_folder_a_shortcut_or_a_google_docs_item_is_never_served(tmp_path, mime):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    stack.fake.files[file_id]["mimeType"] = mime
    start = len(stack.fake.requests)
    with pytest.raises(DriveNotADocument):
        serve(stack, location, content)
    assert media(stack, start) == []


# ====================================================================================================================== download failures


def test_a_failing_download_is_a_typed_error_and_nothing_is_served(tmp_path):
    stack, _ = guarded(tmp_path, max_attempts=2)
    location, file_id, content = saved(stack)
    stack.fake.fail_next(50, 503, "backendError", match=is_media(file_id))
    with pytest.raises(DriveTransientError):
        serve(stack, location, content)
    assert temp_dirs(stack) == []


@contextmanager
def replacing_download(stack, make_download):
    original = stack.client.open_download
    stack.client.open_download = lambda file_id, **kw: make_download()
    try:
        yield
    finally:
        stack.client.open_download = original


class Interrupted(httpx.SyncByteStream):
    def __init__(self, chunks, then):
        self._chunks, self._then = chunks, then

    def __iter__(self):
        yield from self._chunks
        raise self._then


def download_of(stream, status=200):
    @contextmanager
    def manager():
        yield DriveDownload(httpx.Response(status, stream=stream))

    return manager()


def test_a_download_interrupted_half_way_is_a_typed_retryable_error_and_leaves_nothing(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    half = Interrupted([content[: len(content) // 2]], httpx.ReadError("connection reset SECRET-DETAIL"))
    with replacing_download(stack, lambda: download_of(half)):
        with pytest.raises(DriveTransientError) as caught:
            serve(stack, location, content)
    assert caught.value.retryable and "SECRET-DETAIL" not in str(caught.value) and caught.value.__cause__ is None
    assert temp_dirs(stack) == []


def test_a_truncated_download_that_ends_cleanly_is_refused(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)

    class Ends(httpx.SyncByteStream):  # the connection simply ends early: no error, just fewer bytes than the document has
        def __iter__(self):
            yield content[:-5]

    with replacing_download(stack, lambda: download_of(Ends())):
        with pytest.raises(DriveIntegrityError) as caught:
            serve(stack, location, content)
    assert caught.value.reason in ("checksum_mismatch", "size_mismatch") and temp_dirs(stack) == []


def test_a_partial_answer_where_the_whole_document_was_asked_for_is_refused(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)

    class Whole(httpx.SyncByteStream):
        def __iter__(self):
            yield content

    with replacing_download(stack, lambda: download_of(Whole(), status=206)):
        with pytest.raises(DriveIntegrityError) as caught:
            serve(stack, location, content)
    assert caught.value.reason == "unverifiable" and temp_dirs(stack) == []


def test_a_consumer_that_stops_early_still_leaves_nothing_behind(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    with stack.storage.open_stream(location, expected_md5=md5(content), expected_size=len(content)) as download:
        next(iter(download.iter_bytes(chunk_size=16)))
        assert temp_dirs(stack) != []  # the verified copy exists while it is being served ...
    assert temp_dirs(stack) == []  # ... and is gone when the stream is closed


# ====================================================================================================================== retrieve() with a recorded checksum


def test_retrieve_with_a_recorded_checksum_follows_the_same_chain(tmp_path):
    stack, _ = guarded(tmp_path)
    location, file_id, content = saved(stack)
    assert stack.storage.retrieve(location, expected_md5=md5(content), expected_size=len(content)) == content
    with pytest.raises(DriveIntegrityError):
        stack.storage.retrieve(location, expected_md5="1" * 32)
    stack.fake.external_move_to_drive(file_id, OTHER)
    with pytest.raises(DriveForeignObject):
        stack.storage.retrieve(location, expected_md5=md5(content))
    assert temp_dirs(stack) == []


def test_the_verified_download_object_reads_only_its_own_copy(tmp_path):
    path = tmp_path / "copy.bin"
    path.write_bytes(b"0123456789")
    download = VerifiedDownload(path, 10, "bytes=2-4")
    assert (download.status, b"".join(download.iter_bytes())) == (206, b"234")
    assert VerifiedDownload(path, 10, None).headers == {"Content-Length": "10"}
    assert VerifiedDownload(tmp_path / "empty", 0, "bytes=0-").status == 416  # nothing to serve from an empty file
    download.close()
