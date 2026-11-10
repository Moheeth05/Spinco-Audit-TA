"""Phase 3A.1.2 - final security corrections V-1 .. V-8 (from the final Phase 3A.1 verification).

  V-1/V-6  a verification read never sleeps longer than ~2 s whatever Retry-After says, the whole verification has a deadline, an outage seen by
           the check of ONE OBJECT opens the breaker like a root-level one, and one Google outage cannot occupy every request thread
  V-2      archive never changes the trashed flag: an externally trashed file is refused; only an explicit restore un-trashes
  V-3      a pending upload is retried a bounded number of times with a backoff, then FAILED (bytes and history kept), without repeated events
  V-4      change-feed requests are scoped to the expected Shared Drive; an unusable token is replaced without skipping any change
  V-5      the folder structure can only be bound to the root the GUARD verifies (mutating the configuration changes nothing)
  V-7      the destination-guard opt-out exists only when the environment is explicitly dev or staging
  V-8      a stored document that left the expected Shared Drive is detected, marked, never served and never touched

All of it runs against the FAKE Drive: it proves OUR code behaves as designed, not how Google behaves."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import storage_record as sr
from app.models.storage_record import DriveSyncState, StorageEvent
from app.services.drive.client import DriveChangeTokenExpired, DriveClient, UnguardedClientRefused
from app.services.drive.config import DriveConfigError, load_runtime_config
from app.services.drive.errors import DriveForeignObject, DriveObjectTrashed, DriveRootMismatch
from app.services.drive.guard import DriveTargetError
from app.services.drive.lifecycle import StorageStateError, StorageUploadError
from app.services.drive.storage import DriveFolders, parse_location

from drive_testkit import make_stack, pdf_bytes
from test_drive_guard import KIND, OTHER, SHARED, Clock, build_hierarchy, guarded, runtime_env, sha, snapshot, writes


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def events(db, record, event_type):
    query = select(StorageEvent).where(StorageEvent.event_type == event_type)
    query = query.where(StorageEvent.record_id == record.id) if record is not None else query.where(StorageEvent.record_id.is_(None))
    return db.execute(query).scalars().all()


def reads_of(stack, path):
    return [r for r in stack.fake.requests if r["method"] == "GET" and r["path"] == path]


def timed_guarded(tmp_path, **options):
    """A guarded client on SIMULATED time: its sleeps advance the clock (no real waiting) and the guard reads the same clock."""
    clock, pauses = Clock(), []
    stack = make_stack(tmp_path, drive_id=SHARED)
    build_hierarchy(stack, 1)

    def sleep(seconds):
        pauses.append(seconds)
        clock.advance(seconds)

    config = replace(stack.config, expected_drive_id=SHARED, **options)
    client = DriveClient(config, stack.tokens, stack.http, sleep=sleep, monotonic=clock, rng=lambda: 0.5, guard_clock=clock)
    return stack, client, clock, pauses


# ====================================================================================================================== V-1 / V-6: bounded verification


def test_a_huge_retry_after_never_stretches_a_verification_read_but_ordinary_reads_still_honour_it(tmp_path):
    stack, client, clock, pauses = timed_guarded(tmp_path)
    root = stack.config.root_folder_id
    stack.fake.fail_next(1000, 429, "rateLimitExceeded", retry_after="120", match=lambda r: r.url.path == f"/drive/v3/files/{root}")
    start = clock.now
    with pytest.raises(DriveTargetError) as caught:
        client.write_guard.assert_ready()
    assert caught.value.reason == "unverifiable" and max(pauses) <= 2.0 and clock.now - start <= 2.5
    stack.fake.faults.clear()
    pauses.clear()
    stack.fake.fail_next(1, 429, "rateLimitExceeded", retry_after="30", match=lambda r: r.url.path.endswith("/files/generateIds"))
    client.generate_ids(1)
    assert max(pauses) == 30.0  # an ordinary (non-verification) read keeps honouring Retry-After


def test_the_whole_verification_has_a_deadline(tmp_path):
    stack, client, clock, pauses = timed_guarded(tmp_path, guard_verify_attempts=6, guard_verify_deadline_seconds=3.0)
    root = stack.config.root_folder_id
    stack.fake.fail_next(1000, 503, "backendError", retry_after="1.5", match=lambda r: r.url.path == f"/drive/v3/files/{root}")
    start = clock.now
    with pytest.raises(DriveTargetError) as caught:
        client.write_guard.assert_ready()
    assert caught.value.reason == "unverifiable"
    # six attempts were allowed; the deadline cut them off, and the second pause (which could not fit in what was left) was not even started
    assert clock.now - start <= 1.6 and len(reads_of(stack, f"/drive/v3/files/{root}")) == 2


def test_a_walk_up_the_hierarchy_that_runs_out_of_time_is_refused_and_opens_the_breaker(tmp_path):
    stack, client, clock, pauses = timed_guarded(tmp_path, guard_verify_deadline_seconds=5.0)
    # every read "takes" 4 simulated seconds; the walk is root -> ancestor -> drive root (three reads), but there are only five seconds for all of them
    stack.fake.faults.append({"times": 10**6, "kind": "none", "match": lambda r: (clock.advance(4.0) if r.url.path.startswith("/drive/v3/files/") else None, False)[1]})
    start_requests = len(stack.fake.requests)
    with pytest.raises(DriveTargetError) as caught:
        client.write_guard.assert_ready()
    reads = [r for r in stack.fake.requests[start_requests:] if r["path"].startswith("/drive/v3/files/")]
    assert caught.value.reason == "unverifiable" and len(reads) == 2 and client.write_guard.status()["circuit_open"]


def test_an_outage_seen_by_one_object_check_opens_the_breaker_for_every_writer(tmp_path):
    stack, _ = guarded(tmp_path)
    client, guard = stack.client, stack.client.write_guard
    content = pdf_bytes("object")
    created = stack.storage.upload(KIND, sha(content), "o.pdf", content)  # the destination is verified and cached; the folders exist
    assert guard.verified
    stack.fake.drop_connection_next(10**6, match=lambda r: r.url.path == f"/drive/v3/files/{created.obj.id}")
    with pytest.raises(DriveTargetError) as first:
        client.update_file(created.obj.id, name="renamed.pdf")
    assert first.value.reason == "unverifiable" and guard.status()["circuit_open"] and not guard.verified  # the cached verification no longer carries anyone
    start = len(stack.fake.requests)
    for _ in range(5):  # every other writer is refused AT ONCE: no network at all, not five more rounds of retries against the failing read
        with pytest.raises(DriveTargetError) as refused:
            client.create_folder("Another", stack.config.root_folder_id)
        assert refused.value.reason == "unverifiable"
    assert len(stack.fake.requests) == start


def test_the_breaker_closes_again_after_its_window(tmp_path):
    clock = Clock()
    stack, guard = guarded(tmp_path, clock=clock, failure_window=15.0)
    content = pdf_bytes("recover")
    created = stack.storage.upload(KIND, sha(content), "r.pdf", content)
    stack.fake.drop_connection_next(10**6, match=lambda r: r.url.path == f"/drive/v3/files/{created.obj.id}")
    with pytest.raises(DriveTargetError):
        stack.client.update_file(created.obj.id, name="x.pdf")
    stack.fake.faults.clear()
    clock.advance(16.0)
    assert stack.client.update_file(created.obj.id, name="x.pdf").name == "x.pdf"  # the next write verifies again and goes through


def test_one_outage_cannot_occupy_every_request_thread(tmp_path):
    stack, _ = guarded(tmp_path)
    stack.client.write_guard._waiter_seconds = 0.2  # keep the test quick; the default is 1.5 s
    guard, root = stack.client.write_guard, stack.config.root_folder_id
    entered, release = threading.Event(), threading.Event()

    def stall(request):  # Google "hangs" on the first destination read; the thread doing it is stuck until released
        if request.url.path == f"/drive/v3/files/{root}":
            entered.set()
            release.wait(timeout=10)
        return False

    stack.fake.faults.append({"times": 10**6, "kind": "none", "match": stall})
    refused, durations = [], []

    def writer():
        began = time.monotonic()
        try:
            guard.assert_ready()
        except DriveTargetError as exc:
            refused.append(exc.reason)
        durations.append(time.monotonic() - began)

    holder = threading.Thread(target=lambda: guard.try_verify())
    holder.start()
    assert entered.wait(timeout=5)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=40) as pool:
        for future in [pool.submit(writer) for _ in range(40)]:
            future.result(timeout=10)
    elapsed = time.monotonic() - started
    release.set()
    holder.join(timeout=10)
    assert refused == ["unverifiable"] * 40  # every queued writer was refused, none was left waiting on the stuck verification
    assert max(durations) < 2.0 and elapsed < 5.0  # 40 threads, one stalled verification: nobody waited anywhere near the whole stall


def test_concurrent_writers_still_share_one_verification_when_google_is_healthy(tmp_path):
    stack, _ = guarded(tmp_path)
    guard, root = stack.client.write_guard, stack.config.root_folder_id
    guard._waiter_seconds = 5.0
    gate = threading.Event()
    stack.fake.faults.append({"times": 10**6, "kind": "none", "match": lambda r: (gate.wait(timeout=10) if r.url.path == f"/drive/v3/files/{root}" else None, False)[1]})
    outcomes = []
    threads = [threading.Thread(target=lambda: outcomes.append(guard.try_verify())) for _ in range(12)]
    for thread in threads:
        thread.start()
    time.sleep(0.3)
    gate.set()
    for thread in threads:
        thread.join(timeout=10)
    assert outcomes == [True] * 12 and guard.verifications == 1  # single-flight survived the bounded wait


def test_an_invalidate_during_a_verification_still_prevents_caching_it(tmp_path):
    stack, _ = guarded(tmp_path)
    guard, root = stack.client.write_guard, stack.config.root_folder_id
    stack.fake.faults.append({"times": 1, "kind": "none", "match": lambda r: (guard.invalidate() if r.url.path == f"/drive/v3/files/{root}" else None, False)[1]})
    guard.assert_ready()  # this call may proceed (it verified), but the invalidation that raced with it means the result is not trusted afterwards
    assert not guard.verified


# ====================================================================================================================== V-2: archive never un-trashes


def stored_pair(stack, db, marker):
    result = stack.manager.store(db, kind=KIND, content=pdf_bytes(marker), original_filename=f"{marker}.pdf")
    return result.record, parse_location(result.record.location)


def test_archive_refuses_an_externally_trashed_file_and_does_not_untrash_it(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "trashed")
    stack.fake.external_trash(file_id)
    before, start = snapshot(stack), len(stack.fake.requests)
    with pytest.raises(StorageStateError, match="trash"):
        stack.manager.archive(db, record.id, user_id=None)
    with pytest.raises(DriveObjectTrashed):
        stack.storage.archive(record.location, KIND)
    db.refresh(record)
    assert record.state == sr.STORED  # the database is unchanged
    assert stack.fake.files[file_id]["trashed"] is True and snapshot(stack) == before and writes(stack, start) == []  # and nothing was sent to Drive


def test_a_legitimate_archive_and_restore_still_work_and_archive_never_mentions_trashed(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "legit")
    stack.manager.archive(db, record.id, user_id=None)
    patches = [r for r in stack.fake.requests if r["method"] == "PATCH"]
    assert patches and all("trashed" not in r["body"] for r in patches)
    assert stack.fake.files[file_id]["parents"] == [stack.folders.archive(KIND)] and not stack.fake.files[file_id]["trashed"]
    stack.manager.restore(db, record.id, user_id=None)
    assert stack.fake.files[file_id]["parents"] == [stack.folders.active(KIND)]
    assert not stack.fake.files[file_id]["trashed"]


def test_only_an_explicit_restore_may_untrash(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "restore-trash")
    stack.manager.archive(db, record.id, user_id=None)
    stack.fake.external_trash(file_id)
    stack.manager.reconcile(db)
    db.refresh(record)
    assert record.state == sr.MISSING_EXTERNAL and stack.fake.files[file_id]["trashed"] is True  # noticed, not "fixed"
    stack.manager.restore(db, record.id, user_id=None)  # an authorised person asked for it
    db.refresh(record)
    assert record.state == sr.STORED and stack.fake.files[file_id]["trashed"] is False
    assert any(r["body"].get("trashed") is False for r in stack.fake.requests if r["method"] == "PATCH")


def test_delete_of_a_trashed_file_is_quiet_and_changes_nothing(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "delete-trashed")
    stack.fake.external_trash(file_id)
    before = snapshot(stack)
    stack.storage.delete(record.location)  # the interface promises not to raise for something that is already gone
    assert snapshot(stack) == before


# ====================================================================================================================== V-3: bounded upload retries


class Offset:
    """Simulated time for the manager: real time plus a controllable offset (the records' own timestamps are real)."""

    def __init__(self):
        self.seconds = 0.0

    def __call__(self):
        return datetime.now(timezone.utc) + timedelta(seconds=self.seconds)


def upload_requests(stack):
    return [r for r in stack.fake.requests if r["path"].startswith("/upload")]


def failing_upload_stack(tmp_path, *, attempts=3, backoff=60.0):
    offset = Offset()
    stack = make_stack(tmp_path, drive_id=SHARED, expected_drive_id=SHARED, max_attempts=2, max_upload_attempts=attempts, retry_backoff=backoff, now=offset)
    build_hierarchy(stack, 1)
    stack.manager.initialise_structure()
    stack.fake.fail_next(10**6, 503, "backendError", match=lambda r: r.url.path.startswith("/upload"))
    return stack, offset


def first_failed_store(stack, db, marker="retry"):
    with pytest.raises(StorageUploadError) as caught:
        stack.manager.store(db, kind=KIND, content=pdf_bytes(marker), original_filename=f"{marker}.pdf")
    assert caught.value.retryable
    return caught.value.record


def test_a_pending_upload_is_given_up_after_the_configured_number_of_attempts(tmp_path, db):
    stack, offset = failing_upload_stack(tmp_path, attempts=3, backoff=60.0)
    record = first_failed_store(stack, db)
    assert record.attempts == 1 and record.state == sr.UPLOADING
    for _ in range(10):  # plenty of reconciles, each far enough apart for the backoff to have passed
        offset.seconds += 10_000
        stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.FAILED and record.attempts == 3
    assert "abandoned after 3 failed attempts" in record.last_error and "limit 3" in record.last_error
    assert 3 <= len(upload_requests(stack)) <= 3 * 2  # three attempts (each at most two HTTP tries); none beyond the limit reached the network
    sent = len(upload_requests(stack))
    offset.seconds += 10_000
    stack.manager.reconcile(db, stale_after_seconds=0)
    assert len(upload_requests(stack)) == sent  # a FAILED upload is never retried by reconcile


def test_the_failure_is_reported_once_not_on_every_reconcile(tmp_path, db):
    stack, offset = failing_upload_stack(tmp_path, attempts=2, backoff=60.0)
    record = first_failed_store(stack, db)
    for _ in range(8):
        offset.seconds += 10_000
        stack.manager.reconcile(db, stale_after_seconds=0)
    assert len(events(db, record, "UPLOAD_RETRY_LIMIT_REACHED")) == 1
    assert len(events(db, record, "UPLOAD_ATTEMPT_FAILED")) == 2  # one per real attempt, none for the reconciles that did nothing


def test_the_spooled_bytes_and_the_history_are_kept_when_the_limit_is_reached(tmp_path, db):
    stack, offset = failing_upload_stack(tmp_path, attempts=2)
    record = first_failed_store(stack, db, "keep")
    for _ in range(4):
        offset.seconds += 10_000
        stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.FAILED and stack.manager._spooled(record) == pdf_bytes("keep")
    kinds = {e.event_type for e in db.execute(select(StorageEvent).where(StorageEvent.record_id == record.id)).scalars()}
    assert {"UPLOAD_STARTED", "UPLOAD_ATTEMPT_FAILED", "UPLOAD_RETRY_LIMIT_REACHED"} <= kinds


def test_a_failed_attempt_waits_out_a_doubling_backoff_before_the_next(tmp_path, db):
    # 3A.2-B: the wait is the record's own next_attempt_at (60 s, 120 s, ... each stretched or shrunk by up to 10 %), measured on the manager's clock.
    stack, offset = failing_upload_stack(tmp_path, attempts=10, backoff=60.0)
    record = first_failed_store(stack, db)  # attempt 1 failed at t = 0: wait 60 s (54 .. 66 with the jitter)
    assert record.next_attempt_at is not None
    sent = len(upload_requests(stack))
    offset.seconds = 30
    stack.manager.reconcile(db, stale_after_seconds=0)
    assert len(upload_requests(stack)) == sent  # too early
    offset.seconds = 70
    stack.manager.reconcile(db, stale_after_seconds=0)
    assert len(upload_requests(stack)) > sent  # attempt 2
    db.refresh(record)
    assert record.attempts == 2
    sent = len(upload_requests(stack))
    offset.seconds = 70 + 100  # attempt 2 failed at t = 70: the next wait is 120 s (108 .. 132), so 100 s later is too early
    stack.manager.reconcile(db, stale_after_seconds=0)
    assert len(upload_requests(stack)) == sent
    offset.seconds = 70 + 135
    stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.attempts == 3


def test_submitting_the_same_file_again_recovers_a_failed_upload_with_a_fresh_budget(tmp_path, db):
    stack, offset = failing_upload_stack(tmp_path, attempts=2, backoff=0.0)
    record = first_failed_store(stack, db, "again")
    for _ in range(4):
        offset.seconds += 10_000
        stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.FAILED
    stack.fake.faults.clear()  # Google is fine again
    result = stack.manager.store(db, kind=KIND, content=pdf_bytes("again"), original_filename="again.pdf")
    db.refresh(record)
    assert result.duplicate and record.state == sr.STORED and stack.fake.file_count() == 1  # one file, no duplicate
    assert len(events(db, record, "UPLOAD_RETRY_REQUESTED")) == 1 and len(events(db, record, "UPLOAD_RETRY_LIMIT_REACHED")) == 1  # the history stays
    assert stack.manager._spooled(record) is None  # the spool is released once Drive confirmed the bytes


def test_resubmitting_a_failed_upload_starts_a_fresh_budget_even_if_google_is_still_failing(tmp_path, db):
    stack, offset = failing_upload_stack(tmp_path, attempts=2, backoff=0.0)
    record = first_failed_store(stack, db, "fresh")
    for _ in range(4):
        offset.seconds += 10_000
        stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.FAILED and record.attempts == 2
    with pytest.raises(StorageUploadError) as again:  # Google is still failing
        stack.manager.store(db, kind=KIND, content=pdf_bytes("fresh"), original_filename="fresh.pdf")
    db.refresh(record)
    assert again.value.retryable and record.state == sr.UPLOADING and record.attempts == 1  # a new budget (not "2 + 1 = 3 > limit")


def test_an_administrator_can_retry_a_failed_upload_explicitly(tmp_path, db):
    stack, offset = failing_upload_stack(tmp_path, attempts=2, backoff=0.0)
    record = first_failed_store(stack, db, "admin")
    for _ in range(4):
        offset.seconds += 10_000
        stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.FAILED
    stack.fake.faults.clear()
    retried = stack.manager.retry_upload(db, record.id, user_id=None)
    assert retried.state == sr.STORED and retried.attempts == 0 and stack.fake.file_count() == 1
    with pytest.raises(StorageStateError):  # nothing left to retry
        stack.manager.retry_upload(db, record.id, user_id=None)


def test_a_retry_is_refused_when_no_copy_of_the_bytes_was_kept(tmp_path, db):
    stack, offset = failing_upload_stack(tmp_path, attempts=2)
    record = first_failed_store(stack, db, "nospool")
    stack.manager._unspool(record)
    offset.seconds += 10_000
    stack.manager.reconcile(db, stale_after_seconds=0)  # the bytes are gone: reconcile can only give the record up
    db.refresh(record)
    assert record.state == sr.FAILED
    with pytest.raises(StorageStateError, match="No copy"):
        stack.manager.retry_upload(db, record.id, user_id=None)


def test_the_retry_settings_come_from_the_configuration_and_are_validated(tmp_path):
    config = load_runtime_config(runtime_env(tmp_path, SPINCO_GDRIVE_MAX_UPLOAD_ATTEMPTS="4", SPINCO_GDRIVE_UPLOAD_RETRY_BACKOFF_SECONDS="30"))
    assert config.max_upload_attempts == 4 and config.upload_retry_backoff_seconds == 30.0
    default = load_runtime_config(runtime_env(tmp_path))
    assert default.max_upload_attempts == 10 and default.upload_retry_backoff_seconds == 60.0
    for bad in ("0", "-1", "abc", "1000", "2.5"):
        with pytest.raises(DriveConfigError):
            load_runtime_config(runtime_env(tmp_path, SPINCO_GDRIVE_MAX_UPLOAD_ATTEMPTS=bad))
    with pytest.raises(DriveConfigError):
        load_runtime_config(runtime_env(tmp_path, SPINCO_GDRIVE_UPLOAD_RETRY_BACKOFF_SECONDS="0"))
    with pytest.raises(DriveConfigError):
        load_runtime_config(runtime_env(tmp_path, SPINCO_GDRIVE_GUARD_VERIFY_DEADLINE_SECONDS="1000"))


# ====================================================================================================================== V-4: scoped change feed


def last_request(stack, path):
    return reads_of(stack, path)[-1]["query"]


def test_change_requests_are_scoped_to_the_expected_shared_drive(tmp_path, db):
    stack, _ = guarded(tmp_path)
    stack.manager.sync_changes(db)  # baseline
    stack.manager.sync_changes(db)  # a real pass
    start = last_request(stack, "/drive/v3/changes/startPageToken")
    listing = last_request(stack, "/drive/v3/changes")
    assert start["driveId"] == SHARED and start["supportsAllDrives"] == "true"
    assert listing["driveId"] == SHARED and listing["supportsAllDrives"] == "true" and listing["includeItemsFromAllDrives"] == "true"
    assert "~" in db.get(DriveSyncState, stack.manager._changes_state_name()).page_token  # the token itself belongs to this scope


def test_a_harness_client_without_an_expected_drive_keeps_the_unscoped_feed(tmp_path, db):
    stack = make_stack(tmp_path)
    stack.manager.sync_changes(db)
    assert "driveId" not in last_request(stack, "/drive/v3/changes/startPageToken") and stack.manager._changes_state_name() == "changes"


def test_a_guarded_client_refuses_to_read_the_changes_of_another_drive(tmp_path):
    stack, _ = guarded(tmp_path)
    with pytest.raises(ValueError):
        stack.client.get_start_page_token(OTHER)
    with pytest.raises(ValueError):
        stack.client.list_changes("0", drive_id=OTHER)
    assert stack.client.get_start_page_token(SHARED).endswith(f"~{SHARED}")


def test_a_token_issued_for_another_scope_is_not_usable(tmp_path):
    stack, _ = guarded(tmp_path)
    with pytest.raises(DriveChangeTokenExpired):
        stack.client.list_changes("0")  # an unscoped token presented to the scoped feed: Google refuses it, we do not guess


def test_changes_that_belong_to_another_drive_are_not_delivered(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "scoped")
    stack.manager.sync_changes(db)  # baseline
    stack.fake.files[file_id]["trashed"] = True
    stack.fake._changed(file_id, drive_id=OTHER)  # a change that happened in ANOTHER drive's feed
    report = stack.manager.sync_changes(db)
    db.refresh(record)
    assert report.changes_seen == 0 and record.state == sr.STORED
    stack.fake._changed(file_id)  # the same file, in THIS drive's feed
    stack.manager.sync_changes(db)
    db.refresh(record)
    assert record.state == sr.MISSING_EXTERNAL


def test_an_expired_token_is_replaced_after_a_full_reconcile_taken_with_a_token_fetched_first(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "expired")
    stack.manager.sync_changes(db)
    old = db.get(DriveSyncState, stack.manager._changes_state_name()).page_token
    stack.fake.external_trash(file_id)
    stack.fake.min_valid_change_token = len(stack.fake.changes) + 50  # the old token can no longer be used
    start = len(stack.fake.requests)
    report = stack.manager.sync_changes(db)
    assert report.token_expired and report.reconcile is not None and report.reconcile["missing_external"] == 1
    db.refresh(record)
    assert record.state == sr.MISSING_EXTERNAL  # the full pass found what the lost feed would have said
    after = stack.fake.requests[start:]
    expired_at = next(i for i, r in enumerate(after) if r["path"] == "/drive/v3/changes")
    token_at = next(i for i, r in enumerate(after) if i > expired_at and r["path"] == "/drive/v3/changes/startPageToken")
    first_read = next(i for i, r in enumerate(after) if i > expired_at and r["path"].startswith("/drive/v3/files"))
    assert token_at < first_read  # the new token was taken BEFORE the full pass, so nothing that changes during it can fall in a gap
    stack.fake.min_valid_change_token = 0
    db.expire_all()
    assert db.get(DriveSyncState, stack.manager._changes_state_name()).page_token != old and events(db, None, "CHANGE_TOKEN_EXPIRED_RECOVERED")
    assert not stack.manager.sync_changes(db).token_expired


def test_a_reset_that_could_not_judge_everything_keeps_the_old_token_and_says_so_once(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "deferred")
    stack.manager.sync_changes(db)
    name = stack.manager._changes_state_name()
    old = db.get(DriveSyncState, name).page_token
    stack.fake.min_valid_change_token = len(stack.fake.changes) + 50
    stack.fake.fail_next(10**6, 503, "backendError", match=lambda r: r.url.path == f"/drive/v3/files/{file_id}")  # the full pass cannot judge this document
    for _ in range(3):
        report = stack.manager.sync_changes(db)
        assert report.token_expired and report.reconcile["errors"]
    db.expire_all()
    assert db.get(DriveSyncState, name).page_token == old  # not replaced: starting from "now" would silently skip what happened meanwhile
    assert len(events(db, None, "CHANGE_TOKEN_RESET_DEFERRED")) == 1 and not events(db, None, "CHANGE_TOKEN_EXPIRED_RECOVERED")
    stack.fake.faults.clear()
    stack.fake.external_rename(file_id, "renamed-meanwhile.pdf")  # something changes while the reset was deferred (so the new token differs from the old one)
    stack.manager.sync_changes(db)  # Google is back: the whole thing is done properly
    db.expire_all()
    assert db.get(DriveSyncState, name).page_token != old and events(db, None, "CHANGE_TOKEN_EXPIRED_RECOVERED")
    assert events(db, record, "NAME_DRIFT")  # and the full pass saw that change too


def test_an_unscoped_token_from_before_is_replaced_only_after_a_full_reconcile(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "legacy")
    db.add(DriveSyncState(name="changes", page_token="5"))
    db.commit()
    stack.fake.external_trash(file_id)  # a change the old, unscoped token knows nothing reliable about
    report = stack.manager.sync_changes(db)
    assert report.rescoped and not report.baseline and report.reconcile is not None
    db.refresh(record)
    assert record.state == sr.MISSING_EXTERNAL  # found by the full pass, not skipped
    assert events(db, None, "CHANGE_TOKEN_RESCOPED")
    assert db.get(DriveSyncState, "changes").page_token == "5"  # the old row is kept, never deleted
    again = stack.manager.sync_changes(db)
    assert not again.rescoped and not again.baseline


# ====================================================================================================================== V-5: guarded_root comes from the guard


def test_mutating_the_configuration_cannot_make_the_folders_accept_a_different_root(tmp_path):
    stack, _ = guarded(tmp_path)
    verified_root = stack.config.root_folder_id
    stack.client._config = replace(stack.client._config, root_folder_id="SOMEOTHERROOT0001")
    assert stack.client.guarded_root == verified_root == stack.client.write_guard.root_folder_id
    with pytest.raises(DriveRootMismatch):
        DriveFolders(stack.client, "SOMEOTHERROOT0001")
    assert DriveFolders(stack.client, verified_root).root_id == verified_root


def test_mutating_the_configuration_cannot_move_the_listing_or_the_feed_scope(tmp_path):
    stack, _ = guarded(tmp_path)
    stack.client._config = replace(stack.client._config, expected_drive_id=OTHER)
    stack.client.find_children(stack.config.root_folder_id)
    stack.client.get_start_page_token()
    assert reads_of(stack, "/drive/v3/files")[-1]["query"]["driveId"] == SHARED
    assert last_request(stack, "/drive/v3/changes/startPageToken")["driveId"] == SHARED


def test_an_unguarded_client_has_no_guarded_root(tmp_path):
    assert make_stack(tmp_path).client.guarded_root is None


# ====================================================================================================================== V-7: the opt-out needs an explicit dev or staging environment


@pytest.mark.parametrize("value", ["", "   ", "production", "prod", "live", "Production", " PRODUCTION ", "test", "stage", "development", "local", "qa", "uat", "none", "true", "1"])
def test_the_opt_out_is_refused_for_every_environment_value_except_dev_and_staging(tmp_path, monkeypatch, value):
    plain = make_stack(tmp_path)
    monkeypatch.setenv("SPINCO_ENVIRONMENT", value)
    with pytest.raises(UnguardedClientRefused):
        DriveClient(plain.config, plain.tokens, plain.http, unguarded=True)


def test_the_opt_out_is_refused_when_the_environment_is_unset(tmp_path, monkeypatch):
    plain = make_stack(tmp_path)
    monkeypatch.delenv("SPINCO_ENVIRONMENT", raising=False)
    with pytest.raises(UnguardedClientRefused, match="explicitly"):
        DriveClient(plain.config, plain.tokens, plain.http, unguarded=True)


@pytest.mark.parametrize("value", ["dev", "staging", "DEV", " Staging "])
def test_the_opt_out_is_allowed_only_for_an_explicit_dev_or_staging(tmp_path, monkeypatch, value):
    plain = make_stack(tmp_path)
    monkeypatch.setenv("SPINCO_ENVIRONMENT", value)
    assert DriveClient(plain.config, plain.tokens, plain.http, unguarded=True).write_guard is None


@pytest.mark.parametrize("configured", ["production", "prod", "live", "", "unknown"])
def test_the_opt_out_is_refused_when_the_configuration_names_another_environment(tmp_path, monkeypatch, configured):
    plain = make_stack(tmp_path)
    monkeypatch.setenv("SPINCO_ENVIRONMENT", "dev")
    with pytest.raises(UnguardedClientRefused):
        DriveClient(replace(plain.config, environment=configured), plain.tokens, plain.http, unguarded=True)


def test_a_guarded_client_does_not_depend_on_the_environment_variable(tmp_path, monkeypatch):
    stack, _ = guarded(tmp_path)
    monkeypatch.setenv("SPINCO_ENVIRONMENT", "production")
    assert DriveClient(stack.config, stack.tokens, stack.http).write_guard is not None  # the normal, guarded way works in production


# ====================================================================================================================== V-8: documents that left the expected drive


@pytest.mark.parametrize("destination", [OTHER, None], ids=["another Shared Drive", "a My Drive"])
def test_a_document_moved_out_of_the_drive_is_marked_and_never_touched(tmp_path, db, destination):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "leaver")
    stack.manager.link_document(db, record.id, "expense", 42)
    before_events = db.execute(select(StorageEvent).where(StorageEvent.record_id == record.id)).scalars().all()
    stack.fake.external_move_to_drive(file_id, destination)
    before, start = snapshot(stack), len(stack.fake.requests)
    report = stack.manager.reconcile(db)
    db.refresh(record)
    assert record.state == sr.MISSING_EXTERNAL and record.missing_detected_at is not None and report.foreign_drive == 1
    assert len(events(db, record, "EXTERNAL_MOVE_OUT_OF_DRIVE")) == 1
    assert snapshot(stack) == before and writes(stack, start) == []  # the foreign file was not deleted, moved, renamed or touched
    assert record.document_kind == "expense" and record.document_pk == 42 and record.location.startswith("gdrive:")  # the links and history stay
    assert len(db.execute(select(StorageEvent).where(StorageEvent.record_id == record.id)).scalars().all()) == len(before_events) + 1


def test_repeated_reconciliation_adds_no_duplicate_events(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "repeat")
    stack.fake.external_move_to_drive(file_id, OTHER)
    stack.manager.reconcile(db)
    count = db.execute(select(StorageEvent).where(StorageEvent.record_id == record.id)).scalars().all()
    for _ in range(4):
        again = stack.manager.reconcile(db)
        assert again.foreign_drive == 0
    after = db.execute(select(StorageEvent).where(StorageEvent.record_id == record.id)).scalars().all()
    # the only thing allowed to appear once more is the single "recovery rejected" note for a record that is already MISSING_EXTERNAL
    assert len(after) - len(count) <= 1 and len(events(db, record, "EXTERNAL_MOVE_OUT_OF_DRIVE")) == 1
    db.refresh(record)
    assert record.state == sr.MISSING_EXTERNAL


def test_an_archived_document_that_left_the_drive_is_marked_too(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "archived-leaver")
    stack.manager.archive(db, record.id, user_id=None)
    stack.fake.external_move_to_drive(file_id, OTHER)
    stack.manager.reconcile(db)
    db.refresh(record)
    assert record.state == sr.MISSING_EXTERNAL


def test_a_document_outside_the_drive_is_not_served_even_before_reconciliation_noticed(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "unserved")
    stack.fake.external_move_to_drive(file_id, OTHER)  # the record still says STORED: nothing has reconciled yet
    start = len(stack.fake.requests)
    with pytest.raises(DriveForeignObject):
        stack.storage.retrieve(record.location)
    with pytest.raises(DriveForeignObject):
        with stack.storage.open_stream(record.location, expected_md5=record.md5, expected_size=record.size):
            pass
    with pytest.raises(DriveForeignObject):
        with stack.storage.local_copy(record.location):
            pass
    with pytest.raises(DriveForeignObject):
        stack.storage.restore(record.location, KIND)
    assert [r for r in stack.fake.requests[start:] if r["query"].get("alt") == "media"] == []  # not one byte was downloaded


def test_the_change_feed_notices_a_document_that_left_the_drive(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "feed-leaver")
    stack.manager.sync_changes(db)  # baseline
    stack.fake.external_move_to_drive(file_id, OTHER)
    stack.manager.sync_changes(db)
    db.refresh(record)
    assert record.state == sr.MISSING_EXTERNAL


def test_a_document_that_comes_back_is_recovered_and_served_again(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record, file_id = stored_pair(stack, db, "boomerang")
    stack.fake.external_move_to_drive(file_id, OTHER)
    stack.manager.reconcile(db)
    stack.manager.reconcile(db)  # still outside: stays MISSING_EXTERNAL, recovery is rejected (once)
    db.refresh(record)
    assert record.state == sr.MISSING_EXTERNAL and len(events(db, record, "RECOVERY_REJECTED")) == 1
    stack.fake.external_move_to_drive(file_id, SHARED)  # back where it belongs
    report = stack.manager.reconcile(db)
    db.refresh(record)
    assert record.state == sr.STORED and report.recovered_externally == 1 and stack.storage.retrieve(record.location) == pdf_bytes("boomerang")
