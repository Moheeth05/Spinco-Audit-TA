"""Phase 3A.1.1 - corrections D1-D6 found by the Phase 3A.1 verification review.

  D1  an existing Drive object is never ADOPTED (retried upload, folder created by an earlier attempt, stale upload, recovered file) unless it is a
      live object of the expected Shared Drive
  D2/D6  the client OWNS its guard: an unguarded production client cannot exist, the guard cannot be cleared, and guard and folders share one root
  D3  an outage costs about a second, is remembered for a short window, never blocks readers of the guard state, and never holds a module lock
  D4  trashed folders are not accepted as destinations
  D5  listings are scoped to the expected Shared Drive and an `incompleteSearch` answer is never trusted
  +   the fake Drive returns only the fields a request asks for, so a missing field-mask entry is caught

All of it runs against the FAKE Drive: it proves OUR code behaves as designed, not how Google behaves."""

import hashlib
import importlib.util
import re
import sys
import threading
import time
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.api.storage import _drive_failure
from app.database import Base, SessionLocal, engine
from app.models import storage_record as sr
from app.models.storage_record import DocumentStorageRecord, StorageEvent
from app.services.drive import guard as guard_module
from app.services.drive import runtime
from app.services.drive.client import (
    FILE_FIELDS,
    FOLDER_MIME,
    DriveClient,
    DriveIncompleteListing,
    DriveTransientError,
    UnguardedClientRefused,
)
from app.services.drive.errors import DriveRootMismatch
from app.services.drive.config import DEFAULT_GUARD_FAILURE_WINDOW_SECONDS, DriveConfigError, load_runtime_config
from app.services.drive.guard import DriveTargetError
from app.services.drive.lifecycle import StorageUploadError
from app.services.drive.storage import DriveFolders, GoogleDriveStorage, make_location, parse_location

from drive_testkit import guarded_view, make_stack, pdf_bytes
from fake_drive import FOLDER, project
from test_drive_guard import KIND, OTHER, SHARED, Clock, guarded, runtime_env, runtime_stack, sha, snapshot, writes

SPIKE_DIR = Path(__file__).resolve().parents[1] / "drive_spike"


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def events(db, record, event_type):
    return db.execute(select(StorageEvent).where(StorageEvent.record_id == record.id, StorageEvent.event_type == event_type)).scalars().all()


def destination_reads(stack):
    root = stack.config.root_folder_id
    return [r for r in stack.fake.requests if r["method"] == "GET" and r["path"] == f"/drive/v3/files/{root}" and r["query"].get("fields") == guard_module.DESTINATION_FIELDS]


def foreign_file(stack, content, *, drive=OTHER, trashed=False, name="foreign.pdf", properties=None):
    """An object that ALREADY exists under a reserved id inside our active folder but is not a live object of the expected drive (drive=None: My Drive)."""
    reserved = stack.storage.reserve_id()
    stack.fake._add(reserved, name, "application/pdf", [stack.folders.active(KIND)], content=content, app_properties=properties)
    record = stack.fake.files[reserved]
    if drive is None:
        record.pop("driveId", None)
    else:
        record["driveId"] = drive
    if trashed:
        record["trashed"] = record["explicitlyTrashed"] = True
    return reserved


def pending_record(db, stack, content, reserved, *, state=sr.UPLOADING):
    record = DocumentStorageRecord(
        kind=KIND, sha256=sha(content), original_filename="p.pdf", size=len(content), md5=hashlib.md5(content).hexdigest(),
        location=make_location(reserved), state=state, created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(record)
    db.commit()
    stack.manager._spool(record, content)
    return record


# ====================================================================================================================== D1: adoption

ADOPTION_CASES = [
    ("another Shared Drive", {"drive": OTHER}, "adopt_wrong_drive"),
    ("a My Drive", {"drive": None}, "adopt_my_drive"),
    ("the trash", {"drive": SHARED, "trashed": True}, "adopt_trashed"),
]


@pytest.mark.parametrize(("label", "options", "reason"), ADOPTION_CASES, ids=[c[0] for c in ADOPTION_CASES])
def test_an_upload_conflict_never_adopts_an_object_outside_the_expected_drive(tmp_path, label, options, reason):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("adopt")
    reserved = foreign_file(stack, content, **options)
    before = snapshot(stack)
    for _ in range(3):  # and retrying never changes the answer: the check runs at every adoption
        with pytest.raises(DriveTargetError) as caught:
            stack.storage.upload(KIND, sha(content), "x.pdf", content, reserved_id=reserved)
        assert caught.value.reason == reason and caught.value.retryable is False
    assert snapshot(stack) == before  # the foreign object was not touched, renamed, moved or claimed


def test_an_upload_conflict_still_adopts_its_own_earlier_attempt_in_the_expected_drive(tmp_path):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("mine")
    reserved = foreign_file(stack, content, drive=SHARED)  # an earlier, ambiguous attempt that really did create the file here
    outcome = stack.storage.upload(KIND, sha(content), "x.pdf", content, reserved_id=reserved)
    assert outcome.adopted and outcome.obj.id == reserved and outcome.obj.drive_id == SHARED


def test_a_rejected_adoption_leaves_the_pending_record_failed_never_stored_and_a_retry_cannot_bypass_it(tmp_path, db):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("pending")
    reserved = foreign_file(stack, content, drive=OTHER)
    stack.storage._id_pool.append(reserved)  # the next reserved id is the one the foreign object already uses
    with pytest.raises(StorageUploadError) as first:
        stack.manager.store(db, kind=KIND, content=content, original_filename="p.pdf")
    record = first.value.record
    assert first.value.retryable is False and record.state == sr.FAILED and record.state != sr.STORED
    assert record.location == make_location(reserved) and "DriveTargetError" in (record.last_error or "")
    assert events(db, record, "UPLOAD_ADOPTION_REJECTED") and not events(db, record, "STORED")
    assert stack.manager._spooled(record) == content  # the pending bytes are kept, not discarded
    with pytest.raises(StorageUploadError):  # the same bytes uploaded again meet the same check
        stack.manager.store(db, kind=KIND, content=content, original_filename="p.pdf")
    db.refresh(record)
    # 3A.1.2: submitting a FAILED upload again is the recovery path, so it starts a fresh retry budget (attempts 0 -> 1) and says so; both rejections stay in the history
    assert record.state == sr.FAILED and record.attempts == 1 and stack.fake.files[reserved]["driveId"] == OTHER
    assert len(events(db, record, "UPLOAD_ADOPTION_REJECTED")) == 2 and len(events(db, record, "UPLOAD_RETRY_REQUESTED")) == 1


@pytest.mark.parametrize(("label", "options", "reason"), ADOPTION_CASES, ids=[c[0] for c in ADOPTION_CASES])
def test_reconcile_never_adopts_a_stale_upload_whose_file_is_outside_the_expected_drive(tmp_path, db, label, options, reason):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("stale")
    reserved = foreign_file(stack, content, **options)
    record = pending_record(db, stack, content, reserved)
    before = snapshot(stack)
    report = stack.manager.reconcile(db)
    db.refresh(record)
    if reason == "adopt_trashed":  # a trashed file is not "found" by the stale-upload lookup: the bytes are re-sent, and THAT is refused at the conflict
        assert record.state in (sr.FAILED, sr.UPLOADING) and record.state != sr.STORED
    else:
        assert record.state == sr.FAILED and report.failed_uploads == 1 and report.adopted_uploads == 0
        assert events(db, record, "UPLOAD_ADOPTION_REJECTED")
    assert record.state != sr.STORED and record.location == make_location(reserved) and stack.manager._spooled(record) == content
    assert snapshot(stack) == before


def test_reconcile_still_adopts_a_stale_upload_that_is_in_the_expected_drive(tmp_path, db):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("stale-ok")
    reserved = foreign_file(stack, content, drive=SHARED)
    record = pending_record(db, stack, content, reserved)
    report = stack.manager.reconcile(db)
    db.refresh(record)
    assert record.state == sr.STORED and report.adopted_uploads == 1


def test_a_structural_folder_created_by_an_earlier_attempt_is_adopted_only_inside_the_expected_drive(tmp_path):
    stack, _ = guarded(tmp_path)
    root = stack.config.root_folder_id
    stack.fake._add("EARLIERATTEMPT0001", "somewhere", FOLDER, [root])
    stack.fake.files["EARLIERATTEMPT0001"]["driveId"] = OTHER  # a folder that has OUR reserved id but lives in another drive
    stack.client.generate_ids = lambda count=1: ["EARLIERATTEMPT0001"]
    before = snapshot(stack)
    with pytest.raises(DriveTargetError) as caught:
        stack.folders.archive_root()
    assert caught.value.reason == "adopt_wrong_drive" and caught.value.retryable is False and snapshot(stack) == before
    stack.fake.files["EARLIERATTEMPT0001"]["driveId"] = SHARED  # the same situation inside the expected drive: adopted
    assert stack.folders.archive_root() == "EARLIERATTEMPT0001"


def test_deduplication_also_checks_the_drive_of_the_object_it_would_reuse(tmp_path):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("dedupe")
    properties = {"sha256": sha(content), "kind": KIND, "spinco": "1"}
    foreign_file(stack, content, drive=OTHER, properties=properties)
    stack.fake.ignore_drive_scope = True  # a Drive that leaks an out-of-scope item into a drive-scoped listing: defence in depth
    with pytest.raises(DriveTargetError) as caught:
        stack.storage.upload(KIND, sha(content), "x.pdf", content)
    assert caught.value.reason == "adopt_wrong_drive"


def test_a_recovered_file_is_adopted_again_only_inside_the_expected_drive(tmp_path, db):
    stack, _ = guarded(tmp_path)
    record = stack.manager.store(db, kind=KIND, content=pdf_bytes("recover"), original_filename="r.pdf").record
    file_id = parse_location(record.location)
    stack.fake.external_trash(file_id)
    stack.manager.reconcile(db)
    db.refresh(record)
    assert record.state == sr.MISSING_EXTERNAL
    stack.fake.external_untrash(file_id)
    stack.fake.files[file_id]["driveId"] = OTHER  # it reappears, but not in the expected drive
    stack.manager.reconcile(db)
    stack.manager.reconcile(db)
    db.refresh(record)
    assert record.state == sr.MISSING_EXTERNAL and len(events(db, record, "RECOVERY_REJECTED")) == 1  # reported once, never adopted
    stack.fake.files[file_id]["driveId"] = SHARED  # back in the expected drive: now it is recovered
    stack.manager.reconcile(db)
    db.refresh(record)
    assert record.state == sr.STORED and events(db, record, "RECOVERED_EXTERNALLY")


def test_adoption_checks_need_no_network_and_a_harness_client_adopts_as_before(tmp_path):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("offline-check")
    reserved = foreign_file(stack, content, drive=SHARED)
    found = stack.client.get_file(reserved)
    start = len(stack.fake.requests)
    assert stack.client.confirm_adoptable(found) is found and len(stack.fake.requests) == start  # a pure comparison
    (tmp_path / "plain").mkdir()
    plain = make_stack(tmp_path / "plain")
    foreign = types.SimpleNamespace(drive_id=OTHER, trashed=False)
    assert plain.client.confirm_adoptable(foreign) is foreign  # an unguarded harness client has no expected drive to compare with


def test_the_api_reports_an_adoption_refusal_differently_from_an_unverified_destination():
    refused_object = _drive_failure(DriveTargetError("adopt_wrong_drive", retryable=False))
    unverified = _drive_failure(DriveTargetError("wrong_drive"))
    assert refused_object.status_code == 409 and "not in the expected Shared Drive" in refused_object.detail
    assert unverified.status_code == 503 and "could not be verified" in unverified.detail
    assert SHARED not in refused_object.detail + unverified.detail


# ====================================================================================================================== D2 / D6: ownership and root binding


def test_a_client_cannot_exist_without_an_expected_drive_unless_it_explicitly_opts_out(tmp_path):
    plain = make_stack(tmp_path)
    with pytest.raises(UnguardedClientRefused, match="unguarded=True"):
        DriveClient(plain.config, plain.tokens, plain.http)


def test_the_opt_out_is_refused_whenever_the_environment_is_production(tmp_path, monkeypatch):
    plain = make_stack(tmp_path)
    DriveClient(plain.config, plain.tokens, plain.http, unguarded=True)  # allowed for a harness or a fake while the environment is explicitly dev (see conftest)
    monkeypatch.setenv("SPINCO_ENVIRONMENT", "production")
    with pytest.raises(UnguardedClientRefused, match="explicitly"):
        DriveClient(plain.config, plain.tokens, plain.http, unguarded=True)
    monkeypatch.setenv("SPINCO_ENVIRONMENT", " Production ")
    with pytest.raises(UnguardedClientRefused):
        DriveClient(plain.config, plain.tokens, plain.http, unguarded=True)
    monkeypatch.setenv("SPINCO_ENVIRONMENT", "dev")
    with pytest.raises(UnguardedClientRefused, match="explicitly"):
        DriveClient(replace(plain.config, environment="production"), plain.tokens, plain.http, unguarded=True)  # the config says production too


@pytest.mark.parametrize("name", ["dev", "staging"])
def test_the_opt_out_stays_available_to_harnesses_outside_production(tmp_path, monkeypatch, name):
    plain = make_stack(tmp_path)
    monkeypatch.setenv("SPINCO_ENVIRONMENT", name)
    assert DriveClient(plain.config, plain.tokens, plain.http, unguarded=True).write_guard is None


def test_the_opt_out_contradicts_a_configured_expected_drive(tmp_path):
    plain = make_stack(tmp_path)
    with pytest.raises(UnguardedClientRefused, match="contradicts"):
        DriveClient(replace(plain.config, expected_drive_id=SHARED), plain.tokens, plain.http, unguarded=True)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_expected_drive_cannot_build_a_client(tmp_path, blank):
    plain = make_stack(tmp_path)
    with pytest.raises(DriveTargetError) as caught:
        DriveClient(replace(plain.config, expected_drive_id=blank), plain.tokens, plain.http)
    assert caught.value.reason == "not_configured"


def test_there_is_no_public_way_to_install_swap_or_clear_a_guard(tmp_path):
    stack, guard = guarded(tmp_path)
    assert not hasattr(DriveClient, "set_write_guard")
    with pytest.raises(AttributeError):
        stack.client.write_guard = None
    assert stack.client.write_guard is guard


def test_the_guard_verifies_the_clients_own_configured_root(tmp_path):
    stack, guard = guarded(tmp_path)
    assert guard.root_folder_id == stack.config.root_folder_id == stack.client.guarded_root
    (tmp_path / "x").mkdir()
    assert make_stack(tmp_path / "x").client.guarded_root is None  # an explicitly unguarded harness client has no guarded root


def test_the_folder_structure_must_use_the_root_the_guard_verifies(tmp_path):
    stack, _ = guarded(tmp_path)
    with pytest.raises(DriveRootMismatch):
        DriveFolders(stack.client, "SOMEOTHERROOT00001")
    DriveFolders(stack.client, stack.config.root_folder_id)  # the matching root is fine


def test_the_storage_must_share_one_client_and_one_root_with_its_folders(tmp_path):
    stack, _ = guarded(tmp_path)
    other = guarded_view(stack, SHARED, root_folder_id=SHARED)  # a second guarded client whose guard verifies a DIFFERENT root
    with pytest.raises(DriveRootMismatch):
        GoogleDriveStorage(stack.client, other.folders)  # folders built on another client/root
    with pytest.raises(DriveRootMismatch):
        GoogleDriveStorage(other.client, stack.folders)
    GoogleDriveStorage(stack.client, stack.folders)  # consistent: fine


def test_an_unguarded_harness_client_may_root_its_folders_in_any_run_folder(tmp_path):
    plain = make_stack(tmp_path)
    assert DriveFolders(plain.client, "ANYRUNFOLDER000001").root_id == "ANYRUNFOLDER000001"  # the spike harnesses build their structure in a run sub-folder


def test_the_runtime_builds_a_guarded_client_for_the_configured_root(tmp_path):
    stack, env = runtime_stack(tmp_path)
    manager = build_manager_for(env, stack)
    client = manager.drive.client
    assert client.write_guard is not None and client.guarded_root == stack.config.root_folder_id == manager.drive.folders.root_id


def build_manager_for(env, stack):
    return runtime.build_manager(env, http=stack.http)


def test_without_the_destination_settings_the_runtime_never_even_constructs_a_client(tmp_path, monkeypatch):
    stack, env = runtime_stack(tmp_path)
    monkeypatch.setattr(runtime, "DriveClient", lambda *a, **k: pytest.fail("a DriveClient was constructed with an incomplete configuration"))
    broken = {k: v for k, v in env.items() if k != "SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING"}
    with pytest.raises(DriveConfigError):
        runtime.build_manager(broken, http=stack.http)


def code_without_comments(path: Path) -> str:
    return "\n".join(line.split("#")[0] for line in path.read_text(encoding="utf-8").splitlines())


def test_the_opt_out_is_used_only_by_tests_and_harnesses_never_by_the_application():
    backend = Path(__file__).resolve().parents[1]
    skipped = {".venv", "build", "dist_backend", "__pycache__", "pdf_store", "storage"}
    users = set()
    for top in (d for d in backend.iterdir() if d.is_dir() and d.name not in skipped):  # not the virtual environment or build output
        for path in top.rglob("*.py"):
            if "__pycache__" not in path.parts and re.search(r"[,(]\s*unguarded\s*=\s*True\b", code_without_comments(path)):  # a keyword ARGUMENT, not the words in a message
                users.add(top.name)
    assert users and users <= {"tests", "drive_spike"}, users  # nothing under app/


def test_nothing_in_the_application_assigns_or_replaces_a_clients_guard():
    app = Path(__file__).resolve().parents[1] / "app"
    offenders = [p.name for p in app.rglob("*.py") if p.name != "client.py" and re.search(r"\._write_guard\s*=|set_write_guard", code_without_comments(p))]
    assert offenders == []
    client_code = code_without_comments(app / "services" / "drive" / "client.py")
    assert len(re.findall(r"self\._write_guard(?::[^=\n]+)?\s*=", client_code)) == 2  # the initial None and the guard built in __init__: nowhere else


def load_harness(name):
    spec = importlib.util.spec_from_file_location(f"{name}_corrections", SPIKE_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SPIKE_DIR))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SPIKE_DIR))
    return module


def test_the_oauth_harness_is_isolated_from_production_and_says_so(tmp_path, monkeypatch):
    harness = load_harness("run_oauth_live")
    monkeypatch.setenv("SPINCO_ENVIRONMENT", "production")
    ctx = harness.Context("FAKE", tmp_path)
    assert harness.build(ctx) is False and ctx.blocked_kind == "environment" and "dev" in ctx.blocked_reason and "staging" in ctx.blocked_reason
    monkeypatch.setenv("SPINCO_ENVIRONMENT", "dev")
    ok = harness.Context("FAKE", tmp_path / "dev")
    (tmp_path / "dev").mkdir()
    assert harness.build(ok) is True and ok.client.write_guard is None


def test_both_live_harnesses_opt_out_explicitly_and_handle_the_refusal():
    for name in ("run_live_spike.py", "run_oauth_live.py"):
        text = (SPIKE_DIR / name).read_text(encoding="utf-8")
        assert re.search(r"DriveClient\([^)]*unguarded=True", text) and "UnguardedClientRefused" in text, name


# ====================================================================================================================== D3: outage behaviour


def test_a_failed_verification_costs_about_a_second_not_half_a_minute(tmp_path):
    stack, guard = guarded(tmp_path)  # ordinary reads keep the full retry budget (6 attempts)
    root = stack.config.root_folder_id
    stack.fake.drop_connection_next(1000, match=lambda r: r.url.path == f"/drive/v3/files/{root}")
    stack.sleeps.clear()
    with pytest.raises(DriveTargetError):
        guard.assert_ready()
    assert len(destination_reads(stack)) == 2 and sum(stack.sleeps) <= 1.0  # two attempts, one short backoff
    stack.fake.faults.clear()
    stack.fake.drop_connection_next(1000, match=lambda r: r.url.path.endswith("/files/generateIds"))
    stack.sleeps.clear()
    with pytest.raises(DriveTransientError):
        stack.client.generate_ids(1)
    assert sum(stack.sleeps) > 20  # an ordinary read still retries six times: the reduction applies only to verification reads


def test_get_raw_honours_a_smaller_attempt_budget(tmp_path):
    stack, _ = guarded(tmp_path, install=False)
    root = stack.config.root_folder_id
    stack.fake.drop_connection_next(1000, match=lambda r: r.url.path == f"/drive/v3/files/{root}")
    with pytest.raises(DriveTransientError):
        stack.client.get_raw(root, "id", attempts=3)
    assert len([r for r in stack.fake.requests if r["path"] == f"/drive/v3/files/{root}"]) == 3


def test_an_outage_opens_a_short_circuit_breaker_and_writes_recover_by_themselves(tmp_path):
    clock = Clock()
    stack, guard = guarded(tmp_path, clock=clock, failure_window=15.0)
    root = stack.config.root_folder_id
    stack.fake.drop_connection_next(1000, match=lambda r: r.url.path == f"/drive/v3/files/{root}")
    with pytest.raises(DriveTargetError) as first:
        guard.assert_ready()
    assert first.value.reason == "unverifiable" and guard.status()["circuit_open"] is True and guard.verifications == 1
    start = len(stack.fake.requests)
    for _ in range(25):  # while the breaker is open every write is refused AT ONCE: no network wait, no new request
        with pytest.raises(DriveTargetError):
            stack.client.create_folder("x", root)
    assert len(stack.fake.requests) == start and guard.verifications == 1
    clock.advance(14)
    with pytest.raises(DriveTargetError):
        guard.assert_ready()
    assert guard.verifications == 1  # still open at 14 s
    clock.advance(2)  # 16 s: the window is over, Google is back
    stack.fake.faults.clear()
    guard.assert_ready()
    assert guard.verified and guard.verifications == 2 and guard.status()["circuit_open"] is False


def test_a_second_outage_after_recovery_opens_the_breaker_again(tmp_path):
    clock = Clock()
    stack, guard = guarded(tmp_path, clock=clock, ttl=60.0, failure_window=15.0)
    root = stack.config.root_folder_id
    guard.assert_ready()
    clock.advance(61)
    stack.fake.drop_connection_next(1000, match=lambda r: r.url.path == f"/drive/v3/files/{root}")
    with pytest.raises(DriveTargetError):
        guard.assert_ready()
    assert guard.status()["circuit_open"] is True and guard.verifications == 2


def test_a_definitive_refusal_does_not_open_the_breaker(tmp_path):
    stack, guard = guarded(tmp_path, expected=OTHER)
    for _ in range(3):
        with pytest.raises(DriveTargetError) as caught:
            guard.assert_ready()
        assert caught.value.reason == "wrong_drive"
    assert guard.verifications == 3 and guard.status()["circuit_open"] is False  # a wrong drive is answered quickly: nothing to protect callers from


def test_many_writers_during_an_outage_share_one_failed_verification(tmp_path):
    stack, guard = guarded(tmp_path)
    root = stack.config.root_folder_id
    stack.fake.drop_connection_next(1000, match=lambda r: r.url.path == f"/drive/v3/files/{root}")
    barrier, outcomes = threading.Barrier(8), []

    def writer():
        barrier.wait()
        try:
            guard.assert_ready()
            outcomes.append("allowed")
        except DriveTargetError as exc:
            outcomes.append(exc.reason)

    threads = [threading.Thread(target=writer) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(20) for t in threads]
    assert outcomes == ["unverifiable"] * 8  # every writer refused, none allowed
    assert guard.verifications == 1 and len(destination_reads(stack)) == 2  # ONE verification, two attempts, shared by all eight


def blocked_verification(stack):
    """Make the verification read of the destination stall until released (the match function runs inside the fake's request handling)."""
    root = stack.config.root_folder_id
    entered, release = threading.Event(), threading.Event()

    def stall(request):
        if request.url.path == f"/drive/v3/files/{root}":
            entered.set()
            release.wait(15)
        return False  # never injects a fault: it only delays

    stack.fake.fail_next(1, 500, "backendError", match=stall)
    return entered, release


def test_reading_or_invalidating_the_guard_never_waits_for_a_verification_in_flight(tmp_path):
    stack, guard = guarded(tmp_path)
    entered, release = blocked_verification(stack)
    worker = threading.Thread(target=guard.try_verify)
    worker.start()
    assert entered.wait(10)  # the verification is now waiting on the network
    started = time.monotonic()
    assert guard.verified is False and guard.status()["verified"] is False
    guard.invalidate()
    assert time.monotonic() - started < 1.0
    release.set()
    worker.join(15)
    assert guard.verified is False  # the invalidate() that raced with the verification means its result was not cached
    assert guard.try_verify() is True and guard.verified


def test_a_second_writer_waits_for_the_first_verification_instead_of_starting_another(tmp_path):
    stack, guard = guarded(tmp_path)
    entered, release = blocked_verification(stack)
    results = []
    first = threading.Thread(target=lambda: results.append(guard.try_verify()))
    first.start()
    assert entered.wait(10)
    second = threading.Thread(target=lambda: results.append(guard.try_verify()))
    second.start()
    time.sleep(0.2)
    assert guard.verifications == 1  # the second one is queued behind the first, not verifying on its own
    release.set()
    first.join(15), second.join(15)
    assert results == [True, True] and guard.verifications == 1


def test_get_storage_manager_does_not_hold_the_module_lock_while_verifying(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_verify():
        entered.set()
        release.wait(15)
        return True

    fake_manager = types.SimpleNamespace(drive=types.SimpleNamespace(client=types.SimpleNamespace(write_guard=types.SimpleNamespace(try_verify=slow_verify))))
    monkeypatch.setenv("SPINCO_GDRIVE_ENABLED", "true")
    monkeypatch.setattr(runtime, "build_manager", lambda *a, **k: fake_manager)
    runtime.reset_cache()
    results = []
    first = threading.Thread(target=lambda: results.append(runtime.get_storage_manager()))
    first.start()
    try:
        assert entered.wait(10)  # the first request is inside the (slow) verification
        assert runtime._lock.acquire(timeout=1.0), "the module-wide lock was held during network verification"
        runtime._lock.release()
        assert runtime.get_storage_manager() is fake_manager  # another request is served at once, without waiting for the verification
    finally:
        release.set()
        first.join(15)
        runtime.reset_cache()
    assert results == [fake_manager]


def test_the_failure_window_is_configurable_and_bounded(tmp_path):
    assert load_runtime_config(runtime_env(tmp_path)).guard_failure_window_seconds == DEFAULT_GUARD_FAILURE_WINDOW_SECONDS == 15.0
    assert load_runtime_config(runtime_env(tmp_path, SPINCO_GDRIVE_GUARD_FAILURE_WINDOW_SECONDS="30")).guard_failure_window_seconds == 30.0
    for bad in ("0", "0.5", "121", "-3", "soon", "nan"):
        with pytest.raises(DriveConfigError, match="SPINCO_GDRIVE_GUARD_FAILURE_WINDOW_SECONDS"):
            load_runtime_config(runtime_env(tmp_path, SPINCO_GDRIVE_GUARD_FAILURE_WINDOW_SECONDS=bad))


# ====================================================================================================================== D4: trashed destinations


def trash(stack, file_id, *, explicitly=True):
    stack.fake.files[file_id]["trashed"] = True
    stack.fake.files[file_id]["explicitlyTrashed"] = explicitly


def test_a_trashed_folder_is_refused_as_an_upload_destination(tmp_path):
    stack, _ = guarded(tmp_path)
    first = pdf_bytes("one")
    stack.storage.upload(KIND, sha(first), "1.pdf", first)
    trash(stack, stack.folders.active(KIND))
    before, start = snapshot(stack), len(stack.fake.requests)
    second = pdf_bytes("two")
    with pytest.raises(DriveTargetError) as caught:
        stack.storage.upload(KIND, sha(second), "2.pdf", second)
    assert caught.value.reason == "trashed" and snapshot(stack) == before and writes(stack, start) == []


def test_a_trashed_folder_is_refused_as_a_parent_for_a_new_folder(tmp_path):
    stack, _ = guarded(tmp_path)
    folder = stack.folders.active(KIND)
    trash(stack, folder)
    with pytest.raises(DriveTargetError) as caught:
        stack.client.create_folder("Child", folder)
    assert caught.value.reason == "trashed"


def test_a_trashed_folder_is_refused_as_a_move_target(tmp_path):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("mover")
    created = stack.storage.upload(KIND, sha(content), "m.pdf", content)
    archive = stack.folders.archive(KIND)
    trash(stack, archive)
    before = snapshot(stack)
    with pytest.raises(DriveTargetError) as caught:
        stack.storage.archive(created.location, KIND)
    assert caught.value.reason == "trashed" and snapshot(stack) == before


@pytest.mark.parametrize("explicitly", [True, False])
def test_a_folder_trashed_only_through_its_parent_is_refused_too(tmp_path, explicitly):
    stack, _ = guarded(tmp_path)
    folder = stack.folders.active(KIND)
    trash(stack, folder, explicitly=explicitly)  # `trashed` is true either way
    with pytest.raises(DriveTargetError):
        stack.client.create_folder("Child", folder)


def test_a_trashed_object_may_be_changed_only_to_restore_it(tmp_path):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("trashed-object")
    created = stack.storage.upload(KIND, sha(content), "t.pdf", content)
    stack.fake.external_trash(created.obj.id)
    with pytest.raises(DriveTargetError) as caught:  # renaming (or moving) a trashed object is not a restoration
        stack.client.update_file(created.obj.id, name="renamed.pdf")
    assert caught.value.reason == "trashed" and stack.fake.files[created.obj.id]["name"] != "renamed.pdf"
    restored = stack.client.update_file(created.obj.id, trashed=False)  # explicitly restoring it is allowed
    assert restored.trashed is False


def test_restoring_a_trashed_document_through_the_adapter_still_works_under_the_guard(tmp_path):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("restore-from-trash")
    created = stack.storage.upload(KIND, sha(content), "r.pdf", content)
    stack.fake.external_trash(created.obj.id)
    restored = stack.storage.restore(created.location, KIND)
    assert restored.trashed is False and restored.parents == (stack.folders.active(KIND),)


# ====================================================================================================================== D5: drive-scoped listing and incompleteSearch


def last_listing(stack):
    return [r for r in stack.fake.requests if r["path"] == "/drive/v3/files" and r["method"] == "GET"][-1]["query"]


def test_a_guarded_client_lists_only_inside_the_expected_shared_drive(tmp_path):
    stack, _ = guarded(tmp_path)
    stack.client.find_children(stack.config.root_folder_id)
    query = last_listing(stack)
    assert query["corpora"] == "drive" and query["driveId"] == SHARED
    assert query["includeItemsFromAllDrives"] == "true" and query["supportsAllDrives"] == "true" and "incompleteSearch" in query["fields"]


def test_a_harness_client_keeps_the_broad_search_but_still_refuses_incomplete_results(tmp_path):
    plain, _ = guarded(tmp_path, install=False)
    plain.client.find_children(plain.config.root_folder_id)
    query = last_listing(plain)
    assert query["corpora"] == "allDrives" and "driveId" not in query
    plain.fake.incomplete_next(1)
    with pytest.raises(DriveIncompleteListing):
        plain.client.find_children(plain.config.root_folder_id)


def test_a_folder_with_the_same_name_in_another_drive_is_never_used(tmp_path):
    stack, _ = guarded(tmp_path)
    root = stack.config.root_folder_id
    stack.fake._add("FOREIGNFOLDER00001", "Expense Reports", FOLDER, [root])
    stack.fake.files["FOREIGNFOLDER00001"]["driveId"] = OTHER
    mine = stack.folders.active(KIND)
    assert mine != "FOREIGNFOLDER00001" and stack.fake.files[mine]["driveId"] == SHARED
    assert [f.id for f in stack.client.find_children(root, folders_only=True)] == [mine]  # the child listing does not see the foreign folder


def test_an_incomplete_folder_lookup_fails_closed_and_creates_no_duplicate_folder(tmp_path):
    stack, _ = guarded(tmp_path)
    stack.folders.active(KIND)  # the folder exists
    existing = [f for f in stack.fake.files.values() if f["name"] == "Expense Reports"]
    fresh = guarded_view(stack, SHARED)  # a new structure object with an empty cache must look the folder up again
    stack.fake.incomplete_next(1, match=lambda r: "name =" in r.url.params.get("q", ""))
    start = len(stack.fake.requests)
    with pytest.raises(DriveIncompleteListing):  # a partial answer that omitted the folder must NOT be taken as "it does not exist"
        fresh.folders.active(KIND)
    assert writes(stack, start) == [] and len([f for f in stack.fake.files.values() if f["name"] == "Expense Reports"]) == len(existing) == 1
    assert fresh.folders.active(KIND) == existing[0]["id"]  # a complete listing finds it


def test_an_incomplete_duplicate_check_fails_closed_instead_of_creating_a_second_file(tmp_path):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("dup")
    stack.storage.upload(KIND, sha(content), "d.pdf", content)
    assert stack.fake.file_count() == 1
    stack.fake.incomplete_next(1, match=lambda r: "appProperties has" in r.url.params.get("q", ""))
    with pytest.raises(DriveIncompleteListing):  # trusting the partial answer would have uploaded a duplicate
        stack.storage.upload(KIND, sha(content), "d.pdf", content)
    assert stack.fake.file_count() == 1
    again = stack.storage.upload(KIND, sha(content), "d.pdf", content)  # the next, complete listing deduplicates
    assert again.deduplicated and stack.fake.file_count() == 1


def test_an_incomplete_listing_is_never_partly_returned(tmp_path):
    stack, _ = guarded(tmp_path)
    folder = stack.folders.active(KIND)
    for index in range(3):
        content = pdf_bytes(f"page-{index}")
        stack.storage.upload(KIND, sha(content), f"{index}.pdf", content)
    stack.fake.incomplete_next(1, match=lambda r: "pageToken" in r.url.params)  # the SECOND page is incomplete
    collected = []
    with pytest.raises(DriveIncompleteListing):
        for item in stack.client.list_files(f"'{folder}' in parents", page_size=1):
            collected.append(item)
    assert len(collected) == 1  # the complete first page was delivered; nothing from the incomplete page ever was
    assert len(stack.client.find_children(folder)) == 3  # and the helper that collects a list returns all or raises (checked above in the duplicate test)


def test_reconcile_reports_an_incomplete_orphan_scan_as_an_error_and_writes_nothing(tmp_path, db):
    stack, _ = guarded(tmp_path)
    stack.manager.initialise_structure()
    stack.fake.incomplete_next(1, match=lambda r: "name =" not in r.url.params.get("q", "") and "appProperties" not in r.url.params.get("q", ""))
    before, start = snapshot(stack), len(stack.fake.requests)
    report = stack.manager.reconcile(db)
    assert any("orphan scan" in message for message in report.errors)
    assert snapshot(stack) == before and writes(stack, start) == []


def test_the_incomplete_listing_error_is_retryable_and_secret_free():
    error = DriveIncompleteListing()
    assert error.retryable is True and error.reason == "incomplete_search" and "incomplete" in str(error)


# ====================================================================================================================== the fake returns only the requested fields


def test_the_fake_returns_only_the_fields_a_request_asks_for(tmp_path):
    stack, _ = guarded(tmp_path, install=False)
    root = stack.config.root_folder_id
    assert stack.client.get_raw(root, "id") == {"id": root}
    assert stack.client.get_raw(root, "id,capabilities(canAddChildren)") == {"id": root, "capabilities": {"canAddChildren": True}}
    assert set(stack.client.get_raw(root, "id,name,mimeType")) == {"id", "name", "mimeType"}


def test_without_a_fields_mask_the_fake_answers_like_google_with_its_default_fields(tmp_path):
    stack, _ = guarded(tmp_path, install=False)
    root = stack.config.root_folder_id
    answer = stack.client._request("GET", f"https://www.googleapis.com/drive/v3/files/{root}", params={"supportsAllDrives": "true"}).json()
    assert set(answer) == {"kind", "id", "name", "mimeType"} or set(answer) == {"id", "name", "mimeType"}  # never driveId, parents or capabilities


def test_the_projection_helper_handles_nesting_lists_and_missing_fields():
    record = {"id": "a", "parents": ["p"], "capabilities": {"canEdit": True, "canTrash": False}, "files": [{"id": "x", "name": "n"}, {"id": "y", "name": "m"}]}
    assert project(record, "id,parents") == {"id": "a", "parents": ["p"]}
    assert project(record, "capabilities(canTrash)") == {"capabilities": {"canTrash": False}}
    assert project(record, "files(id)") == {"files": [{"id": "x"}, {"id": "y"}]}
    assert project(record, "id,nothing,capabilities(nothing)") == {"id": "a", "capabilities": {}}


def test_a_destination_field_mask_that_forgets_a_field_the_guard_reads_is_caught(tmp_path, monkeypatch):
    stack, guard = guarded(tmp_path)
    monkeypatch.setattr(guard_module, "DESTINATION_FIELDS", "id,mimeType,driveId,trashed,explicitlyTrashed,capabilities(canAddChildren,canMoveItemWithinDrive)")  # `parents` forgotten
    with pytest.raises(DriveTargetError) as caught:
        guard.assert_ready()
    assert caught.value.reason == "hierarchy"  # real Google would omit `parents` too, so the guard would refuse everything: the fake now shows it


def test_an_object_field_mask_that_forgets_the_drive_id_is_caught(tmp_path, monkeypatch):
    stack, guard = guarded(tmp_path)
    guard.assert_ready()
    monkeypatch.setattr(guard_module, "OBJECT_FIELDS", "id,trashed,explicitlyTrashed")  # `driveId` forgotten
    with pytest.raises(DriveTargetError) as caught:
        stack.client.create_folder("x", stack.config.root_folder_id)
    assert caught.value.reason == "my_drive"


def test_every_field_the_guard_and_the_adoption_checks_read_is_in_the_masks():
    assert {"id", "mimeType", "driveId", "parents", "trashed", "explicitlyTrashed"} <= set(guard_module.DESTINATION_FIELDS.replace("(", ",").replace(")", "").split(","))
    assert {"driveId", "trashed", "explicitlyTrashed"} <= set(guard_module.OBJECT_FIELDS.split(","))
    assert "driveId" in FILE_FIELDS and "trashed" in FILE_FIELDS  # what confirm() and assert_adoptable() read from a written/read object
    assert guard_module._FOLDER_MIME == FOLDER_MIME  # the guard keeps its own copy of the constant; they must never drift apart
