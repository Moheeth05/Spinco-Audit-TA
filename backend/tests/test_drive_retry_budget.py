"""Phase 3A.2-B: the retry budget, deferral tracking, schedule and per-record lease of pending uploads. FAKE-DRIVE tests of OUR accounting (the Drive failures
are injected into the adapter's upload call); they say nothing about how Google really behaves.

The six rules under test (see app/services/drive/failures.py):
  1 a genuine transient failure consumes the retry budget      4 a permission / quota refusal does not
  2 an authentication deferral does not                         5 a manual administrator retry is audited and resets / requeues per policy
  3 a guard / security / configuration refusal does not         6 a permanent failure is never retried automatically
plus: a revoked authorization parks the record (no budget, no lease, no automatic retry), and one worker owns one upload at a time."""

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import storage_record as sr
from app.models.logs import AuditLog
from app.models.storage_record import DocumentStorageRecord, StorageEvent
from app.models.user import User
from app.services import audit
from app.services.drive import failures as fl
from app.services.drive import lifecycle
from app.services.drive import guard as guard_module
from app.services.drive.auth import DriveAuthError, DriveAuthPermanentError, DriveAuthTransientError
from app.services.drive.client import DriveError, DriveIntegrityError, DriveNotFound, DrivePermissionError, DriveQuotaError, DriveTransientError
from app.services.drive.errors import DriveConflict, DriveDestinationMissing, DriveIdentityMismatch, DriveIncompleteListing
from app.services.drive.guard import DriveTargetError
from app.services.drive.lifecycle import UPLOAD_LEASE_SECONDS, StorageManager, StorageStateError, StorageUploadError, _Lease
from app.services.drive.storage import DriveFolderMissing, make_location, parse_location

from drive_testkit import make_stack, pdf_bytes

KIND = "expense_report"


class Clock:
    """The storage manager's clock, under the test's control. It starts at the real time (the records' own timestamps are real)."""

    def __init__(self):
        self.t = datetime.now(timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)

    @property
    def naive(self):
        return self.t.replace(tzinfo=None)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def user(db):
    row = User(email="retry.admin@example.com", name="Retry Admin", role="ADMIN", password_hash=None, is_active=True)
    db.add(row)
    db.commit()
    return row


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def make(tmp_path, *, limit=10, **manager_options):
    clock = Clock()
    stack = make_stack(tmp_path, now=clock, max_upload_attempts=limit, manager_options={"rng": lambda: 0.5, **manager_options})  # rng 0.5: the jitter factor is exactly 1.0
    stack.manager.initialise_structure()  # reconcile and manual retries re-send bytes but never create folders, so the structure exists up front
    return stack, clock


def raising(stack, factory):
    """Every upload now fails with factory(); returns the list that records each call."""
    calls = []

    def upload(*args, **kwargs):
        calls.append(kwargs.get("reserved_id"))
        raise factory()

    stack.storage.upload = upload
    return calls


def repair(stack):
    del stack.storage.upload  # back to the real (fake-Drive) upload


def pending(db, stack, marker="p", **fields):
    """A pending upload as a crashed or earlier process would have left it: UPLOADING, id reserved, bytes spooled, older than any stale limit."""
    content = pdf_bytes(marker)
    values = dict(
        kind=KIND, sha256=sha(content), original_filename=f"{marker}.pdf", size=len(content), md5=hashlib.md5(content).hexdigest(),
        location=make_location(stack.storage.reserve_id()), state=sr.UPLOADING, created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    values.update(fields)
    record = DocumentStorageRecord(**values)
    db.add(record)
    db.commit()
    stack.manager._spool(record, content)
    return record, content


def events(db, record, event_type=None):
    query = select(StorageEvent).where(StorageEvent.record_id == record.id).order_by(StorageEvent.id)
    if event_type:
        query = query.where(StorageEvent.event_type == event_type)
    return db.execute(query).scalars().all()


def audits(db, action):
    return db.execute(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)).scalars().all()


def store_failing(stack, db, marker="s"):
    with pytest.raises(StorageUploadError) as caught:
        stack.manager.store(db, kind=KIND, content=pdf_bytes(marker), original_filename=f"{marker}.pdf")
    return caught.value


# ============================================================================================================================ classification (pure)

TARGET = DriveTargetError
CLASS_CASES = [
    ("transient", lambda: DriveTransientError("503", status=503, retryable=True), fl.TRANSIENT),
    ("incomplete listing", DriveIncompleteListing, fl.TRANSIENT),
    ("generic retryable", lambda: DriveError("odd", retryable=True), fl.TRANSIENT),
    ("auth transient", lambda: DriveAuthTransientError("token endpoint down"), fl.AUTH_DEFERRAL),
    ("guard: unverifiable", lambda: TARGET("unverifiable"), fl.OUTAGE),
    ("guard: not_configured", lambda: TARGET("not_configured"), fl.CONFIGURATION),
    ("guard: hierarchy", lambda: TARGET("hierarchy"), fl.CONFIGURATION),
    ("guard: destination_missing", lambda: TARGET("destination_missing"), fl.CONFIGURATION),
    ("guard: no_capability", lambda: TARGET("no_capability"), fl.CONFIGURATION),
    ("guard: destination_denied", lambda: TARGET("destination_denied"), fl.PERMISSION),
    ("guard: wrong_drive", lambda: TARGET("wrong_drive"), fl.SECURITY),
    ("guard: my_drive", lambda: TARGET("my_drive"), fl.SECURITY),
    ("guard: write_outside_drive", lambda: TARGET("write_outside_drive"), fl.SECURITY),
    ("folder missing", lambda: DriveFolderMissing("active:expense_report"), fl.CONFIGURATION),
    ("destination folder missing", DriveDestinationMissing, fl.CONFIGURATION),
    ("permission", lambda: DrivePermissionError("403", status=403), fl.PERMISSION),
    ("quota", lambda: DriveQuotaError("quota", status=403), fl.QUOTA),
    ("auth permanent", lambda: DriveAuthPermanentError("invalid_grant"), fl.AUTH_PERMANENT),
    ("auth, unrecognised kind", lambda: DriveAuthError("something else"), fl.AUTH_PERMANENT),
    ("not found", lambda: DriveNotFound("404", status=404), fl.PERMANENT),
    ("conflict", lambda: DriveConflict("409", status=409), fl.PERMANENT),
    ("generic, not retryable", lambda: DriveError("odd"), fl.PERMANENT),
    ("adoption rejected", lambda: TARGET("adopt_wrong_drive", retryable=False), fl.PERMANENT),
    ("integrity", lambda: DriveIntegrityError("md5 differs"), fl.INTEGRITY),
    ("identity mismatch", DriveIdentityMismatch, fl.INTEGRITY),
    ("unexpected, not a Drive error at all", lambda: RuntimeError("bug"), fl.TRANSIENT),
]


@pytest.mark.parametrize(("label", "factory", "expected"), CLASS_CASES, ids=[c[0] for c in CLASS_CASES])
def test_every_failure_has_exactly_one_class(label, factory, expected):
    assert fl.classify(factory()) == expected


def test_every_destination_guard_reason_code_is_classified_and_nothing_extra_is():
    assert set(fl.GUARD_REASON_CLASS) == set(guard_module._MESSAGES)  # a new reason cannot silently fall to a default
    assert set(fl.GUARD_REASON_CLASS.values()) <= set(fl.ALL_CLASSES)
    for reason in guard_module._MESSAGES:  # and each one classifies the way its table entry says (adoption refusals are not retryable by construction)
        exc = TARGET(reason, retryable=not reason.startswith("adopt_"))
        assert fl.classify(exc) == fl.GUARD_REASON_CLASS[reason]


def test_the_policy_table_is_complete_and_consistent():
    assert set(fl.POLICIES) == set(fl.ALL_CLASSES)
    for name, policy in fl.POLICIES.items():
        if policy.deferral or policy.parked:
            assert not policy.counts_budget and not policy.terminal, name  # a deferral or a parked record never costs budget and never ends in FAILED by itself
        if policy.terminal:
            assert policy.counts_budget and policy.deferral is None and not policy.parked, name
        assert not (policy.parked and policy.deferral), name
    assert {n for n, p in fl.POLICIES.items() if p.counts_budget} == {fl.TRANSIENT, fl.PERMANENT, fl.INTEGRITY}  # only these three ever consume the budget
    assert {n for n, p in fl.POLICIES.items() if p.terminal} == {fl.PERMANENT, fl.INTEGRITY}
    assert {n for n, p in fl.POLICIES.items() if p.parked} == {fl.AUTH_PERMANENT}


def test_backoff_doubles_up_to_its_cap_and_never_applies_to_terminal_or_parked_classes():
    kwargs = dict(attempts=1, deferrals=1, transient_base=60.0, transient_cap=3600.0)
    assert [fl.backoff_seconds(fl.TRANSIENT, **{**kwargs, "attempts": n}) for n in (1, 2, 3, 7, 8, 40)] == [60, 120, 240, 3600, 3600, 3600]
    assert [fl.backoff_seconds(fl.AUTH_DEFERRAL, **{**kwargs, "deferrals": n}) for n in (1, 2, 3, 4, 5, 99)] == [60, 120, 240, 480, 900, 900]  # quick problems: looked at quickly
    assert [fl.backoff_seconds(fl.SECURITY, **{**kwargs, "deferrals": n}) for n in (1, 2, 3, 9)] == [900, 1800, 3600, 3600]  # problems a person must fix: looked at rarely
    assert fl.backoff_seconds(fl.PERMANENT, **kwargs) is None and fl.backoff_seconds(fl.INTEGRITY, **kwargs) is None and fl.backoff_seconds(fl.AUTH_PERMANENT, **kwargs) is None
    assert fl.backoff_seconds(fl.TRANSIENT, **{**kwargs, "transient_base": 0.0}) == 0.0


def test_the_jitter_stays_within_ten_percent_both_ways():
    assert fl.jittered(60.0, lambda: 0.0) == pytest.approx(54.0) and fl.jittered(60.0, lambda: 0.5) == pytest.approx(60.0)
    assert 65.9 < fl.jittered(60.0, lambda: 0.999999) <= 66.0 and fl.jittered(0.0, lambda: 0.3) == 0.0


def test_needs_attention_is_read_only_and_says_why():
    now = datetime(2026, 11, 5, 12, 0, 0)

    def row(**kw):
        return type("R", (), {"state": "UPLOADING", "failure_class": None, "deferred_since": None, **kw})()

    assert fl.needs_attention(row(), now) is None
    assert fl.needs_attention(row(state="FAILED"), now) == "failed"
    assert fl.needs_attention(row(state="STORED"), now) is None
    assert fl.needs_attention(row(failure_class=fl.AUTH_PERMANENT), now) == "authorization_needed"
    assert fl.needs_attention(row(failure_class=fl.OUTAGE, deferred_since=now - timedelta(hours=23)), now) is None
    assert fl.needs_attention(row(failure_class=fl.OUTAGE, deferred_since=now - timedelta(hours=25)), now) == "deferred_outage"


def test_the_lease_setting_is_fixed_and_validated():
    assert UPLOAD_LEASE_SECONDS == 1800.0  # decision U-6: a fixed 30 minutes
    with pytest.raises(ValueError):
        StorageManager(None, lease_seconds=0)


# ================================================================================================================ the six rules, through the manager

BUDGET, DEFERRAL, PARKED, TERMINAL = "budget", "deferral", "parked", "terminal"
FAST, SLOW = 60.0, 900.0
ACCOUNTING = [  # label, failure, class, rule, first wait (seconds) for a scheduled kind
    ("1 transient", lambda: DriveTransientError("503", status=503, retryable=True), fl.TRANSIENT, BUDGET, 60.0),
    ("1 incomplete listing", DriveIncompleteListing, fl.TRANSIENT, BUDGET, 60.0),
    ("2 auth deferral", lambda: DriveAuthTransientError("token endpoint down"), fl.AUTH_DEFERRAL, DEFERRAL, FAST),
    ("3 guard outage", lambda: TARGET("unverifiable"), fl.OUTAGE, DEFERRAL, FAST),
    ("3 guard configuration", lambda: TARGET("hierarchy"), fl.CONFIGURATION, DEFERRAL, SLOW),
    ("3 folder missing", lambda: DriveFolderMissing("active:expense_report"), fl.CONFIGURATION, DEFERRAL, SLOW),
    ("3 guard security", lambda: TARGET("wrong_drive"), fl.SECURITY, DEFERRAL, SLOW),
    ("4 permission", lambda: DrivePermissionError("403", status=403), fl.PERMISSION, DEFERRAL, SLOW),
    ("4 quota", lambda: DriveQuotaError("quota", status=403), fl.QUOTA, DEFERRAL, SLOW),
    ("parked: revoked authorization", lambda: DriveAuthPermanentError("invalid_grant"), fl.AUTH_PERMANENT, PARKED, None),
    ("6 not found", lambda: DriveNotFound("404", status=404), fl.PERMANENT, TERMINAL, None),
    ("6 adoption rejected", lambda: TARGET("adopt_my_drive", retryable=False), fl.PERMANENT, TERMINAL, None),
    ("6 integrity", lambda: DriveIntegrityError("md5 differs"), fl.INTEGRITY, TERMINAL, None),
]


@pytest.mark.parametrize(("label", "factory", "cls", "rule", "wait"), ACCOUNTING, ids=[c[0] for c in ACCOUNTING])
def test_each_failure_is_accounted_for_by_its_rule(tmp_path, db, label, factory, cls, rule, wait):
    stack, clock = make(tmp_path)
    raising(stack, factory)
    error = store_failing(stack, db, "acct")
    record = error.record
    assert record.failure_class == cls and record.last_error
    assert record.lease_owner is None and record.lease_expires_at is None  # nothing is left held, whatever happened
    assert record.last_attempt_at == clock.naive  # the attempt that was claimed is on record
    if rule == BUDGET:
        assert (record.attempts, record.deferrals, record.state) == (1, 0, sr.UPLOADING) and record.deferred_since is None
        assert record.next_attempt_at == clock.naive + timedelta(seconds=wait) and error.retryable
    elif rule == DEFERRAL:
        assert (record.attempts, record.deferrals, record.state) == (0, 1, sr.UPLOADING)  # the budget is untouched
        assert record.deferred_since == clock.naive and record.next_attempt_at == clock.naive + timedelta(seconds=wait) and error.retryable
    elif rule == PARKED:
        assert (record.attempts, record.deferrals, record.state) == (0, 1, sr.UPLOADING) and record.next_attempt_at is None and not error.retryable
    else:
        assert (record.attempts, record.state, record.next_attempt_at) == (1, sr.FAILED, None) and not error.retryable
    assert stack.manager._spooled(record) == pdf_bytes("acct")  # the bytes are kept in every case
    assert len([e for e in events(db, record) if e.event_type.startswith("UPLOAD_") and e.event_type != "UPLOAD_STARTED"]) == 1  # one note for the one attempt


def test_a_genuine_transient_failure_uses_the_budget_waits_out_a_doubling_schedule_and_ends_in_failed_at_the_limit(tmp_path, db):
    stack, clock = make(tmp_path, limit=3)
    calls = raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    record = store_failing(stack, db).record  # attempt 1 at t = 0
    assert record.attempts == 1 and record.next_attempt_at == clock.naive + timedelta(seconds=60)
    stack.manager.reconcile(db, stale_after_seconds=0)
    assert len(calls) == 1  # not due yet: the schedule is the record's own
    clock.advance(61)
    stack.manager.reconcile(db, stale_after_seconds=0)  # attempt 2
    db.refresh(record)
    assert len(calls) == 2 and record.attempts == 2 and record.next_attempt_at == clock.naive + timedelta(seconds=120) and record.state == sr.UPLOADING
    clock.advance(121)
    stack.manager.reconcile(db, stale_after_seconds=0)  # attempt 3: the last one the budget allows
    db.refresh(record)
    assert len(calls) == 3 and record.state == sr.FAILED and record.attempts == 3 and record.next_attempt_at is None and record.failure_class == fl.TRANSIENT
    assert "abandoned after 3 failed attempts (limit 3)" in record.last_error
    assert len(events(db, record, "UPLOAD_RETRY_LIMIT_REACHED")) == 1 and len(events(db, record, "UPLOAD_ATTEMPT_FAILED")) == 3
    assert stack.manager._spooled(record) == pdf_bytes("s")  # bytes kept
    clock.advance(10**6)
    stack.manager.reconcile(db, stale_after_seconds=0)
    assert len(calls) == 3  # a FAILED upload is never retried automatically


@pytest.mark.parametrize(
    ("factory", "cls"),
    [(lambda: DriveAuthTransientError("down"), fl.AUTH_DEFERRAL), (lambda: TARGET("unverifiable"), fl.OUTAGE), (lambda: TARGET("hierarchy"), fl.CONFIGURATION),
     (lambda: DrivePermissionError("403", status=403), fl.PERMISSION), (lambda: DriveQuotaError("q", status=403), fl.QUOTA), (lambda: TARGET("wrong_drive"), fl.SECURITY)],
    ids=["auth", "outage", "configuration", "permission", "quota", "security"],
)
def test_a_long_run_of_deferrals_never_uses_the_budget_never_fails_the_record_and_is_reported_when_it_lasts(tmp_path, db, factory, cls):
    stack, clock = make(tmp_path, limit=3)  # a budget of three, and forty refusals
    calls = raising(stack, factory)
    record = store_failing(stack, db).record
    for _ in range(40):
        clock.advance(4000)  # beyond the longest deferral wait (3 960 s with jitter)
        stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert len(calls) == 41 and record.state == sr.UPLOADING and record.attempts == 0 and record.deferrals == 41 and record.failure_class == cls
    assert record.deferred_since is not None and fl.needs_attention(record, clock.naive) == f"deferred_{cls}"  # visible, not failed
    assert len(events(db, record, "UPLOAD_DEFERRED")) + len(events(db, record, "UPLOAD_DEFERRED_AUTH")) == 1  # one note per distinct reason, not one per pass
    repair(stack)  # whatever was wrong is fixed
    clock.advance(4000)
    stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.STORED and record.attempts == 0 and record.deferrals == 0 and record.failure_class is None and record.deferred_since is None


def test_a_success_after_failures_keeps_counting_only_the_failed_attempts(tmp_path, db):
    stack, clock = make(tmp_path)
    raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    record = store_failing(stack, db).record
    clock.advance(61)
    stack.manager.reconcile(db, stale_after_seconds=0)
    repair(stack)
    clock.advance(121)
    stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.STORED and record.attempts == 2  # two failed attempts happened; the successful one is not counted (as before this phase)
    assert record.failure_class is None and record.next_attempt_at is None and record.lease_owner is None and stack.manager._spooled(record) is None


def test_a_genuine_failure_ends_a_deferral_episode(tmp_path, db):
    stack, clock = make(tmp_path)
    raising(stack, lambda: DriveAuthTransientError("down"))
    record = store_failing(stack, db).record
    assert record.deferrals == 1 and record.deferred_since is not None
    raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    clock.advance(100)
    stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert (record.attempts, record.deferrals, record.deferred_since, record.failure_class) == (1, 0, None, fl.TRANSIENT)


def test_an_unexpected_error_is_charged_released_and_propagated_unchanged(tmp_path, db):
    stack, clock = make(tmp_path, limit=2)
    raising(stack, lambda: RuntimeError("a bug nobody foresaw"))
    with pytest.raises(RuntimeError, match="a bug"):
        stack.manager.store(db, kind=KIND, content=pdf_bytes("bug"), original_filename="b.pdf")
    record = db.execute(select(DocumentStorageRecord)).scalars().one()
    assert (record.state, record.attempts, record.failure_class, record.lease_owner) == (sr.UPLOADING, 1, fl.TRANSIENT, None)  # charged, released, scheduled
    assert "RuntimeError" in record.last_error
    clock.advance(61)
    stack.manager.reconcile(db, stale_after_seconds=0)  # a poison document cannot loop forever: its budget ends it
    db.refresh(record)
    assert record.state == sr.FAILED and record.attempts == 2


# ================================================================================================ revoked authorization: parked, not retried

def test_a_revoked_authorization_parks_the_record_releases_its_lease_and_stops_automatic_retries(tmp_path, db):
    stack, clock = make(tmp_path)
    calls = raising(stack, lambda: DriveAuthPermanentError("invalid_grant"))
    error = store_failing(stack, db, "revoked")
    record = error.record
    assert not error.retryable and record.state == sr.UPLOADING and record.failure_class == fl.AUTH_PERMANENT
    assert record.attempts == 0 and record.lease_owner is None and record.lease_expires_at is None and record.next_attempt_at is None  # no budget, no lease
    assert fl.needs_attention(record, clock.naive) == "authorization_needed"
    assert stack.manager._spooled(record) == pdf_bytes("revoked")
    for _ in range(5):  # any amount of time, any number of reconciliations: no automatic retry
        clock.advance(10**5)
        stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert len(calls) == 1 and record.state == sr.UPLOADING and record.attempts == 0 and record.failure_class == fl.AUTH_PERMANENT
    assert len(events(db, record, "UPLOAD_PARKED_AUTH")) == 1
    lease = stack.manager._claim(db, record.id, manual=False)
    assert lease is None and stack.manager._unclaimed_reason(db, record) == "parked"  # not even a direct automatic claim


def test_an_administrator_releases_a_parked_record_by_retrying_it_after_the_authorization_is_repaired(tmp_path, db, user):
    stack, clock = make(tmp_path)
    raising(stack, lambda: DriveAuthPermanentError("invalid_grant"))
    record = store_failing(stack, db, "repair").record
    still_broken = stack.manager.retry_upload  # while the authorization is still revoked, a manual retry fails the same way and parks the record again
    with pytest.raises(StorageUploadError) as again:
        still_broken(db, record.id, user_id=user.id)
    assert not again.value.retryable
    db.refresh(record)
    assert record.failure_class == fl.AUTH_PERMANENT and record.attempts == 0 and record.lease_owner is None and record.next_attempt_at is None
    repair(stack)
    retried = stack.manager.retry_upload(db, record.id, user_id=user.id)
    assert retried.state == sr.STORED and retried.failure_class is None
    assert [a.user_id for a in audits(db, audit.STORAGE_RETRY_ACCEPTED)] == [user.id, user.id]


# ====================================================================================================================== leases and claims

def test_a_claim_charges_the_attempt_and_takes_a_30_minute_lease_that_nobody_else_can_take(tmp_path, db):
    stack, clock = make(tmp_path)
    record, _ = pending(db, stack)
    lease = stack.manager._claim(db, record.id, manual=False)
    assert lease is not None and 0 < len(lease.owner) <= 40
    db.refresh(record)
    assert record.lease_owner == lease.owner and record.attempts == 1 and record.last_attempt_at == clock.naive
    assert record.lease_expires_at == clock.naive + timedelta(seconds=UPLOAD_LEASE_SECONDS)
    assert stack.manager._claim(db, record.id, manual=False) is None and stack.manager._claim(db, record.id, manual=True) is None  # not even a manual one
    assert stack.manager._unclaimed_reason(db, record) == "busy"
    clock.advance(UPLOAD_LEASE_SECONDS - 1)
    assert stack.manager._claim(db, record.id, manual=True) is None  # still inside the lease
    clock.advance(2)
    second = stack.manager._claim(db, record.id, manual=False)  # expired: taken over (every claim has its own owner token)
    assert second is not None and second.owner != lease.owner
    db.refresh(record)
    assert record.lease_owner == second.owner and record.attempts == 2  # the first attempt stays charged: it never reported back


def test_a_live_lease_keeps_reconcile_and_a_resubmission_away_and_nothing_is_uploaded_twice(tmp_path, db):
    stack, clock = make(tmp_path)
    record, content = pending(db, stack)
    lease = stack.manager._claim(db, record.id, manual=False)  # another worker is uploading this right now
    calls = raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    stack.manager.reconcile(db, stale_after_seconds=0)
    result = stack.manager.store(db, kind=KIND, content=content, original_filename="p.pdf")  # the same file submitted again meanwhile
    assert calls == [] and result.duplicate and result.record.state == sr.UPLOADING  # nobody started a second upload
    db.refresh(record)
    assert record.lease_owner == lease.owner and record.attempts == 1


def test_a_crashed_worker_is_recovered_when_its_lease_expires_and_the_crashed_attempt_stays_charged(tmp_path, db):
    stack, clock = make(tmp_path)
    record, content = pending(db, stack)
    assert stack.manager._claim(db, record.id, manual=False) is not None  # ... and the worker dies here: no outcome is ever recorded
    stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.UPLOADING and stack.fake.file_count() == 0  # inside the lease: left alone
    clock.advance(UPLOAD_LEASE_SECONDS + 1)
    report = stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert report.reuploaded == 1 and record.state == sr.STORED and stack.fake.file_count() == 1
    assert record.attempts == 1 and record.lease_owner is None  # the crashed attempt was never refunded


def test_a_worker_that_keeps_crashing_is_stopped_by_the_retry_limit(tmp_path, db):
    stack, clock = make(tmp_path, limit=3)
    record, _ = pending(db, stack)
    for _ in range(3):  # three workers each claim and die
        assert stack.manager._claim(db, record.id, manual=False) is not None
        clock.advance(UPLOAD_LEASE_SECONDS + 1)
    calls = raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    assert stack.manager._claim(db, record.id, manual=False) is None and stack.manager._unclaimed_reason(db, record) == "exhausted"
    report = stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert calls == [] and record.state == sr.FAILED and record.attempts == 3 and report.failed_uploads == 1
    assert "abandoned after 3 failed attempts" in record.last_error and len(events(db, record, "UPLOAD_RETRY_LIMIT_REACHED")) == 1
    assert stack.manager._spooled(record) is not None  # the bytes and the history are kept


def test_a_worker_that_lost_its_lease_can_record_nothing(tmp_path, db):
    stack, clock = make(tmp_path)
    record, _ = pending(db, stack)
    slow = stack.manager._claim(db, record.id, manual=False)
    clock.advance(UPLOAD_LEASE_SECONDS + 1)
    current = stack.manager._claim(db, record.id, manual=False)  # taken over while the slow worker was still going
    assert not stack.manager._record_success(db, slow, record, "STORED")  # its late success is refused
    outcome = stack.manager._record_failure(db, _Lease(record.id, slow.owner), record, DriveTransientError("503", status=503, retryable=True))
    assert outcome.lease_lost
    db.refresh(record)
    assert record.state == sr.UPLOADING and record.lease_owner == current.owner and record.attempts == 2 and record.failure_class is None  # untouched by the stale worker
    assert len(events(db, record, "UPLOAD_LEASE_LOST")) == 2 and not events(db, record, "STORED")
    assert stack.manager._record_success(db, current, record, "STORED")  # the real owner can
    db.refresh(record)
    assert record.state == sr.STORED and record.lease_owner is None


def test_a_lease_ends_exactly_once(tmp_path, db):
    stack, _ = make(tmp_path)
    record, _ = pending(db, stack)
    lease = stack.manager._claim(db, record.id, manual=False)
    assert stack.manager._record_success(db, lease, record, "STORED")
    assert not stack.manager._record_success(db, lease, record, "STORED")  # a second outcome under the same lease is ignored, and is not noted as a loss
    assert not events(db, record, "UPLOAD_LEASE_LOST") and len(events(db, record, "STORED")) == 1


def test_a_claim_requires_a_pending_due_unparked_record_with_budget_left(tmp_path, db):
    stack, clock = make(tmp_path, limit=3)
    future = clock.naive + timedelta(seconds=500)
    waiting, _ = pending(db, stack, "waiting", next_attempt_at=future)
    parked, _ = pending(db, stack, "parked", failure_class=fl.AUTH_PERMANENT)
    spent, _ = pending(db, stack, "spent", attempts=3)
    done, _ = pending(db, stack, "done", state=sr.STORED)
    failed, _ = pending(db, stack, "failed", state=sr.FAILED)
    manager = stack.manager
    assert manager._claim(db, waiting.id, manual=False) is None and manager._unclaimed_reason(db, waiting) == "not_due"
    assert manager._claim(db, parked.id, manual=False) is None and manager._unclaimed_reason(db, parked) == "parked"
    assert manager._claim(db, spent.id, manual=True) is None and manager._unclaimed_reason(db, spent) == "exhausted"  # not even a manual claim exceeds the budget
    for record in (done, failed):
        assert manager._claim(db, record.id, manual=True) is None and manager._unclaimed_reason(db, record) == "not_pending"
    assert manager._claim(db, waiting.id, manual=True) is not None and manager._claim(db, parked.id, manual=True) is not None  # manual skips schedule and parking only


def test_the_sweep_selects_only_due_unleased_unparked_pending_uploads(tmp_path, db):
    stack, clock = make(tmp_path)
    past, future = clock.naive - timedelta(seconds=5), clock.naive + timedelta(seconds=500)
    due_fresh, _ = pending(db, stack, "due-fresh")
    due_past, _ = pending(db, stack, "due-past", next_attempt_at=past)
    waiting, _ = pending(db, stack, "waiting", next_attempt_at=future)
    leased, _ = pending(db, stack, "leased", lease_owner="someone", lease_expires_at=clock.naive + timedelta(seconds=500))
    expired, _ = pending(db, stack, "expired", lease_owner="gone", lease_expires_at=past)
    parked, _ = pending(db, stack, "parked", failure_class=fl.AUTH_PERMANENT)
    failed, _ = pending(db, stack, "failed", state=sr.FAILED)
    young, _ = pending(db, stack, "young", created_at=datetime.now(timezone.utc))
    due = [r.id for r in stack.manager._due_uploads(db, clock.naive, 300)]  # the sweep's own answer, before any claim has a say
    assert due == [due_fresh.id, due_past.id, expired.id]  # (ordered by id)
    calls = raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    stack.manager.reconcile(db)  # the default staleness: a brand-new record is left to its own caller
    locations = {parse_location(r.location): r.id for r in (due_fresh, due_past, waiting, leased, expired, parked, failed, young)}
    attempted = sorted(locations[c] for c in calls)
    assert attempted == sorted([due_fresh.id, due_past.id, expired.id])


def test_the_same_file_submitted_again_while_it_waits_for_its_schedule_does_not_skip_the_wait(tmp_path, db):
    # 3A.2-B corrections (F1): a resubmission is NOT an administrator's retry. It used to bypass the backoff (and so could burn a record's budget during an
    # outage); now it is treated like the automatic retry: it waits for the schedule, and is audited.
    stack, clock = make(tmp_path)
    calls = raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    record = store_failing(stack, db, "again").record
    assert len(calls) == 1 and record.next_attempt_at is not None  # waiting 60 s
    for _ in range(5):  # a person (or a script) submits the same file over and over
        result = stack.manager.store(db, kind=KIND, content=pdf_bytes("again"), original_filename="again.pdf")
        assert result.duplicate and not result.created and result.record.state == sr.UPLOADING
    db.refresh(record)
    assert len(calls) == 1 and record.attempts == 1  # no attempt was made, the budget was not touched
    clock.advance(61)
    with pytest.raises(StorageUploadError):  # once it is due, the next resubmission does attempt
        stack.manager.store(db, kind=KIND, content=pdf_bytes("again"), original_filename="again.pdf")
    db.refresh(record)
    assert len(calls) == 2 and record.attempts == 2


# ======================================================================================================================== manual retry, audited

def failed_record(stack, db, clock):
    calls = raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    record = store_failing(stack, db, "manual").record
    while record.state != sr.FAILED:
        clock.advance(4000)
        stack.manager.reconcile(db, stale_after_seconds=0)
        db.refresh(record)
    return record, calls


def test_a_manual_retry_of_a_failed_upload_resets_the_budget_is_audited_and_completes_it(tmp_path, db, user):
    stack, clock = make(tmp_path, limit=2)
    record, _ = failed_record(stack, db, clock)
    assert record.state == sr.FAILED and record.attempts == 2
    repair(stack)
    retried = stack.manager.retry_upload(db, record.id, user_id=user.id)
    assert retried.state == sr.STORED and retried.attempts == 0 and retried.deferrals == 0 and retried.failure_class is None and stack.fake.file_count() == 1
    accepted = audits(db, audit.STORAGE_RETRY_ACCEPTED)
    assert len(accepted) == 1 and accepted[0].user_id == user.id and accepted[0].entity_id == str(record.id) and accepted[0].entity_type == "DocumentStorage"
    assert "from=FAILED" in accepted[0].details and "attempts=2" in accepted[0].details and "failure_class=transient" in accepted[0].details
    requested = events(db, record, "UPLOAD_RETRY_REQUESTED")
    assert len(requested) == 1 and requested[0].actor_id == user.id
    assert len(events(db, record, "UPLOAD_RETRY_LIMIT_REACHED")) == 1  # the history of the earlier failures stays
    assert not audits(db, audit.STORAGE_RETRY_REJECTED)


def test_a_failed_manual_retry_gets_a_fresh_budget_and_leaves_a_normal_schedule(tmp_path, db, user):
    stack, clock = make(tmp_path, limit=2)
    record, calls = failed_record(stack, db, clock)
    before = len(calls)
    with pytest.raises(StorageUploadError) as again:
        stack.manager.retry_upload(db, record.id, user_id=user.id)
    db.refresh(record)
    assert len(calls) == before + 1 and again.value.retryable and record.state == sr.UPLOADING and record.attempts == 1  # 1 of a fresh 2, not "2 + 1 > 2"
    assert record.next_attempt_at == clock.naive + timedelta(seconds=60)


def test_requests_that_cannot_be_honoured_are_refused_and_audited_too(tmp_path, db, user):
    stack, clock = make(tmp_path)
    stored, _ = pending(db, stack, "stored", state=sr.STORED)
    with pytest.raises(StorageStateError, match="no pending or failed upload"):
        stack.manager.retry_upload(db, stored.id, user_id=user.id)
    nospool, _ = pending(db, stack, "nospool", state=sr.FAILED)
    stack.manager._unspool(nospool)
    with pytest.raises(StorageStateError, match="No copy"):
        stack.manager.retry_upload(db, nospool.id, user_id=user.id)
    held, _ = pending(db, stack, "held", state=sr.FAILED, lease_owner="other-request", lease_expires_at=clock.naive + timedelta(seconds=600))
    with pytest.raises(StorageStateError, match="already being retried"):
        stack.manager.retry_upload(db, held.id, user_id=user.id)
    spent, _ = pending(db, stack, "spent", attempts=10)
    with pytest.raises(StorageStateError, match="already in progress, or has used up"):
        stack.manager.retry_upload(db, spent.id, user_id=user.id)
    with pytest.raises(LookupError):
        stack.manager.retry_upload(db, 99999, user_id=user.id)
    rejected = audits(db, audit.STORAGE_RETRY_REJECTED)
    assert [a.entity_id for a in rejected] == [str(stored.id), str(nospool.id), str(held.id), str(spent.id)] and {a.user_id for a in rejected} == {user.id}
    assert not audits(db, audit.STORAGE_RETRY_ACCEPTED)
    db.refresh(held)
    assert held.state == sr.FAILED and held.lease_owner == "other-request"  # the refused requests changed nothing


def test_a_duplicate_manual_retry_is_refused_once_the_first_has_finished(tmp_path, db, user):
    stack, clock = make(tmp_path, limit=2)
    record, _ = failed_record(stack, db, clock)
    repair(stack)
    stack.manager.retry_upload(db, record.id, user_id=user.id)
    with pytest.raises(StorageStateError):
        stack.manager.retry_upload(db, record.id, user_id=user.id)  # the double click: nothing left to retry
    assert stack.fake.file_count() == 1 and len(audits(db, audit.STORAGE_RETRY_ACCEPTED)) == 1 and len(audits(db, audit.STORAGE_RETRY_REJECTED)) == 1


def test_a_manual_retry_of_a_waiting_upload_tries_now_and_keeps_its_budget(tmp_path, db, user):
    stack, clock = make(tmp_path)
    calls = raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    record = store_failing(stack, db, "waiting").record  # attempt 1 failed: waiting 60 s
    assert record.attempts == 1 and record.next_attempt_at > clock.naive
    with pytest.raises(StorageUploadError) as again:  # an administrator does not wait for the schedule
        stack.manager.retry_upload(db, record.id, user_id=user.id)
    db.refresh(record)
    assert len(calls) == 2 and again.value.retryable and record.attempts == 2 and record.state == sr.UPLOADING  # the budget is KEPT (not reset)
    accepted = audits(db, audit.STORAGE_RETRY_ACCEPTED)
    assert len(accepted) == 1 and "from=UPLOADING" in accepted[0].details and "budget kept" in accepted[0].details and accepted[0].user_id == user.id
    repair(stack)
    done = stack.manager.retry_upload(db, record.id, user_id=user.id)
    assert done.state == sr.STORED and len(audits(db, audit.STORAGE_RETRY_ACCEPTED)) == 2


def test_a_manual_retry_still_meets_every_destination_check(tmp_path, db, user):
    stack, clock = make(tmp_path)
    raising(stack, lambda: TARGET("adopt_wrong_drive", retryable=False))
    record = store_failing(stack, db, "foreign").record
    assert record.state == sr.FAILED and record.failure_class == fl.PERMANENT
    with pytest.raises(StorageUploadError) as again:  # it fails the same way: a retry cannot get around the check
        stack.manager.retry_upload(db, record.id, user_id=user.id)
    db.refresh(record)
    assert not again.value.retryable and record.state == sr.FAILED and record.attempts == 1
    assert len(events(db, record, "UPLOAD_ADOPTION_REJECTED")) == 2


def test_the_new_retry_columns_are_not_part_of_what_the_api_shows():
    from app.schemas.storage import StorageRecordOut

    hidden = {"failure_class", "deferrals", "deferred_since", "next_attempt_at", "last_attempt_at", "lease_owner", "lease_expires_at", "attempts", "location", "md5", "sha256"}
    assert not hidden & set(StorageRecordOut.model_fields)  # (surfacing them is observability work, a later phase; the owner token also names a host)

# ================================================================= resubmission: audited, attributed, and never an administrator's retry (review F1)

def resubmit(stack, db, marker, **kwargs):
    return stack.manager.store(db, kind=KIND, content=pdf_bytes(marker), original_filename=f"{marker}.pdf", **kwargs)


def test_resubmitting_a_failed_upload_is_audited_with_the_actor_and_keeps_the_recovery_workflow(tmp_path, db, user):
    stack, clock = make(tmp_path, limit=2)
    record, _ = failed_record(stack, db, clock)
    assert record.state == sr.FAILED
    repair(stack)
    result = resubmit(stack, db, "manual", user_id=user.id)  # the established same-file recovery still works
    db.refresh(record)
    assert result.duplicate and not result.created and record.state == sr.STORED and record.attempts == 0 and stack.fake.file_count() == 1
    rows = audits(db, audit.STORAGE_RETRY_ACCEPTED)
    assert len(rows) == 1 and rows[0].user_id == user.id and rows[0].entity_id == str(record.id)
    assert "via=resubmission" in rows[0].details and "from=FAILED" in rows[0].details and f"actor={user.id}" in rows[0].details and "attempts=2" in rows[0].details
    assert [e.actor_id for e in events(db, record, "UPLOAD_RETRY_REQUESTED")] == [user.id]  # the event trail names the actor too


def test_a_resubmission_without_an_identity_is_audited_as_unknown_and_never_gets_an_invented_user(tmp_path, db, user):
    stack, clock = make(tmp_path, limit=2)
    record, _ = failed_record(stack, db, clock)
    repair(stack)
    resubmit(stack, db, "manual")  # no user_id: the caller does not know who is asking
    row = audits(db, audit.STORAGE_RETRY_ACCEPTED)[0]
    assert row.user_id is None and "actor=unknown" in row.details  # not the administrator, not user 1, not anybody
    assert events(db, record, "UPLOAD_RETRY_REQUESTED")[0].actor_id is None


def test_repeated_resubmission_cannot_burn_the_budget_or_hammer_drive_and_every_one_is_audited(tmp_path, db, user):
    stack, clock = make(tmp_path, limit=3)
    calls = raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    record = store_failing(stack, db, "storm").record
    for _ in range(20):
        resubmit(stack, db, "storm", user_id=user.id)
    db.refresh(record)
    assert len(calls) == 1 and record.attempts == 1 and record.state == sr.UPLOADING  # an outage + an impatient uploader: nothing happens
    rows = audits(db, audit.STORAGE_RESUBMITTED)
    assert len(rows) == 20 and all("no attempt started (not_due)" in r.details and r.user_id == user.id for r in rows)
    clock.advance(61)
    with pytest.raises(StorageUploadError):
        resubmit(stack, db, "storm", user_id=user.id)
    assert len(calls) == 2
    rows = audits(db, audit.STORAGE_RESUBMITTED)
    assert len(rows) == 21 and f"state=UPLOADING; attempt started; actor={user.id}" in rows[-1].details and "no attempt" not in rows[-1].details  # the attempt itself is audited too


def test_a_resubmission_cannot_release_a_record_parked_for_an_administrator(tmp_path, db, user):
    stack, clock = make(tmp_path)
    calls = raising(stack, lambda: DriveAuthPermanentError("invalid_grant"))
    record = store_failing(stack, db, "parked-resubmit").record
    assert record.failure_class == fl.AUTH_PERMANENT and len(calls) == 1
    for who in (user.id, None, user.id):
        clock.advance(10**5)
        result = resubmit(stack, db, "parked-resubmit", user_id=who)
        assert result.duplicate and not result.created and result.record.state == sr.UPLOADING
    db.refresh(record)
    assert len(calls) == 1 and record.failure_class == fl.AUTH_PERMANENT and record.attempts == 0 and record.lease_owner is None  # still parked, nothing was tried
    rows = audits(db, audit.STORAGE_RESUBMITTED)
    assert len(rows) == 3 and all("(parked)" in r.details for r in rows) and [r.user_id for r in rows] == [user.id, None, user.id]
    assert any("actor=unknown" in r.details for r in rows)
    repair(stack)  # the authorization is repaired: only an administrator's retry releases the record
    released = stack.manager.retry_upload(db, record.id, user_id=user.id)
    assert released.state == sr.STORED and audits(db, audit.STORAGE_RETRY_ACCEPTED)[-1].user_id == user.id


def test_a_resubmission_of_a_held_upload_is_audited_and_starts_nothing(tmp_path, db, user):
    stack, clock = make(tmp_path)
    record, content = pending(db, stack)
    stack.manager._claim(db, record.id, manual=False)  # another worker holds it
    calls = raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    result = stack.manager.store(db, kind=KIND, content=content, original_filename="p.pdf", user_id=user.id)
    assert calls == [] and result.duplicate and not result.created
    assert "no attempt started (busy)" in audits(db, audit.STORAGE_RESUBMITTED)[0].details


def test_a_plain_duplicate_of_a_finished_document_is_not_an_event_worth_auditing(tmp_path, db, user):
    stack, clock = make(tmp_path)
    first = resubmit(stack, db, "done")
    assert first.created and first.record.state == sr.STORED
    again = resubmit(stack, db, "done", user_id=user.id)
    assert again.duplicate and not again.created and again.record.state == sr.STORED
    assert not audits(db, audit.STORAGE_RESUBMITTED) and not audits(db, audit.STORAGE_RETRY_ACCEPTED)


def test_a_resubmission_of_an_exhausted_record_gives_it_up_visibly_and_is_audited(tmp_path, db, user):
    stack, clock = make(tmp_path, limit=2)
    record, _ = pending(db, stack, attempts=2)  # the budget is already used up (for example by crashed workers)
    with pytest.raises(StorageUploadError) as caught:
        stack.manager.store(db, kind=KIND, content=pdf_bytes("p"), original_filename="p.pdf", user_id=user.id)
    assert not caught.value.retryable
    db.refresh(record)
    assert record.state == sr.FAILED and "no attempt started (exhausted)" in audits(db, audit.STORAGE_RESUBMITTED)[0].details


# ======================================================================================================== store(): `created` means created and uploaded (F5)

def test_created_is_true_only_after_the_upload_completed_and_was_recorded(tmp_path, db):
    stack, clock = make(tmp_path)
    result = resubmit(stack, db, "fresh")
    assert result.created and not result.duplicate and result.record.state == sr.STORED and stack.fake.file_count() == 1


def test_a_failed_upload_never_returns_a_result_at_all(tmp_path, db):
    stack, clock = make(tmp_path)
    raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    with pytest.raises(StorageUploadError) as caught:
        resubmit(stack, db, "nope")
    assert caught.value.record.state == sr.UPLOADING  # the intent is on record; the caller learned it did not complete


def test_creating_a_record_never_goes_through_a_claim_so_the_creator_cannot_lose_one(tmp_path, db):
    # Step 0 / N2: the creator's lease is part of the INSERT. (This replaces the earlier test, which simulated the creator losing a separate claim after the
    # insert - an interleaving that can no longer exist.) Proof: creation succeeds and reports `created=True` even if every separate claim is refused.
    stack, clock = make(tmp_path)

    def every_claim_is_lost(*args, **kwargs):
        raise AssertionError("creating a record must not need a separate claim")

    stack.manager._claim = every_claim_is_lost
    result = resubmit(stack, db, "race")
    assert result.created and not result.duplicate and result.record.state == sr.STORED and stack.fake.file_count() == 1

def test_a_success_that_could_not_be_recorded_is_not_reported_as_a_creation(tmp_path, db, monkeypatch):
    stack, clock = make(tmp_path)
    monkeypatch.setattr(stack.manager, "_record_success", lambda *a, **k: False)  # this call lost its lease just before recording its success
    result = resubmit(stack, db, "lost")
    assert not result.created and result.duplicate and result.record.state == sr.UPLOADING
    assert stack.manager._spooled(result.record) is not None  # the retry copy stays for whoever took the upload over


def test_two_resubmissions_racing_for_one_new_file_create_it_once(tmp_path, db):
    stack, clock = make(tmp_path)
    first = resubmit(stack, db, "twice")
    second = resubmit(stack, db, "twice")
    assert [first.created, second.created] == [True, False] and second.duplicate and stack.fake.file_count() == 1


# =============================================================================================== limit-reached notes per episode, not per lifetime (review F4)

def test_a_second_separate_limit_reached_episode_is_noted_again_but_repeated_processing_of_one_is_not(tmp_path, db, user):
    stack, clock = make(tmp_path, limit=2)
    record, calls = failed_record(stack, db, clock)  # episode 0 ends in FAILED
    assert len(events(db, record, "UPLOAD_RETRY_LIMIT_REACHED")) == 1
    for _ in range(6):  # repeated processing of the SAME episode (reconciles over a long time) adds nothing
        clock.advance(10**5)
        stack.manager.reconcile(db, stale_after_seconds=0)
    assert len(events(db, record, "UPLOAD_RETRY_LIMIT_REACHED")) == 1
    with pytest.raises(StorageUploadError):  # a manual retry starts episode 1 ...
        stack.manager.retry_upload(db, record.id, user_id=user.id)
    for _ in range(3):  # ... which fails its way to the limit again
        clock.advance(4000)
        stack.manager.reconcile(db, stale_after_seconds=0)
        db.refresh(record)
    assert record.state == sr.FAILED
    notes = events(db, record, "UPLOAD_RETRY_LIMIT_REACHED")
    assert len(notes) == 2 and notes[0].detail != notes[1].detail and "episode 0" in notes[0].detail and "episode 1" in notes[1].detail
    for _ in range(4):
        clock.advance(10**5)
        stack.manager.reconcile(db, stale_after_seconds=0)
    assert len(events(db, record, "UPLOAD_RETRY_LIMIT_REACHED")) == 2  # still exactly one per episode


def test_a_crash_loop_abandonment_is_noted_once_per_episode_too(tmp_path, db):
    stack, clock = make(tmp_path, limit=2)
    record, _ = pending(db, stack)
    for _ in range(2):
        stack.manager._claim(db, record.id, manual=False)
        clock.advance(UPLOAD_LEASE_SECONDS + 1)
    for _ in range(3):
        stack.manager.reconcile(db, stale_after_seconds=0)
    assert record.state == sr.FAILED and len(events(db, record, "UPLOAD_RETRY_LIMIT_REACHED")) == 1
    assert stack.manager._reset_failed(db, record.id)  # a second episode: the same crash loop again
    for _ in range(2):
        stack.manager._claim(db, record.id, manual=False)
        clock.advance(UPLOAD_LEASE_SECONDS + 1)
    stack.manager._event(db, record, "UPLOAD_RETRY_REQUESTED", "recorded as a retry request would")  # (a reset is always accompanied by this event)
    db.commit()
    stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.FAILED and len(events(db, record, "UPLOAD_RETRY_LIMIT_REACHED")) == 2


def test_a_deferral_note_is_written_again_for_a_new_episode_but_not_for_each_pass_of_one(tmp_path, db):
    stack, clock = make(tmp_path, limit=5)
    calls = raising(stack, lambda: DriveAuthTransientError("down"))
    record = store_failing(stack, db, "episodes").record
    for _ in range(5):  # one long episode
        clock.advance(4000)
        stack.manager.reconcile(db, stale_after_seconds=0)
    assert len(events(db, record, "UPLOAD_DEFERRED_AUTH")) == 1
    raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))  # a genuine failure ENDS the episode ...
    clock.advance(4000)
    stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.deferred_since is None
    raising(stack, lambda: DriveAuthTransientError("down"))  # ... so the same kind of trouble later is a NEW episode
    clock.advance(4000)
    stack.manager.reconcile(db, stale_after_seconds=0)
    notes = events(db, record, "UPLOAD_DEFERRED_AUTH")
    assert len(notes) == 2 and notes[0].detail != notes[1].detail and len(calls) >= 6


# ====================================================================== the real sweep query, and no index that is never used (review F8)

def test_the_real_sweep_query_is_served_by_the_state_index_and_no_unused_retry_index_exists(tmp_path, db):
    import sqlalchemy as sa

    stack, clock = make(tmp_path)
    for i in range(30):
        pending(db, stack, f"plan-{i}")
    statements = []
    listener = lambda conn, cursor, statement, parameters, context, executemany: statements.append((statement, parameters))  # noqa: E731
    sa.event.listen(engine, "before_cursor_execute", listener)
    try:
        stack.manager._due_uploads(db, clock.naive, 300)  # the REAL query, not a look-alike
    finally:
        sa.event.remove(engine, "before_cursor_execute", listener)
    statement, parameters = next(s for s in statements if "FROM document_storage" in s[0] and "ORDER BY document_storage.id" in s[0])
    with engine.connect() as connection:
        plan = " | ".join(str(row[-1]) for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters))
    assert "ix_document_storage_state" in plan and "next_attempt" not in plan and "lease" not in plan, plan
    # ... and the schema carries no index that only costs writes: exactly the four original indexes (plus the unique constraint)
    from app.models.storage_record import DocumentStorageRecord as Model

    assert {ix.name for ix in Model.__table__.indexes} == {"ix_document_storage_document_pk", "ix_document_storage_kind", "ix_document_storage_sha256", "ix_document_storage_state"}


# ======================================================================= settle / commit failures leave a recoverable claim (review test-quality gaps)

def test_a_crash_between_the_settle_update_and_its_commit_leaves_the_lease_and_the_charge(tmp_path, db):
    stack, clock = make(tmp_path)
    record, _ = pending(db, stack)
    lease = stack.manager._claim(db, record.id, manual=False)
    real_event, failures = stack.manager._event, []

    def dies(*args, **kwargs):
        failures.append(1)
        raise RuntimeError("the process dies after the settle UPDATE and before its commit")

    stack.manager._event = dies
    with pytest.raises(RuntimeError):
        stack.manager._record_failure(db, lease, record, DriveTransientError("503", status=503, retryable=True))
    stack.manager._event = real_event
    db.rollback()  # what the database does with an uncommitted transaction when the process is gone
    db.refresh(record)
    assert failures and record.lease_owner == lease.owner and record.attempts == 1 and record.failure_class is None  # as if the outcome had never been recorded
    clock.advance(UPLOAD_LEASE_SECONDS + 1)
    assert stack.manager._claim(db, record.id, manual=False) is not None  # and the lease expiry recovers it


def test_a_commit_that_fails_while_recording_a_success_leaves_the_claim_intact(tmp_path, db):
    from sqlalchemy.exc import OperationalError

    stack, clock = make(tmp_path)
    record, _ = pending(db, stack)
    lease = stack.manager._claim(db, record.id, manual=False)
    real_commit = db.commit

    def failing_commit():
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    db.commit = failing_commit
    try:
        with pytest.raises(OperationalError):
            stack.manager._record_success(db, lease, record, "STORED")
    finally:
        db.commit = real_commit
    db.rollback()
    db.refresh(record)
    assert record.state == sr.UPLOADING and record.lease_owner == lease.owner and record.attempts == 1 and record.stored_at is None
    assert not events(db, record, "STORED")


def test_a_failure_while_recording_a_failure_propagates_and_the_charged_claim_is_recoverable(tmp_path, db, monkeypatch):
    stack, clock = make(tmp_path)
    raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))

    def broken(*args, **kwargs):
        raise RuntimeError("recording the outcome failed")

    stack.manager._record_failure = broken
    with pytest.raises(RuntimeError, match="recording the outcome failed"):
        resubmit(stack, db, "double-fault")
    del stack.manager._record_failure  # (the instance override only: the fixtures' own patches stay)
    db.rollback()
    record = db.execute(select(DocumentStorageRecord)).scalars().one()
    assert record.state == sr.UPLOADING and record.attempts == 1 and record.lease_owner is not None  # charged and held: exactly a crash
    clock.advance(UPLOAD_LEASE_SECONDS + 1)
    repair(stack)
    stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.STORED


# ================================================================================================= small hygiene (review F6, F11 and path safety)

def test_the_lease_owner_token_is_long_unique_and_bounded():
    from app.services.drive.lifecycle import _new_owner

    owners = {_new_owner() for _ in range(2000)}
    assert len(owners) == 2000 and all(0 < len(o) <= 40 for o in owners)
    assert all(len(o.rsplit("-", 1)[1]) == 16 for o in owners)  # 64 random bits per claim


@pytest.mark.parametrize(
    "text",
    [
        r"could not read C:\Users\Some Person\AppData\Roaming\secrets\token.json: denied",
        r"open('D:/SPINCO-SECRETS/oauth-token.json') failed",
        r"\\fileserver\share\keys\token.json is locked",
        "No such file or directory: /home/someone/.config/spinco/token.json",
    ],
)
def test_filesystem_paths_never_reach_a_persisted_error_text(text):
    from app.services.drive.lifecycle import _safe

    safe = _safe(OSError(text))
    assert "<path>" in safe and not any(part in safe for part in ("Some Person", "SPINCO-SECRETS", "fileserver", "someone", "token.json", "AppData"))
    assert safe.startswith("OSError: ")


def test_a_path_in_an_exception_is_scrubbed_from_the_record_and_its_events(tmp_path, db):
    stack, clock = make(tmp_path)
    raising(stack, lambda: DriveAuthPermanentError(f"No stored Google authorization was found at {tmp_path / 'secret-token.json'}."))
    record = store_failing(stack, db, "path-leak").record
    blob = " ".join([record.last_error or ""] + [e.detail or "" for e in events(db, record)] + [a.details or "" for a in db.execute(select(AuditLog)).scalars()])
    assert "secret-token.json" not in blob and str(tmp_path) not in blob and "<path>" in record.last_error

# ============================================================ Step 0 / N2: the record is created already claimed (exactly one creator, no stray spool)

def competing_store(stack, content, *, pretend_not_found_first=False, user_id=None):
    """A second request for the same bytes, on its OWN session, run at a precise moment (from inside a hook of the first request). With
    `pretend_not_found_first` its first look-up answers 'not known yet' - the interleaving of two requests that both passed the existence check before
    either inserted (so the loser reaches the unique-constraint path)."""
    original = stack.manager._get
    calls = []

    def get(db, kind, sha):
        calls.append(1)
        return None if (pretend_not_found_first and len(calls) == 1) else original(db, kind, sha)

    if pretend_not_found_first:
        stack.manager._get = get
    session = SessionLocal()
    try:
        result = stack.manager.store(session, kind=KIND, content=content, original_filename="same.pdf", user_id=user_id)
        return result.created, result.duplicate, result.record.state, result.record.lease_owner
    finally:
        session.close()
        if pretend_not_found_first:
            del stack.manager._get


def spool_files(tmp_path):
    spool = tmp_path / "spool"
    return sorted(p.name for p in spool.glob("*")) if spool.exists() else []


@pytest.mark.parametrize("pretend_not_found_first", [False, True], ids=["finds the committed record", "passed the existence check too (unique-key loser)"])
@pytest.mark.parametrize("moment", ["after_the_intent_commit", "during_the_upload"])
def test_a_duplicate_arriving_at_any_moment_of_the_creation_gets_the_in_progress_answer_and_the_creator_alone_creates(tmp_path, db, moment, pretend_not_found_first):
    stack, clock = make(tmp_path)
    content = pdf_bytes("one-creator")
    seen = {}
    real_spool, real_upload = stack.manager._spool, stack.storage.upload

    def duplicate_now(label):
        seen[label] = competing_store(stack, content, pretend_not_found_first=pretend_not_found_first)
        seen[label + "_spool"] = spool_files(tmp_path)  # what a loser leaves behind

    def spool_then_compete(record, data):
        real_spool(record, data)
        if moment == "after_the_intent_commit":
            duplicate_now("dup")

    def upload_then_compete(*args, **kwargs):
        if moment == "during_the_upload":
            duplicate_now("dup")
        return real_upload(*args, **kwargs)

    stack.manager._spool = spool_then_compete
    stack.storage.upload = upload_then_compete
    first = stack.manager.store(db, kind=KIND, content=content, original_filename="same.pdf")

    created, duplicate, state, owner = seen["dup"]
    assert (created, duplicate, state) == (False, True, sr.UPLOADING) and owner is not None  # the loser: "in progress, held by someone else"
    assert len(seen["dup_spool"]) == 1  # exactly the creator's retry copy: the loser wrote none and removed none
    assert first.created and not first.duplicate and first.record.state == sr.STORED  # the creator, and only the creator, reports the creation
    db.expire_all()
    rows = db.execute(select(DocumentStorageRecord)).scalars().all()
    assert len(rows) == 1 and rows[0].state == sr.STORED and rows[0].lease_owner is None and rows[0].attempts == 0 and stack.fake.file_count() == 1
    assert spool_files(tmp_path) == []  # nothing abandoned or duplicated once the upload is confirmed
    assert len(events(db, rows[0], "UPLOAD_STARTED")) == 1 and len(events(db, rows[0], "STORED")) == 1
    assert len(audits(db, audit.STORAGE_RESUBMITTED)) == 1 and "busy" in audits(db, audit.STORAGE_RESUBMITTED)[0].details


def test_the_new_record_already_holds_its_lease_when_it_first_becomes_visible(tmp_path, db):
    stack, clock = make(tmp_path)
    seen = {}
    real_spool = stack.manager._spool

    def look(record, data):
        with SessionLocal() as other:  # what ANOTHER session can see as soon as the intent commit is done
            row = other.execute(select(DocumentStorageRecord)).scalars().one()
            seen.update(state=row.state, owner=row.lease_owner, expires=row.lease_expires_at, attempts=row.attempts, last=row.last_attempt_at)
        real_spool(record, data)

    stack.manager._spool = look
    stack.manager.store(db, kind=KIND, content=pdf_bytes("visible"), original_filename="v.pdf")
    assert seen["state"] == sr.UPLOADING and seen["owner"] and seen["attempts"] == 1  # visible and claimed in one step: no unclaimed window
    assert seen["expires"] == clock.naive + timedelta(seconds=UPLOAD_LEASE_SECONDS) and seen["last"] == clock.naive
    assert len(seen["owner"]) <= 40


def test_a_failed_creation_keeps_exactly_one_retry_copy_and_a_duplicate_does_not_add_another(tmp_path, db):
    stack, clock = make(tmp_path)
    raising(stack, lambda: DriveTransientError("503", status=503, retryable=True))
    content = pdf_bytes("fails-once")
    first = store_failing(stack, db, "fails-once")
    assert first.record.attempts == 1 and first.record.lease_owner is None and spool_files(tmp_path) == [f"{KIND}_{sha(content)}.part"]
    again = competing_store(stack, content)  # not due (backoff): nothing is attempted, nothing is spooled twice
    assert again[:3] == (False, True, sr.UPLOADING)
    assert len(spool_files(tmp_path)) == 1


def test_an_overtaken_creator_cannot_claim_the_creation_or_touch_the_new_owners_record(tmp_path, db):
    """Stale-owner protection is unchanged: the creator's lease expires during its upload, another request takes the record over and completes it; the
    creator's late success is refused and it does not report a creation."""
    stack, clock = make(tmp_path)
    content = pdf_bytes("overtaken")
    real_upload, state = stack.storage.upload, {"done": False}

    def slow_then_overtaken(*args, **kwargs):
        if not state["done"]:
            state["done"] = True
            clock.advance(UPLOAD_LEASE_SECONDS + 1)  # the creator is "still uploading" past its lease
            session = SessionLocal()
            try:
                took_over = stack.manager.store(session, kind=KIND, content=content, original_filename="overtaken.pdf", user_id=None)
                state["takeover"] = (took_over.created, took_over.record.state)
            finally:
                session.close()
        return real_upload(*args, **kwargs)

    stack.storage.upload = slow_then_overtaken
    first = stack.manager.store(db, kind=KIND, content=content, original_filename="overtaken.pdf")
    assert state["takeover"] == (False, sr.STORED)  # the other request completed it - but it did not CREATE it
    assert not first.created and first.duplicate  # the creator's late success is refused: not reported as a creation
    db.expire_all()
    record = db.execute(select(DocumentStorageRecord)).scalars().one()
    assert record.state == sr.STORED and record.lease_owner is None and stack.fake.file_count() == 1
    assert len(events(db, record, "UPLOAD_LEASE_LOST")) == 1 and spool_files(tmp_path) == []


def fail_at_rename(monkeypatch, *, once_inner=None):
    """Makes ONLY the spool's final rename (`os.replace(<temp>, <...>.part)`) fail; every other os.replace in the process is the real one."""
    real = os.replace

    def replace(src, dst, *args, **kwargs):
        if str(dst).endswith(".part"):
            if once_inner is not None:
                once_inner()
            raise OSError(28, "No space left on device")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(lifecycle.os, "replace", replace)
    return real


def test_a_failed_rename_of_the_retry_copy_leaves_no_partial_or_temporary_file_and_releases_the_lease(tmp_path, db, monkeypatch):
    # Step 0 review M1: the REAL _spool runs; the failure is injected at os.replace (the old test replaced _spool wholesale, so its assertion was vacuous)
    stack, clock = make(tmp_path)
    real = fail_at_rename(monkeypatch)
    with pytest.raises(OSError):
        stack.manager.store(db, kind=KIND, content=pdf_bytes("disk-full"), original_filename="d.pdf")
    assert spool_files(tmp_path) == []  # not the .part, not a .tmp holding the document bytes
    record = db.execute(select(DocumentStorageRecord)).scalars().one()
    assert record.state == sr.UPLOADING and record.lease_owner is None and record.attempts == 1 and record.failure_class == fl.TRANSIENT  # charged, released, scheduled
    monkeypatch.setattr(lifecycle.os, "replace", real)
    clock.advance(61)
    stack.manager.reconcile(db, stale_after_seconds=0)  # no copy was kept: reconcile can only say so, never invent bytes
    db.refresh(record)
    assert record.state == sr.FAILED and "no copy was kept" in record.last_error


def test_a_partial_write_of_the_retry_copy_is_removed(tmp_path, db, monkeypatch):
    stack, clock = make(tmp_path)
    real_write = Path.write_bytes

    def partial(self, data):
        if self.name.endswith(".tmp"):
            real_write(self, data[: len(data) // 2])  # half the bytes reach the disk ...
            raise OSError(5, "I/O error")  # ... then the write fails
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", partial)
    with pytest.raises(OSError):
        stack.manager.store(db, kind=KIND, content=pdf_bytes("partial"), original_filename="p.pdf")
    assert spool_files(tmp_path) == []  # nothing partial remains


def test_cleaning_up_a_failed_spool_never_removes_another_writers_file(tmp_path, db, monkeypatch):
    """Two writers of the SAME record's retry copy: the first fails at its rename AFTER the second one has completed. The failed one removes only its own
    temporary file; the completed copy survives intact (the temp names are unique per call)."""
    stack, clock = make(tmp_path)
    content = pdf_bytes("concurrent-spool")
    record, _ = pending(db, stack, "concurrent-spool")  # a record whose retry copy the helper has already written
    stack.manager._unspool(record)
    state = {"inner_done": False}

    def second_writer_completes_first():
        if not state["inner_done"]:
            state["inner_done"] = True
            monkeypatch.setattr(lifecycle.os, "replace", real_replace)  # the inner writer uses the real rename
            stack.manager._spool(record, content)
            monkeypatch.setattr(lifecycle.os, "replace", failing)

    real_replace = os.replace
    failing = fail_at_rename(monkeypatch, once_inner=second_writer_completes_first)
    with pytest.raises(OSError):
        stack.manager._spool(record, content)  # the OUTER writer fails at its rename
    assert spool_files(tmp_path) == [f"{KIND}_{record.sha256}.part"]  # the inner writer's finished copy survived; no temp file is left
    assert stack.manager._spooled(record) == content  # ... and it is intact


def test_temporary_files_of_a_killed_process_are_swept_but_in_flight_ones_never(tmp_path, db):
    stack, clock = make(tmp_path)
    spool = tmp_path / "spool"
    spool.mkdir(exist_ok=True)
    old, young, keep = spool / "a.part.dead1.tmp", spool / "b.part.live2.tmp", spool / "c.part"
    for path in (old, young, keep):
        path.write_bytes(b"x")
    long_ago = clock.t.timestamp() - 7200
    os.utime(old, (long_ago, long_ago))
    assert stack.manager.sweep_stale_spool_temp_files() == 1
    assert not old.exists() and young.exists() and keep.exists()  # the old temp is gone; a recent (possibly in-flight) one and every real copy are untouched
    stack.manager.reconcile(db, stale_after_seconds=0)  # reconcile runs the same sweep (nothing left to remove)
    assert young.exists() and keep.exists()


# ============================================================ review F12: a legitimately long upload does not lose its lease (heartbeat)

def test_a_long_upload_keeps_its_lease_alive_so_nobody_can_take_it_over(tmp_path, db):
    """A 0.9 s lease and a 2.6 s upload on the REAL clock. Without renewal another request could claim the record after 0.9 s; with the heartbeat it
    cannot, and the uploader still completes, reports the creation and is the only one that wrote."""
    import threading
    import time

    stack = make_stack(tmp_path, manager_options={"lease_seconds": 0.9})
    stack.manager.initialise_structure()
    content = pdf_bytes("long-upload")
    real_upload = stack.storage.upload
    seen = {}

    def slow_upload(*args, **kwargs):
        time.sleep(1.4)  # longer than one lease: renewals must have happened
        with SessionLocal() as other:
            row = other.execute(select(DocumentStorageRecord)).scalars().one()
            seen["lease_still_held"], seen["expires"] = row.lease_owner is not None, row.lease_expires_at
            seen["competitor_can_claim"] = stack.manager._claim(other, row.id, manual=True) is not None  # even a MANUAL claim is refused while the lease is live
        time.sleep(1.2)
        return real_upload(*args, **kwargs)

    stack.storage.upload = slow_upload
    started = time.time()
    result = stack.manager.store(db, kind=KIND, content=content, original_filename="long.pdf")
    assert time.time() - started > 2.5  # it really ran longer than two leases
    assert seen["lease_still_held"] and not seen["competitor_can_claim"]
    assert result.created and result.record.state == sr.STORED and stack.fake.file_count() == 1
    assert not events(db, result.record, "UPLOAD_LEASE_LOST") and threading.active_count() < 20  # no renewer thread left behind


def test_a_holder_that_dies_stops_renewing_so_the_lease_still_expires(tmp_path, db):
    import time

    stack = make_stack(tmp_path, manager_options={"lease_seconds": 0.6})
    stack.manager.initialise_structure()

    class Crash(BaseException):
        pass

    def die_slowly(*args, **kwargs):
        time.sleep(0.5)
        raise Crash()

    stack.storage.upload = die_slowly
    with pytest.raises(Crash):
        stack.manager.store(db, kind=KIND, content=pdf_bytes("dies"), original_filename="d.pdf")  # the renewer is stopped by the context manager on the way out
    time.sleep(1.0)  # longer than a lease with nobody renewing
    record = db.execute(select(DocumentStorageRecord)).scalars().one()
    assert record.lease_expires_at < datetime.now(timezone.utc).replace(tzinfo=None)  # expired by itself: recovery works exactly as before
    assert stack.manager._claim(db, record.id, manual=False) is not None

# ====================================================================================================== Step 0 / N5: the stale docstring

def test_the_sweep_docstring_no_longer_names_the_removed_index():
    import inspect

    doc = inspect.getdoc(StorageManager._due_uploads)
    assert "state_next_attempt_at" not in doc and "ix_document_storage_state" in doc and "no index on the retry columns" in doc