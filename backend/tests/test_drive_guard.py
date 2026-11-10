"""Phase 3A.1 - the production destination guard (app/services/drive/guard.py), its configuration, and its enforcement on every write path.

Everything here runs against the FAKE Drive (tests/fake_drive.py). These tests prove that OUR code refuses to write unless the destination is
verified; they say nothing about how Google itself answers (that is what the read-only status command and the live harness are for)."""

import hashlib
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.database import Base, SessionLocal, engine
from app.models import storage_record as sr
from app.models.storage_record import DocumentStorageRecord
from app.services.drive import runtime
from app.services.drive.auth import DriveAuthError
from app.services.drive.client import DrivePermissionError, DriveNotFound
from app.services.drive.config import DriveConfig, DriveConfigError, load_config, load_runtime_config
from app.services.drive.errors import DriveForeignObject
from app.services.drive.guard import DESTINATION_FIELDS, DriveTargetError, DriveTargetGuard
from app.services.drive.lifecycle import StorageUploadError
from app.services.drive.runtime import build_manager
from app.services.drive.storage import DriveFolderMissing, make_location

from drive_testkit import guarded_view, make_stack, pdf_bytes, write_service_account_file
from fake_drive import FOLDER

KIND = "expense_report"
SHARED = "0ASyntheticSharedDrive01"
OTHER = "0AOtherSharedDrive0002"
STAGING = "0AStagingSharedDrive03"
ANCESTOR0 = "ANCESTOR00000000"
ANCESTOR1 = "ANCESTOR00000001"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def build_hierarchy(stack, nest):
    """A fake Shared Drive with a realistic hierarchy:  <drive root> / <nest ancestor folders> / <the configured destination folder>."""
    fake, root = stack.fake, stack.config.root_folder_id
    fake._add(SHARED, "Shared Drive", FOLDER, [])  # the Shared Drive's own root: its id IS the drive id
    parent = SHARED
    for index in range(nest):
        ancestor = f"ANCESTOR{index:08d}"
        fake._add(ancestor, f"Level {index}", FOLDER, [parent])
        parent = ancestor
    fake.files[root]["parents"] = [parent]


def guarded(tmp_path, *, expected=SHARED, nest=1, clock=None, ttl=300.0, max_attempts=6, max_depth=20, failure_window=15.0, install=True):
    """install=True (default): a stack whose CLIENT OWNS its guard (built from the configuration, exactly as the runtime does) -> (stack, guard).
    install=False: a plain stack (the harness/fake opt-out) plus a STANDALONE guard reading through it -> (stack, guard); the guard is not
    connected to any client's writes. Used to unit-test the guard itself and to create objects before a guarded view is made."""
    if install:
        stack = make_stack(
            tmp_path, drive_id=SHARED, max_attempts=max_attempts, expected_drive_id=expected, guard_ttl=ttl, guard_failure_window=failure_window,
            guard_clock=clock, guard_max_depth=max_depth,
        )
        build_hierarchy(stack, nest)
        return stack, stack.client.write_guard
    stack = make_stack(tmp_path, drive_id=SHARED, max_attempts=max_attempts)
    build_hierarchy(stack, nest)
    guard = DriveTargetGuard(stack.client, stack.config.root_folder_id, expected, ttl_seconds=ttl, failure_window_seconds=failure_window, clock=clock or time.monotonic, max_depth=max_depth)
    return stack, guard


def writes(stack, start=0):
    """Requests that could change Drive. The sign-in token exchange (/token) is not a Drive write."""
    return [r for r in stack.fake.requests[start:] if r["method"] != "GET" and r["path"] != "/token"]


def destination_reads(stack):
    root = stack.config.root_folder_id
    return [r for r in stack.fake.requests if r["method"] == "GET" and r["path"] == f"/drive/v3/files/{root}" and r["query"].get("fields") == DESTINATION_FIELDS]


def snapshot(stack):
    return {fid: (f["name"], tuple(f["parents"]), f["trashed"], f.get("driveId")) for fid, f in stack.fake.files.items()}


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


# ====================================================================================================================== configuration


def runtime_env(tmp_path, **overrides):
    env = {
        "SPINCO_GDRIVE_ROOT_FOLDER_ID": "ROOTFOLDER0000001",
        "SPINCO_GDRIVE_CREDENTIALS_FILE": str(write_service_account_file(tmp_path)),
        "SPINCO_ENVIRONMENT": "production",
        "SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION": SHARED,
    }
    for name, value in overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


@pytest.mark.parametrize("name", ["dev", "staging", "production"])
def test_each_environment_is_configured_with_its_own_explicit_expected_drive(tmp_path, name):
    overrides = {"SPINCO_ENVIRONMENT": name, "SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION": None}
    overrides[f"SPINCO_GDRIVE_EXPECTED_DRIVE_ID_{name.upper()}"] = SHARED  # ONLY this environment's own variable is set
    config = load_runtime_config(runtime_env(tmp_path, **overrides))
    assert config.environment == name and config.expected_drive_id == SHARED and config.guard_ttl_seconds == 300.0


def test_the_staging_drive_is_never_assumed_to_be_production(tmp_path):
    env = runtime_env(tmp_path, SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION=None, SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING=STAGING)
    with pytest.raises(DriveConfigError, match="SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION") as caught:
        load_runtime_config(env)
    assert STAGING not in str(caught.value)


def test_a_missing_expected_drive_id_is_refused(tmp_path):
    with pytest.raises(DriveConfigError, match="SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION"):
        load_runtime_config(runtime_env(tmp_path, SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION=None))
    with pytest.raises(DriveConfigError, match="SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION"):
        load_runtime_config(runtime_env(tmp_path, SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION="   "))


@pytest.mark.parametrize("value", [None, "", "prod", "live", "Prod-1"])
def test_the_environment_must_be_stated_explicitly_and_correctly(tmp_path, value):
    with pytest.raises(DriveConfigError, match="SPINCO_ENVIRONMENT"):
        load_runtime_config(runtime_env(tmp_path, SPINCO_ENVIRONMENT=value))


def test_the_environment_name_is_case_insensitive_but_nothing_else_is_guessed(tmp_path):
    assert load_runtime_config(runtime_env(tmp_path, SPINCO_ENVIRONMENT=" Production ")).environment == "production"


@pytest.mark.parametrize("bad", ["short", "has spaces and !! chars", "x" * 200])
def test_a_malformed_expected_drive_id_is_refused(tmp_path, bad):
    with pytest.raises(DriveConfigError, match="SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION"):
        load_runtime_config(runtime_env(tmp_path, SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION=bad))


def test_an_unscoped_expected_drive_id_is_refused_as_ambiguous(tmp_path):
    with pytest.raises(DriveConfigError, match="not supported"):
        load_runtime_config(runtime_env(tmp_path, SPINCO_GDRIVE_EXPECTED_DRIVE_ID=SHARED))


def test_a_misspelt_environment_scope_is_refused_not_ignored(tmp_path):
    with pytest.raises(DriveConfigError, match="not a recognised setting"):
        load_runtime_config(runtime_env(tmp_path, SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PROD=OTHER))


def test_production_cannot_share_an_id_with_another_environment(tmp_path):
    env = runtime_env(tmp_path, SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING=SHARED)  # the copy-paste mistake
    with pytest.raises(DriveConfigError, match="identical to the one configured for staging") as caught:
        load_runtime_config(env)
    assert SHARED not in str(caught.value)


def test_environments_may_share_one_settings_file_when_their_ids_differ(tmp_path):
    env = runtime_env(tmp_path, SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING=STAGING, SPINCO_GDRIVE_EXPECTED_DRIVE_ID_DEV="")
    assert load_runtime_config(env).expected_drive_id == SHARED
    assert load_runtime_config({**env, "SPINCO_ENVIRONMENT": "staging"}).expected_drive_id == STAGING


@pytest.mark.parametrize(("raw", "expected"), [(None, 300.0), ("60", 60.0), ("10", 10.0), ("900", 900.0)])
def test_the_verification_lifetime_is_bounded(tmp_path, raw, expected):
    assert load_runtime_config(runtime_env(tmp_path, SPINCO_GDRIVE_GUARD_TTL_SECONDS=raw)).guard_ttl_seconds == expected


@pytest.mark.parametrize("raw", ["5", "901", "0", "-1", "soon"])
def test_a_silly_verification_lifetime_is_refused(tmp_path, raw):
    with pytest.raises(DriveConfigError, match="SPINCO_GDRIVE_GUARD_TTL_SECONDS"):
        load_runtime_config(runtime_env(tmp_path, SPINCO_GDRIVE_GUARD_TTL_SECONDS=raw))


def test_load_config_and_direct_construction_are_unchanged_for_the_harnesses(tmp_path):
    env = runtime_env(tmp_path, SPINCO_ENVIRONMENT=None, SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION=None)
    config = load_config(env)  # the harnesses and fakes do not need the guard settings
    assert config.environment == "dev" and config.expected_drive_id is None
    assert DriveConfig(root_folder_id="ROOTFOLDER0000001").expected_drive_id is None


# ====================================================================================================================== verification


@pytest.mark.parametrize("nest", [0, 1, 4])
def test_the_expected_shared_drive_is_verified_and_writes_proceed(tmp_path, nest):
    stack, guard = guarded(tmp_path, nest=nest)
    content = pdf_bytes("ok")
    outcome = stack.storage.upload(KIND, sha(content), "ok.pdf", content)
    assert guard.verified and guard.last_refusal is None and outcome.obj.drive_id == SHARED and stack.fake.file_count() == 1


def test_the_destination_may_be_the_shared_drive_root_itself(tmp_path):
    stack, _ = guarded(tmp_path)
    guard = DriveTargetGuard(stack.client, SHARED, SHARED)
    guard.assert_ready()
    assert guard.verified


def test_a_different_shared_drive_is_refused(tmp_path):
    stack, guard = guarded(tmp_path, expected=OTHER)
    with pytest.raises(DriveTargetError) as caught:
        guard.assert_ready()
    assert caught.value.reason == "wrong_drive" and not guard.verified and writes(stack) == []


def test_a_my_drive_folder_is_refused(tmp_path):
    stack = make_stack(tmp_path)  # a plain My Drive fake: its folders carry no driveId
    guard = DriveTargetGuard(stack.client, stack.config.root_folder_id, SHARED)
    with pytest.raises(DriveTargetError) as caught:
        guard.assert_ready()
    assert caught.value.reason == "my_drive" and writes(stack) == []


def arrange_other_drive_ancestor(stack):
    stack.fake.files[ANCESTOR0]["driveId"] = OTHER


def arrange_chain_never_reaches_the_drive_root(stack):
    stack.fake.files[ANCESTOR0]["parents"] = []


def arrange_two_parents(stack):
    stack.fake.files[stack.config.root_folder_id]["parents"] = [ANCESTOR0, SHARED]


def arrange_loop(stack):
    stack.fake.files[ANCESTOR0]["parents"] = [ANCESTOR1]  # root -> A1 -> A0 -> A1 ...


def arrange_missing_ancestor(stack):
    stack.fake.external_delete(ANCESTOR0)


def arrange_trashed_ancestor(stack):
    stack.fake.files[ANCESTOR0]["trashed"] = True


def arrange_ancestor_is_a_file(stack):
    stack.fake.files[ANCESTOR0]["mimeType"] = "application/pdf"


def arrange_no_parent_at_all(stack):
    stack.fake.files[stack.config.root_folder_id]["parents"] = []


def arrange_drive_root_has_a_parent(stack):
    stack.fake.files[SHARED]["parents"] = ["SOMEWHEREELSE0001"]


HIERARCHY_CASES = [
    ("ancestor in another drive", arrange_other_drive_ancestor, "wrong_drive", {}),
    ("chain never reaches the drive root", arrange_chain_never_reaches_the_drive_root, "hierarchy", {}),
    ("two parents", arrange_two_parents, "hierarchy", {}),
    ("loop", arrange_loop, "hierarchy", {}),
    ("ancestor missing", arrange_missing_ancestor, "destination_missing", {}),
    ("ancestor trashed", arrange_trashed_ancestor, "trashed", {}),
    ("ancestor is a file", arrange_ancestor_is_a_file, "not_folder", {}),
    ("destination has no parent at all", arrange_no_parent_at_all, "hierarchy", {}),
    ("the drive root has a parent", arrange_drive_root_has_a_parent, "hierarchy", {}),
]


@pytest.mark.parametrize(("label", "arrange", "reason", "extra"), HIERARCHY_CASES, ids=[c[0] for c in HIERARCHY_CASES])
def test_an_incorrect_parent_hierarchy_is_refused(tmp_path, label, arrange, reason, extra):
    stack, guard = guarded(tmp_path, nest=2)
    arrange(stack)
    with pytest.raises(DriveTargetError) as caught:
        guard.assert_ready()
    assert caught.value.reason == reason and guard.last_refusal == reason and not guard.verified
    assert writes(stack) == []


def test_a_hierarchy_deeper_than_the_limit_is_refused(tmp_path):
    stack, guard = guarded(tmp_path, nest=5, max_depth=3)
    with pytest.raises(DriveTargetError) as caught:
        guard.assert_ready()
    assert caught.value.reason == "hierarchy"


def test_a_destination_that_is_not_a_folder_is_refused(tmp_path):
    stack, _ = guarded(tmp_path)
    stack.fake._add("AFILENOTAFOLDER001", "x.pdf", "application/pdf", [stack.config.root_folder_id])
    with pytest.raises(DriveTargetError) as caught:
        DriveTargetGuard(stack.client, "AFILENOTAFOLDER001", SHARED).assert_ready()
    assert caught.value.reason == "not_folder"


def test_a_destination_in_the_trash_is_refused(tmp_path):
    stack, guard = guarded(tmp_path)
    stack.fake.files[stack.config.root_folder_id]["trashed"] = True
    with pytest.raises(DriveTargetError) as caught:
        guard.assert_ready()
    assert caught.value.reason == "trashed"


@pytest.mark.parametrize("capability", ["canAddChildren", "canMoveItemWithinDrive"])
def test_an_account_that_cannot_add_and_move_items_is_refused(tmp_path, capability):
    stack, guard = guarded(tmp_path)
    stack.fake.files[stack.config.root_folder_id]["capabilities"][capability] = False
    with pytest.raises(DriveTargetError) as caught:
        guard.assert_ready()
    assert caught.value.reason == "no_capability"


@pytest.mark.parametrize("expected", ["", "   ", None])
def test_a_missing_expected_drive_id_cannot_even_build_a_guard(tmp_path, expected):
    stack, _ = guarded(tmp_path, install=False)
    with pytest.raises(DriveTargetError) as caught:
        DriveTargetGuard(stack.client, stack.config.root_folder_id, expected)
    assert caught.value.reason == "not_configured"
    with pytest.raises(DriveTargetError):
        DriveTargetGuard(stack.client, "", SHARED)


# ---- Google / network failures: every one fails closed


def test_a_google_permission_failure_during_verification_refuses_the_write(tmp_path):
    stack, guard = guarded(tmp_path)
    root = stack.config.root_folder_id
    stack.fake.fail_next(1, 403, "forbidden", match=lambda r: r.url.path == f"/drive/v3/files/{root}")
    with pytest.raises(DriveTargetError) as caught:
        guard.assert_ready()
    assert caught.value.reason == "destination_denied" and writes(stack) == []


def test_a_destination_that_cannot_be_found_refuses_the_write(tmp_path):
    stack, guard = guarded(tmp_path)
    stack.fake.external_delete(stack.config.root_folder_id)
    with pytest.raises(DriveTargetError) as caught:
        guard.assert_ready()
    assert caught.value.reason == "destination_missing"


def test_a_network_failure_during_verification_fails_closed_and_recovers_after_the_failure_window(tmp_path):
    clock = Clock()
    stack, guard = guarded(tmp_path, max_attempts=2, clock=clock, failure_window=15.0)
    root = stack.config.root_folder_id
    stack.fake.drop_connection_next(50, match=lambda r: r.url.path == f"/drive/v3/files/{root}")  # only the verification read is cut off
    content = pdf_bytes("offline")
    with pytest.raises(DriveTargetError) as caught:
        stack.storage.upload(KIND, sha(content), "o.pdf", content)
    assert caught.value.reason == "unverifiable" and writes(stack) == [] and not guard.verified
    stack.fake.faults.clear()  # the network is back ...
    with pytest.raises(DriveTargetError):  # ... but the circuit breaker is still open: refused at once, without asking Google
        stack.storage.upload(KIND, sha(content), "o.pdf", content)
    clock.advance(16)  # the failure window has passed: the next write verifies again and goes through (a failure is never remembered as a permanent block)
    assert stack.storage.upload(KIND, sha(content), "o.pdf", content).obj.drive_id == SHARED and guard.verified


def test_a_google_outage_during_verification_fails_closed(tmp_path):
    stack, guard = guarded(tmp_path, max_attempts=2)
    stack.fake.fail_next(50, 503, "backendError")
    with pytest.raises(DriveTargetError) as caught:
        guard.assert_ready()
    assert caught.value.reason == "unverifiable"


@pytest.mark.parametrize("answer", [["not", "a", "dict"], None, ValueError("not json"), DriveAuthError("sign-in failed")])
def test_an_unusable_answer_or_a_failed_sign_in_fails_closed(tmp_path, answer):
    stack, guard = guarded(tmp_path)

    def broken(*args, **kwargs):
        if isinstance(answer, Exception):
            raise answer
        return answer

    stack.client.get_raw = broken
    with pytest.raises(DriveTargetError) as caught:
        guard.assert_ready()
    assert caught.value.reason == "unverifiable"


def test_refusal_messages_carry_no_drive_id_path_or_credential(tmp_path):
    (tmp_path / "wrong").mkdir()
    (tmp_path / "my").mkdir()
    wrong_stack, wrong_guard = guarded(tmp_path / "wrong", expected=OTHER)
    my_stack = make_stack(tmp_path / "my")
    my_guard = DriveTargetGuard(my_stack.client, my_stack.config.root_folder_id, SHARED)
    for stack, guard in ((wrong_stack, wrong_guard), (my_stack, my_guard)):
        with pytest.raises(DriveTargetError) as caught:
            guard.assert_ready()
        text = str(caught.value) + repr(vars(caught.value)) + repr(guard) + repr(guard.status())
        for forbidden in (SHARED, OTHER, stack.config.root_folder_id, "fake-token", str(tmp_path)):
            assert forbidden not in text, forbidden


# ====================================================================================================================== cache lifetime


def test_a_verification_is_reused_within_its_lifetime_and_repeated_after_it(tmp_path):
    clock = Clock()
    stack, guard = guarded(tmp_path, clock=clock, ttl=60.0)
    guard.assert_ready()
    guard.assert_ready()
    clock.advance(59)
    guard.assert_ready()
    assert guard.verifications == 1 and len(destination_reads(stack)) == 1
    clock.advance(2)  # 61 seconds after the verification
    assert not guard.verified
    guard.assert_ready()
    assert guard.verifications == 2 and len(destination_reads(stack)) == 2


def test_invalidate_forces_a_fresh_verification(tmp_path):
    stack, guard = guarded(tmp_path)
    guard.assert_ready()
    guard.invalidate()
    assert not guard.verified
    guard.assert_ready()
    assert guard.verifications == 2


def test_a_refusal_is_never_cached_as_success(tmp_path):
    stack, guard = guarded(tmp_path, expected=OTHER)
    for _ in range(2):
        with pytest.raises(DriveTargetError):
            guard.assert_ready()
    assert guard.verifications == 2 and not guard.verified


@pytest.mark.parametrize(("status", "reason", "error"), [(403, "forbidden", DrivePermissionError), (404, "notFound", DriveNotFound)])
def test_a_write_refused_as_forbidden_or_missing_drops_the_cached_verification(tmp_path, status, reason, error):
    stack, guard = guarded(tmp_path)
    content = pdf_bytes("inv")
    created = stack.storage.upload(KIND, sha(content), "i.pdf", content)
    assert guard.verified
    stack.fake.fail_next(1, status, reason, match=lambda r: r.method == "PATCH")
    with pytest.raises(error):
        stack.client.update_file(created.obj.id, name="again.pdf")
    assert not guard.verified  # the next write re-verifies instead of trusting a stale answer
    stack.client.update_file(created.obj.id, name="again.pdf")
    assert guard.verified and guard.verifications >= 2


def test_concurrent_writes_share_one_verification(tmp_path):
    stack, guard = guarded(tmp_path)
    barrier = threading.Barrier(8)
    failures = []

    def worker():
        barrier.wait()
        try:
            guard.assert_ready()
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not failures and guard.verifications == 1 and len(destination_reads(stack)) == 1


def test_the_lifetime_must_be_positive(tmp_path):
    stack, _ = guarded(tmp_path, install=False)
    with pytest.raises(ValueError):
        DriveTargetGuard(stack.client, stack.config.root_folder_id, SHARED, ttl_seconds=0)


# ====================================================================================================================== things that move after verification


def test_a_destination_moved_to_another_drive_is_caught_when_the_verification_expires(tmp_path):
    clock = Clock()
    stack, guard = guarded(tmp_path, clock=clock, ttl=60.0)
    first = pdf_bytes("before")
    stack.storage.upload(KIND, sha(first), "b.pdf", first)  # verified: allowed
    stack.fake.files[stack.config.root_folder_id]["driveId"] = OTHER  # the folder is moved away AFTER the verification
    clock.advance(61)
    start = len(stack.fake.requests)
    second = pdf_bytes("after")
    with pytest.raises(DriveTargetError) as caught:
        stack.storage.upload(KIND, sha(second), "a.pdf", second)
    assert caught.value.reason == "wrong_drive" and writes(stack, start) == []


def test_a_folder_moved_out_of_the_drive_is_caught_immediately_for_the_object_being_written_into(tmp_path):
    stack, guard = guarded(tmp_path)
    first = pdf_bytes("one")
    stack.storage.upload(KIND, sha(first), "1.pdf", first)
    active = stack.folders.active(KIND)  # its id is cached by the adapter
    stack.fake.files[active]["driveId"] = OTHER  # moved out of the expected drive; the cached destination verification is still fresh
    start = len(stack.fake.requests)
    second = pdf_bytes("two")
    with pytest.raises(DriveTargetError) as caught:
        stack.storage.upload(KIND, sha(second), "2.pdf", second)
    assert caught.value.reason == "wrong_drive" and writes(stack, start) == []


def test_a_file_that_left_the_drive_cannot_be_archived(tmp_path):
    stack, guard = guarded(tmp_path)
    content = pdf_bytes("leaver")
    created = stack.storage.upload(KIND, sha(content), "l.pdf", content)
    stack.folders.ensure_structure()  # the Archive folders exist already, so the only thing left to refuse is the move itself
    stack.fake.files[created.obj.id]["driveId"] = OTHER
    before = snapshot(stack)
    start = len(stack.fake.requests)
    with pytest.raises(DriveForeignObject):  # 3A.1.2: refused up front (not served/archived/restored), before any write is even attempted
        stack.storage.archive(created.location, KIND)
    assert snapshot(stack) == before and writes(stack, start) == []
    with pytest.raises(DriveTargetError):  # and the guard itself still refuses the same move when it is attempted directly on the client
        stack.client.update_file(created.obj.id, add_parents=stack.folders.archive(KIND), remove_parents=stack.fake.files[created.obj.id]["parents"][0])
    assert snapshot(stack) == before and writes(stack, start) == []


def test_a_move_into_a_folder_of_another_drive_is_refused(tmp_path):
    stack, guard = guarded(tmp_path)
    content = pdf_bytes("target")
    created = stack.storage.upload(KIND, sha(content), "t.pdf", content)
    archive_folder = stack.folders.archive(KIND)
    stack.fake.files[archive_folder]["driveId"] = OTHER
    before = snapshot(stack)
    with pytest.raises(DriveTargetError):
        stack.storage.archive(created.location, KIND)
    assert snapshot(stack) == before


def test_a_write_that_lands_in_another_drive_stops_all_further_writes(tmp_path):
    stack, guard = guarded(tmp_path)
    guard.assert_ready()
    stack.fake.drive_id = OTHER  # from now on Drive stamps new items with a different drive
    with pytest.raises(DriveTargetError) as caught:
        stack.client.create_folder("Misplaced", stack.config.root_folder_id)
    assert caught.value.reason == "write_outside_drive" and not guard.verified
    start = len(stack.fake.requests)
    stack.fake.drive_id = SHARED
    stack.client.create_folder("Fine again", stack.config.root_folder_id)  # verifies again, then proceeds
    assert guard.verified and len(writes(stack, start)) == 1


# ====================================================================================================================== every write path


def unverified_destination(tmp_path):
    """A destination holding real objects (created with a plain client), then a GUARDED view of the same Drive whose guard can never verify it
    (it expects another drive). Each write path below has something to act on."""
    plain, _ = guarded(tmp_path, install=False)
    a, b = pdf_bytes("a"), pdf_bytes("b")
    first = plain.storage.upload(KIND, sha(a), "a.pdf", a)  # stays active
    second = plain.storage.upload(KIND, sha(b), "b.pdf", b)
    plain.storage.archive(second.location, KIND)  # archived: the restore path has something to restore
    plain.folders.ensure_structure()
    stack = guarded_view(plain, OTHER)
    return stack, stack.client.write_guard, first, second


WRITE_PATHS = {
    "create a folder": lambda s, a, b: s.client.create_folder("New Folder", s.config.root_folder_id),
    "create a structural folder": lambda s, a, b: s.folders._ensure("Brand New", s.config.root_folder_id, "role"),
    "upload (client)": lambda s, a, b: s.client.upload_file(file_id=s.client.generate_ids(1)[0], name="u.pdf", parent_id=s.folders.active(KIND), content=b"%PDF-1.4"),
    "upload (adapter)": lambda s, a, b: s.storage.upload(KIND, sha(pdf_bytes("c")), "c.pdf", pdf_bytes("c")),
    "save (interface)": lambda s, a, b: s.storage.save(KIND, sha(pdf_bytes("d")), "d.pdf", pdf_bytes("d")),
    "rename": lambda s, a, b: s.client.update_file(a.obj.id, name="renamed.pdf"),
    "move": lambda s, a, b: s.client.update_file(a.obj.id, add_parents=s.folders.archive(KIND), remove_parents=s.folders.active(KIND)),
    "trash": lambda s, a, b: s.client.update_file(a.obj.id, trashed=True),
    "archive": lambda s, a, b: s.storage.archive(a.location, KIND),
    "restore": lambda s, a, b: s.storage.restore(b.location, KIND),
    "delete (archives)": lambda s, a, b: s.storage.delete(a.location),
    "quarantine": lambda s, a, b: s.storage._quarantine(a.obj),
}


# Quarantine is a best-effort follow-up to an integrity failure: the adapter deliberately swallows ITS errors so the integrity error stays the one the
# caller sees. The refusal still happens before any request, and the guard still records it.
SWALLOWED = {"quarantine"}


@pytest.mark.parametrize("path", list(WRITE_PATHS))
def test_every_write_path_refuses_an_unverified_destination_before_any_request_is_sent(tmp_path, path):
    stack, guard, first, second = unverified_destination(tmp_path)
    before, start = snapshot(stack), len(stack.fake.requests)
    if path in SWALLOWED:
        WRITE_PATHS[path](stack, first, second)
    else:
        with pytest.raises(DriveTargetError):
            WRITE_PATHS[path](stack, first, second)
    assert guard.last_refusal == "wrong_drive" and not guard.verified
    assert writes(stack, start) == [], "a write reached Drive"
    assert snapshot(stack) == before


def test_the_manager_refuses_store_archive_and_restore_and_leaves_the_records_alone(tmp_path, db):
    plain, _ = guarded(tmp_path, install=False)
    stored = plain.manager.store(db, kind=KIND, content=pdf_bytes("m1"), original_filename="m1.pdf").record
    archived = plain.manager.store(db, kind=KIND, content=pdf_bytes("m2"), original_filename="m2.pdf").record
    plain.manager.archive(db, archived.id, user_id=None)
    stack = guarded_view(plain, OTHER)  # a guarded manager over the same Drive whose guard can never verify it
    before, start = snapshot(stack), len(stack.fake.requests)

    with pytest.raises(StorageUploadError) as upload:
        stack.manager.store(db, kind=KIND, content=pdf_bytes("m3"), original_filename="m3.pdf")
    assert upload.value.retryable and upload.value.record.state == sr.UPLOADING  # recoverable once the destination is fixed, never marked failed
    with pytest.raises(DriveTargetError):
        stack.manager.archive(db, stored.id, user_id=None)
    with pytest.raises(DriveTargetError):
        stack.manager.restore(db, archived.id, user_id=None)
    db.refresh(stored), db.refresh(archived)
    assert stored.state == sr.STORED and archived.state == sr.ARCHIVED
    assert writes(stack, start) == [] and snapshot(stack) == before


# ====================================================================================================================== reconcile and sync never create folders


def test_reconcile_and_sync_create_no_folder_and_send_no_write(tmp_path, db):
    stack, guard = guarded(tmp_path)
    before, start = snapshot(stack), len(stack.fake.requests)
    stack.manager.reconcile(db)
    stack.manager.sync_changes(db)  # baseline
    stack.manager.sync_changes(db)  # a real pass
    stack.manager.reconcile(db)
    assert snapshot(stack) == before and writes(stack, start) == []


def test_resolve_structure_reports_missing_folders_without_creating_them(tmp_path):
    stack, _ = guarded(tmp_path)
    start = len(stack.fake.requests)
    structure = stack.folders.resolve_structure()
    assert structure["archive"] is None and structure["quarantine"] is None and structure[f"active:{KIND}"] is None and writes(stack, start) == []
    with pytest.raises(DriveFolderMissing):
        stack.folders.active(KIND, create=False)
    assert writes(stack, start) == []
    created = stack.folders.ensure_structure()  # creating is explicit
    assert stack.folders.resolve_structure()[f"active:{KIND}"] == created[f"active:{KIND}"]


def test_reconcile_of_a_record_whose_file_sits_outside_the_structure_creates_nothing(tmp_path, db):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("loose")
    loose = stack.client.upload_file(file_id=stack.client.generate_ids(1)[0], name="loose.pdf", parent_id=stack.config.root_folder_id, content=content, app_properties={"sha256": sha(content), "kind": KIND})
    db.add(DocumentStorageRecord(kind=KIND, sha256=sha(content), original_filename="loose.pdf", size=len(content), md5=hashlib.md5(content).hexdigest(), location=make_location(loose.id), state=sr.STORED))
    db.commit()
    before, start = snapshot(stack), len(stack.fake.requests)
    report = stack.manager.reconcile(db)
    assert report.checked == 1 and report.moved_externally == 1  # noticed and reported, not "fixed" by building folders
    assert snapshot(stack) == before and writes(stack, start) == []


def test_a_pending_upload_waits_when_its_folder_is_missing_and_completes_after_an_explicit_initialisation(tmp_path, db):
    stack, _ = guarded(tmp_path)
    content = pdf_bytes("pending")
    record = DocumentStorageRecord(
        kind=KIND, sha256=sha(content), original_filename="p.pdf", size=len(content), md5=hashlib.md5(content).hexdigest(),
        location=make_location(stack.storage.reserve_id()), state=sr.UPLOADING, created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(record)
    db.commit()
    stack.manager._spool(record, content)
    before, start = snapshot(stack), len(stack.fake.requests)

    report = stack.manager.reconcile(db)
    db.refresh(record)
    # waiting visibly, nothing lost. 3A.2-B: a missing folder is a CONFIGURATION deferral - it used to cost one retry attempt (attempts == 1), now it costs none.
    assert record.state == sr.UPLOADING and record.attempts == 0 and record.deferrals == 1 and record.failure_class == "configuration" and "DriveFolderMissing" in (record.last_error or "")
    assert report.reuploaded == 0 and report.failed_uploads == 0 and any("destination folder does not exist" in e for e in report.errors)
    assert snapshot(stack) == before and writes(stack, start) == []  # reconcile created no folder and uploaded nothing

    stack.manager.initialise_structure()  # the explicit, guarded way to create the structure
    report = stack.manager.reconcile(db)
    db.refresh(record)
    assert record.state == sr.UPLOADING and report.reuploaded == 0  # 3A.1.2: the deferred attempt is still inside its schedule, so reconcile leaves it alone
    record.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)  # ... until its schedule has passed (3A.2-B: the record's own next_attempt_at)
    db.commit()
    report = stack.manager.reconcile(db)
    db.refresh(record)
    assert record.state == sr.STORED and report.reuploaded == 1 and stack.fake.file_count() == 1


# ====================================================================================================================== the application's runtime always installs the guard


def runtime_stack(tmp_path, *, expected=SHARED):
    stack, _ = guarded(tmp_path, install=False)
    env = {
        "SPINCO_GDRIVE_ROOT_FOLDER_ID": stack.config.root_folder_id,
        "SPINCO_GDRIVE_CREDENTIALS_FILE": str(stack.credentials_path),
        "SPINCO_ENVIRONMENT": "staging",
        "SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING": expected,
    }
    return stack, env


def test_build_manager_installs_the_guard_and_writes_only_to_the_expected_drive(tmp_path):
    stack, env = runtime_stack(tmp_path)
    manager = build_manager(env, http=stack.http)
    guard = manager.drive.client.write_guard
    assert isinstance(guard, DriveTargetGuard) and not guard.verified  # nothing is trusted until Google confirms it
    content = pdf_bytes("runtime")
    outcome = manager.drive.upload(KIND, sha(content), "r.pdf", content)
    assert guard.verified and outcome.obj.drive_id == SHARED


def test_a_manager_built_for_the_wrong_drive_cannot_write(tmp_path):
    stack, env = runtime_stack(tmp_path, expected=OTHER)
    manager = build_manager(env, http=stack.http)
    start = len(stack.fake.requests)
    assert manager.drive.client.write_guard.try_verify() is False
    content = pdf_bytes("wrong")
    with pytest.raises(DriveTargetError):
        manager.drive.upload(KIND, sha(content), "w.pdf", content)
    assert writes(stack, start) == []


def test_build_manager_refuses_to_start_without_the_destination_settings(tmp_path):
    stack, env = runtime_stack(tmp_path)
    for name in ("SPINCO_ENVIRONMENT", "SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING"):
        broken = {k: v for k, v in env.items() if k != name}
        with pytest.raises(DriveConfigError, match=name):
            build_manager(broken, http=stack.http)


def test_the_dependency_reports_a_generic_503_when_the_destination_settings_are_missing(tmp_path, monkeypatch):
    stack, env = runtime_stack(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING")
    monkeypatch.setenv("SPINCO_GDRIVE_ENABLED", "true")
    runtime.reset_cache()
    try:
        with pytest.raises(HTTPException) as caught:
            runtime.get_storage_manager()
    finally:
        runtime.reset_cache()
    assert caught.value.status_code == 503 and "not configured" in caught.value.detail and SHARED not in caught.value.detail


def test_the_runtime_is_the_only_place_in_the_application_that_builds_a_drive_client():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    builders = [p.relative_to(app_dir).as_posix() for p in app_dir.rglob("*.py") if "DriveClient(" in p.read_text(encoding="utf-8") and p.name != "client.py"]
    assert builders == ["services/drive/runtime.py"], builders
    source = (app_dir / "services" / "drive" / "runtime.py").read_text(encoding="utf-8")
    code = "\n".join(line.split("#")[0] for line in source.splitlines())  # comments may mention the opt-out; code must not use it
    assert not re.search(r"unguarded\s*=\s*True", code), "the application runtime must never opt out of the destination guard"
    assert "client.write_guard is None" in source and source.index("client.write_guard is None") < source.index("GoogleDriveStorage(")


def test_every_write_verb_in_the_client_goes_through_the_one_guarded_choke_point():
    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "drive" / "client.py").read_text(encoding="utf-8")
    assert source.count("self._http.send(") == 1  # the only place a request leaves the client
    assert "is_write = method.upper() not in _READ_METHODS" in source and "self._write_guard.assert_ready()" in source
    assert 'self._request("POST"' in source and 'self._request("PATCH"' in source  # the write methods exist and use that same _request


@pytest.mark.parametrize("verb", ["POST", "PUT", "PATCH", "DELETE", "delete", "Patch", "MKCOL", "COPY"])
def test_the_choke_point_refuses_any_non_read_verb_before_it_leaves_the_client(tmp_path, verb):
    """A verb that no current method uses (or one added tomorrow) cannot slip past: anything that is not GET/HEAD needs a verified destination."""
    stack, guard = guarded(tmp_path, expected=OTHER)
    start = len(stack.fake.requests)
    with pytest.raises(DriveTargetError):
        stack.client._request(verb, "https://www.googleapis.com/drive/v3/files/whatever", json_body={"name": "x"})
    assert writes(stack, start) == [] and [r for r in stack.fake.requests[start:] if r["method"] not in ("GET",)] == [r for r in stack.fake.requests[start:] if r["path"] == "/token"]


def test_reads_are_never_blocked_by_the_write_guard(tmp_path):
    stack, guard = guarded(tmp_path, expected=OTHER)  # a destination that can never be verified
    root = stack.config.root_folder_id
    assert stack.client.get_raw(root, "id")["id"] == root  # viewing existing documents keeps working while writes are refused
    assert stack.client.get_about() and not guard.verified