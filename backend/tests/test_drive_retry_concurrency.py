"""Phase 3A.2-B: the lease and the compare-and-swap statements under real concurrency.

Part A races THREADS (each with its own database session) on the test database. Part B races separate OS PROCESSES - what several API workers really are -
on a disposable SQLite file built and migrated by Alembic (the real migration, not create_all), reached only through tests/migration_harness.py.
No Drive and no network: the statements under test are the database's compare-and-swap, which is what makes several processes safe."""

import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import migration_harness as h
from app.database import Base, SessionLocal, engine
from app.models import storage_record as sr
from app.models.logs import AuditLog
from app.models.storage_record import DocumentStorageRecord
from app.models.user import User
from app.services import audit
from app.services.drive.client import DriveTransientError
from app.services.drive.lifecycle import StorageManager, StorageStateError, StorageUploadError
from app.services.drive.storage import make_location, parse_location

from drive_testkit import pdf_bytes
from test_drive_retry_budget import KIND, make, pending, raising, sha

THREADS = 8


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def race(workers: int, body):
    """Runs body(i) in `workers` threads released at the same instant; returns their results (or the exception each raised)."""
    barrier, results = threading.Barrier(workers), [None] * workers

    def run(i):
        try:
            barrier.wait(timeout=30)
            results[i] = body(i)
        except Exception as exc:  # noqa: BLE001 - the exception IS the result being examined
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return results


def bare_record(**fields):
    content = pdf_bytes("race")
    values = dict(
        kind=KIND, sha256=sha(content), original_filename="race.pdf", size=len(content), md5="0" * 32, location="gdrive:RACEFILEID0000001",
        state=sr.UPLOADING, created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    values.update(fields)
    return DocumentStorageRecord(**values)


# ======================================================================================================================== A: threads


def test_only_one_of_many_simultaneous_claims_wins(db):
    record = bare_record()
    db.add(record)
    db.commit()
    record_id = record.id  # plain values only cross into the threads

    def claim(_):
        with SessionLocal() as session:
            return StorageManager(None)._claim(session, record_id, manual=False)

    leases = race(THREADS, claim)
    winners = [lease for lease in leases if lease is not None and not isinstance(lease, Exception)]
    assert [x for x in leases if isinstance(x, Exception)] == [] and len(winners) == 1
    db.refresh(record)
    assert record.attempts == 1 and record.lease_owner == winners[0].owner  # charged exactly once, held by exactly one


def test_only_one_of_many_simultaneous_manual_retries_resets_a_failed_record(db):
    record = bare_record(state=sr.FAILED, attempts=10, failure_class="transient", deferrals=3)
    db.add(record)
    db.commit()
    record_id = record.id

    def reset(_):
        with SessionLocal() as session:
            return StorageManager(None)._reset_failed(session, record_id)

    results = race(THREADS, reset)
    assert results.count(True) == THREADS - results.count(False) == 1 and not [x for x in results if isinstance(x, Exception)]
    db.refresh(record)
    assert (record.state, record.attempts, record.deferrals, record.failure_class) == (sr.UPLOADING, 0, 0, None)


def test_simultaneous_manual_retry_requests_are_accepted_once_audited_once_and_upload_once(tmp_path, db):
    stack, clock = make(tmp_path, limit=2)
    admin = User(email="race.admin@example.com", name="Race Admin", role="ADMIN", password_hash=None, is_active=True)
    db.add(admin)
    db.commit()
    admin_id = admin.id  # plain values only cross into the threads: an ORM object belongs to this thread's session
    calls = raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    with pytest.raises(StorageUploadError):
        stack.manager.store(db, kind=KIND, content=pdf_bytes("dup"), original_filename="dup.pdf")
    record = db.execute(select(DocumentStorageRecord)).scalars().one()
    record_id = record.id
    while record.state != sr.FAILED:
        clock.advance(4000)
        stack.manager.reconcile(db, stale_after_seconds=0)
        db.refresh(record)
    del stack.storage.upload  # Google is back
    uploads = []
    real_upload = stack.storage.upload
    stack.storage.upload = lambda *a, **k: uploads.append(1) or real_upload(*a, **k)

    def retry(_):
        with SessionLocal() as session:
            return stack.manager.retry_upload(session, record_id, user_id=admin_id)

    results = race(THREADS, retry)
    accepted = [r for r in results if isinstance(r, DocumentStorageRecord)]
    refused = [r for r in results if isinstance(r, StorageStateError)]
    others = [r for r in results if not isinstance(r, (DocumentStorageRecord, StorageStateError))]
    assert len(accepted) == 1 and len(refused) == THREADS - 1 and not others, [repr(r)[:300] for r in others]
    assert uploads == [1] and stack.fake.file_count() == 1  # one request did the work; no duplicate file
    db.expire_all()
    assert db.get(DocumentStorageRecord, record.id).state == sr.STORED
    rows = db.execute(select(AuditLog).where(AuditLog.entity_id == str(record_id))).scalars().all()
    assert [a.action for a in rows].count(audit.STORAGE_RETRY_ACCEPTED) == 1 and [a.action for a in rows].count(audit.STORAGE_RETRY_REJECTED) == THREADS - 1
    assert {a.user_id for a in rows} == {admin_id}  # every request, honoured or not, is attributed
    assert len(calls) >= 2


def test_simultaneous_reconciles_attempt_each_due_upload_once(tmp_path, db):
    stack, clock = make(tmp_path)
    records = [pending(db, stack, f"r{i}")[0] for i in range(3)]
    attempts = []

    def slow_failure(*args, **kwargs):
        attempts.append(kwargs["reserved_id"])
        time.sleep(0.3)  # keeps the lease held long enough for every other reconciler to run into it
        raise DriveTransientError("503", status=503, retryable=True)

    stack.storage.upload = slow_failure

    def reconcile(_):
        with SessionLocal() as session:
            return stack.manager.reconcile(session, stale_after_seconds=0)

    results = race(4, reconcile)
    assert not [r for r in results if isinstance(r, Exception)]
    assert sorted(attempts) == sorted(parse_location(r.location) for r in records)  # each record was uploaded by exactly one of the four reconcilers
    db.expire_all()
    assert [db.get(DocumentStorageRecord, r.id).attempts for r in records] == [1, 1, 1]


# ============================================================================================================ B: separate OS processes

WORKER = """
import sys, time
from datetime import datetime, timedelta, timezone
from app.database import SessionLocal
from app.models.storage_record import DocumentStorageRecord
from app.services.drive.lifecycle import StorageManager, _Lease

mode, start, record_id, offset = sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
manager = StorageManager(None, now=lambda: datetime.now(timezone.utc) + timedelta(seconds=offset), max_upload_attempts=10)
while time.time() < start:
    pass
db = SessionLocal()
if mode == "claim":
    lease = manager._claim(db, record_id, manual=False)
    print("WIN " + lease.owner if lease else "LOSE")
elif mode == "reset":
    print("WIN" if manager._reset_failed(db, record_id) else "LOSE")
elif mode == "stale":
    record = db.get(DocumentStorageRecord, record_id)
    print("WIN" if manager._record_success(db, _Lease(record_id, sys.argv[5]), record, "STORED") else "LOSE")
"""

requires_root = pytest.mark.skipif(not h.root_available(), reason="the approved disposable-database folder is not available on this machine")


@pytest.fixture(scope="module")
def migrated_template():
    """A disposable database built by the REAL migrations (baseline, hardening, then c3a9f0b17d42), copied for each test."""
    run = h.new_run_dir("template")
    path = run / "migrated.db"
    h.build_base_database(path)
    migrated = h.upgrade_to_new(path)
    assert migrated.returncode == 0, h.output(migrated)
    yield path
    h.remove_run_dir(run)


@pytest.fixture()
def disposable(migrated_template):
    run = h.new_run_dir()
    target = run / "race.db"
    shutil.copy2(migrated_template, target)
    yield target
    h.remove_run_dir(run)


def insert_record(path, **overrides):
    columns = dict(kind=KIND, sha256="a" * 64, original_filename="p.pdf", size=1, md5="0" * 32, location=make_location("PROCESSFILEID00001"), state="UPLOADING",
                   attempts=0, created_at="2026-11-01 10:00:00", updated_at="2026-11-01 10:00:00")
    columns.update(overrides)
    with h.connect(str(h.assert_disposable(path))) as con:
        con.execute(f"insert into document_storage ({','.join(columns)}) values ({','.join('?' * len(columns))})", tuple(columns.values()))
        return con.execute("select max(id) from document_storage").fetchone()[0]


def row(path, record_id):
    with h.connect(str(h.assert_disposable(path))) as con:
        con.row_factory = sqlite3.Row
        return dict(con.execute("select * from document_storage where id = ?", (record_id,)).fetchone())


def run_processes(path, mode, record_id, *, count=8, offset=0.0, extra=()):
    start = time.time() + 4.0  # every process imports the application first, then waits for this instant
    processes = [h.spawn_python(path, WORKER, mode, repr(start), str(record_id), repr(offset), *extra) for _ in range(count)]
    outputs = []
    for p in processes:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err[-500:]
        outputs.append(out.strip())
    return outputs


@requires_root
def test_eight_processes_claim_one_record_exactly_one_wins(disposable):
    record_id = insert_record(disposable)
    results = run_processes(disposable, "claim", record_id)
    winners = [r for r in results if r.startswith("WIN")]
    assert len(winners) == 1 and results.count("LOSE") == 7
    state = row(disposable, record_id)
    assert state["attempts"] == 1 and state["lease_owner"] == winners[0].split()[1] and state["lease_expires_at"] is not None
    assert h.integrity(disposable) == ("ok", [])


@requires_root
def test_after_a_crash_the_expired_lease_is_taken_over_by_exactly_one_process_and_the_stale_owner_is_refused(disposable):
    record_id = insert_record(disposable)
    first = run_processes(disposable, "claim", record_id)
    old_owner = next(r for r in first if r.startswith("WIN")).split()[1]
    assert run_processes(disposable, "claim", record_id, count=4).count("LOSE") == 4  # the first worker's lease is still valid for the next 30 minutes
    second = run_processes(disposable, "claim", record_id, offset=1801.0)  # 30 minutes later: the first worker is presumed dead
    winners = [r for r in second if r.startswith("WIN")]
    assert len(winners) == 1
    state = row(disposable, record_id)
    new_owner = winners[0].split()[1]
    assert new_owner != old_owner and state["lease_owner"] == new_owner and state["attempts"] == 2  # the crashed attempt stayed charged
    late = run_processes(disposable, "stale", record_id, count=2, extra=(old_owner,))  # the presumed-dead worker wakes up and tries to report success
    assert late == ["LOSE", "LOSE"]
    after = row(disposable, record_id)
    assert after["state"] == "UPLOADING" and after["lease_owner"] == new_owner and after["attempts"] == 2 and after["stored_at"] is None  # nothing it said was recorded
    with h.connect(str(h.assert_disposable(disposable))) as con:
        assert con.execute("select count(*) from storage_events where record_id = ? and event_type = 'UPLOAD_LEASE_LOST'", (record_id,)).fetchone()[0] == 2


@requires_root
def test_eight_processes_retry_one_failed_record_exactly_one_is_accepted(disposable):
    record_id = insert_record(disposable, state="FAILED", attempts=10, deferrals=2, failure_class="transient", last_error="Upload abandoned after 10 failed attempts")
    results = run_processes(disposable, "reset", record_id)
    assert results.count("WIN") == 1 and results.count("LOSE") == 7
    state = row(disposable, record_id)
    assert (state["state"], state["attempts"], state["deferrals"], state["failure_class"], state["lease_owner"]) == ("UPLOADING", 0, 0, None, None)
    assert state["next_attempt_at"] is not None and state["last_error"].startswith("Upload abandoned")  # due now; the history is not erased
    assert h.integrity(disposable) == ("ok", [])


def test_competing_stores_of_one_new_file_report_exactly_one_creation_and_it_is_a_stored_one(tmp_path, db):
    """Review F5: `created=True` means THIS call created the record and its upload completed. Six simultaneous stores of the same new bytes: one Drive
    file, one record, STORED, and `created` is True for at most one caller - never for a caller whose claim was lost."""
    stack, clock = make(tmp_path)
    content = pdf_bytes("compete")

    def store(_):
        with SessionLocal() as session:
            result = stack.manager.store(session, kind=KIND, content=content, original_filename="c.pdf")
            return result.created, result.duplicate, result.record.state

    results = race(6, store)
    assert not [r for r in results if isinstance(r, Exception)], results
    created = [r for r in results if r[0]]
    assert len(created) == 1 and all(r[1] for r in results if not r[0])  # Step 0 / N2: EXACTLY one caller created it; nobody else is told they did
    assert created[0][2] == sr.STORED  # and the one who is told so really did upload and persist it
    assert not list((tmp_path / "spool").glob("*"))  # no retry copy left behind by the winner or by any loser
    db.expire_all()
    rows = db.execute(select(DocumentStorageRecord)).scalars().all()
    assert len(rows) == 1 and rows[0].state == sr.STORED and stack.fake.file_count() == 1 and rows[0].lease_owner is None


def test_competing_stores_while_drive_fails_never_report_a_creation(tmp_path, db):
    stack, clock = make(tmp_path)
    calls = raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    content = pdf_bytes("compete-fail")

    def store(_):
        with SessionLocal() as session:
            result = stack.manager.store(session, kind=KIND, content=content, original_filename="c.pdf")
            return ("returned", result.created, result.record.state)

    results = race(6, store)
    returned = [r for r in results if isinstance(r, tuple)]
    errors = [r for r in results if isinstance(r, StorageUploadError)]
    assert len(returned) + len(errors) == 6, results  # every caller either got a duplicate/pending answer or learned the upload failed
    assert not any(r[1] for r in returned)  # nobody was told "created" for an upload that did not complete
    db.expire_all()
    record = db.execute(select(DocumentStorageRecord)).scalars().one()
    assert record.state == sr.UPLOADING and 1 <= len(calls) <= 2 and record.lease_owner is None

PROCESS_STORE = """
import os, sys, time
from pathlib import Path
from types import SimpleNamespace

from app.database import SessionLocal
from app.services.drive.lifecycle import StorageManager
from app.services.drive.storage import make_location

start, spool, marker = float(sys.argv[1]), sys.argv[2], sys.argv[3]


class StubDrive:  # a stand-in for the Drive adapter (each process has its own memory, so a shared fake Drive is not possible); synthetic only
    def reserve_id(self):
        return "PID%d%s" % (os.getpid(), os.urandom(6).hex())  # a syntactically valid, unique file id

    def upload(self, kind, file_hash, original_filename, content, *, reserved_id=None, create_folders=True):
        with open(marker, "a") as handle:  # one line per REAL upload, across all processes
            handle.write("%d\\n" % os.getpid())
        time.sleep(2.0)  # long enough that every other process arrives while this one holds the lease
        return SimpleNamespace(location=make_location(reserved_id), obj=SimpleNamespace(id=reserved_id), adopted=False, deduplicated=False)


manager = StorageManager(StubDrive(), spool_dir=Path(spool))
content = b"%PDF-1.4 synthetic identical bytes for the process race\\n" * 20
while time.time() < start:
    pass
session = SessionLocal()
try:
    result = manager.store(session, kind="expense_report", content=content, original_filename="same.pdf")
    print("CREATED" if result.created else "DUP " + result.record.state)
except Exception as exc:
    print("ERR " + type(exc).__name__)
"""


@requires_root
def test_separate_processes_storing_identical_bytes_create_the_record_exactly_once(disposable):
    """Six OS processes, one migrated disposable SQLite database (the real migration), the same bytes at the same instant: exactly ONE process creates the
    record and uploads, the others get the in-progress answer, one record results and no retry copy is left behind."""
    run_dir = disposable.parent
    spool, marker = run_dir / "spool", run_dir / "uploads.log"
    start = time.time() + 4.0  # every process imports the application first, then waits for this instant
    processes = [h.spawn_python(disposable, PROCESS_STORE, repr(start), str(spool), str(marker)) for _ in range(6)]
    outputs = []
    for process in processes:
        out, err = process.communicate(timeout=180)
        assert process.returncode == 0, err[-400:]
        outputs.append(out.strip())
    assert outputs.count("CREATED") == 1, outputs  # exactly one creator
    assert all(o in ("DUP UPLOADING", "DUP STORED") for o in outputs if o != "CREATED"), outputs  # everybody else: an existing / in-progress record, never "created"
    assert len(marker.read_text().split()) == 1  # ONE upload in total (not one per process)
    with h.connect(str(h.assert_disposable(disposable))) as con:
        rows = con.execute("select state, attempts, lease_owner, lease_expires_at from document_storage").fetchall()
        assert len(rows) == 1 and rows[0] == ("STORED", 0, None, None), rows  # one record, finished, lease released, claim charge refunded
        assert con.execute("select count(*) from storage_events where event_type = 'UPLOAD_STARTED'").fetchone()[0] == 1
    assert not spool.exists() or list(spool.glob("*")) == []  # no abandoned or duplicate retry copy after the race
    assert h.integrity(disposable) == ("ok", [])