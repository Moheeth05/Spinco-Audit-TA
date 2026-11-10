"""Storage lifecycle on top of the Drive adapter: reliable store, archive/restore, external-change detection, reconciliation and the
change feed. FAKE-DRIVE tests of OUR logic - they are not evidence of how Google's real service behaves."""

import hashlib
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.database import Base, SessionLocal, engine
from app.models import storage_record as sr
from app.models.expense_report import Expense, ExpenseReport
from app.models.logs import AuditLog
from app.models.storage_record import DocumentStorageRecord, DriveSyncState, StorageEvent
from app.models.user import User
from app.services.drive.client import DriveTransientError
from app.services.drive.lifecycle import StorageStateError, StorageUploadError
from app.services.drive.storage import make_location, parse_location

from drive_testkit import make_stack, pdf_bytes

KIND = "expense_report"


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def user(db):
    row = User(email="drive.admin@example.com", name="Drive Admin", role="ADMIN", password_hash=None, is_active=True)
    db.add(row)
    db.commit()
    return row


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def events(db, record=None, event_type=None):
    query = select(StorageEvent).order_by(StorageEvent.id)
    if record is not None:
        query = query.where(StorageEvent.record_id == record.id)
    if event_type:
        query = query.where(StorageEvent.event_type == event_type)
    return db.execute(query).scalars().all()


def store(stack, db, marker="a", user_id=None):
    content = pdf_bytes(marker)
    result = stack.manager.store(db, kind=KIND, content=content, original_filename=f"ER-{marker}.pdf", user_id=user_id)
    return result, content


def backdate(db, record, seconds=3600):
    record.created_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    record.updated_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    if record.next_attempt_at is not None:  # 3A.2-B: a failed attempt waits for its own schedule (next_attempt_at); "the backoff has passed" moves it into the past
        record.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds)
    db.commit()


# ------------------------------------------------------------------------------------------------------------------- store


def test_store_makes_a_verified_stored_record_and_the_intent_is_durable_before_the_upload(tmp_path, db):
    stack = make_stack(tmp_path)
    content = pdf_bytes("one")
    seen = {}
    original = stack.storage.upload

    def spy(*args, **kwargs):
        with SessionLocal() as other:  # a DIFFERENT session: what is committed, not what is pending
            row = other.execute(select(DocumentStorageRecord)).scalars().one()
            seen["state"], seen["location"] = row.state, row.location
        return original(*args, **kwargs)

    stack.storage.upload = spy
    result = stack.manager.store(db, kind=KIND, content=content, original_filename="ER-1.pdf")
    assert seen["state"] == sr.UPLOADING and seen["location"].startswith("gdrive:")  # intent existed before any byte was sent
    record = result.record
    assert result.created and not result.duplicate
    assert record.state == sr.STORED and record.md5 == hashlib.md5(content).hexdigest() and record.size == len(content)
    assert record.stored_at and record.last_verified_at and record.last_error is None
    assert stack.storage.retrieve(record.location) == content
    assert [e.event_type for e in events(db, record)] == ["UPLOAD_STARTED", "STORED"]
    assert not list((tmp_path / "spool").glob("*"))  # the retry copy is removed once Drive has confirmed the bytes


def test_a_record_never_becomes_stored_when_the_upload_fails(tmp_path, db):
    stack = make_stack(tmp_path, max_attempts=2)
    stack.storage.reserve_id()  # prime the id pool, so the intent can still be recorded while Drive is down
    stack.fake.fail_next(100, 503)
    content = pdf_bytes("fail")
    with pytest.raises(StorageUploadError) as exc:
        stack.manager.store(db, kind=KIND, content=content, original_filename="a.pdf")
    record = exc.value.record
    assert exc.value.retryable and record.state == sr.UPLOADING and record.attempts == 1 and record.last_error
    assert not [e for e in events(db, record) if e.event_type == "STORED"]
    assert stack.fake.file_count() == 0
    assert list((tmp_path / "spool").glob("*"))  # the bytes are kept so the upload can be retried or reconciled


def test_a_retry_after_an_outage_completes_the_same_record_with_exactly_one_file(tmp_path, db):
    stack = make_stack(tmp_path, max_attempts=2)
    stack.storage.reserve_id()  # prime the id pool, so the intent can still be recorded while Drive is down
    stack.fake.fail_next(100, 503)
    content = pdf_bytes("retry")
    with pytest.raises(StorageUploadError) as failed:
        stack.manager.store(db, kind=KIND, content=content, original_filename="a.pdf")
    stack.fake.faults.clear()  # Drive is back
    waiting = stack.manager.store(db, kind=KIND, content=content, original_filename="a.pdf")
    # 3A.2-B corrections: submitting the same file again is not an administrator's retry, so it waits for the record's backoff like the automatic retry does
    assert waiting.duplicate and not waiting.created and waiting.record.state == sr.UPLOADING
    backdate(db, failed.value.record)  # the backoff has passed
    result = stack.manager.store(db, kind=KIND, content=content, original_filename="a.pdf")
    assert result.duplicate and result.record.state == sr.STORED
    assert db.execute(select(func.count()).select_from(DocumentStorageRecord)).scalar() == 1
    assert stack.fake.file_count() == 1


def test_when_drive_cannot_even_reserve_an_id_nothing_is_recorded_and_the_caller_just_retries(tmp_path, db):
    stack = make_stack(tmp_path, max_attempts=2)  # empty id pool + Drive down
    stack.fake.fail_next(100, 503)
    with pytest.raises(StorageUploadError) as exc:
        stack.manager.store(db, kind=KIND, content=pdf_bytes("no-id"), original_filename="a.pdf")
    assert exc.value.retryable and exc.value.record is None
    assert db.execute(select(func.count()).select_from(DocumentStorageRecord)).scalar() == 0
    assert not list((tmp_path / "spool").glob("*")) if (tmp_path / "spool").exists() else True

def test_a_permission_refusal_is_a_visible_deferral_that_uses_no_retry_budget(tmp_path, db):
    # 3A.2-B: a permission refusal says something about the account or the folder, not about this upload. It used to end in FAILED after one try; now the
    # record keeps its bytes, uses no retry budget, waits on a slow schedule and is retried once someone fixes the permission.
    stack = make_stack(tmp_path)
    stack.fake.read_only = True
    with pytest.raises(StorageUploadError) as exc:
        stack.manager.store(db, kind=KIND, content=pdf_bytes("perm"), original_filename="a.pdf")
    record = exc.value.record
    assert exc.value.retryable and record.state == sr.UPLOADING and record.attempts == 0
    assert record.failure_class == "permission" and record.deferrals == 1 and record.deferred_since is not None and record.next_attempt_at is not None
    assert record.lease_owner is None and record.lease_expires_at is None  # nothing is left held
    assert "Permission" in record.last_error


def test_an_integrity_failure_is_failed_and_quarantined_never_stored(tmp_path, db):
    stack = make_stack(tmp_path)
    stack.fake.corrupt_next_upload = True
    with pytest.raises(StorageUploadError) as exc:
        stack.manager.store(db, kind=KIND, content=pdf_bytes("bad"), original_filename="a.pdf")
    assert not exc.value.retryable and exc.value.record.state == sr.FAILED
    assert [e.event_type for e in events(db, exc.value.record)][-1] == "UPLOAD_INTEGRITY_FAILED"
    assert [f for f in stack.fake.files.values() if stack.folders.quarantine() in f["parents"]]


def test_duplicates_create_no_second_file_and_no_second_record(tmp_path, db):
    stack = make_stack(tmp_path)
    first, content = store(stack, db, "dup")
    second = stack.manager.store(db, kind=KIND, content=content, original_filename="another name.pdf")
    assert second.duplicate and second.record.id == first.record.id
    assert stack.fake.file_count() == 1 and db.execute(select(func.count()).select_from(DocumentStorageRecord)).scalar() == 1


def test_uploading_the_bytes_of_an_archived_document_does_not_resurrect_or_duplicate_it(tmp_path, db, user):
    stack = make_stack(tmp_path)
    first, content = store(stack, db, "arch")
    stack.manager.archive(db, first.record.id, user_id=user.id)
    again = stack.manager.store(db, kind=KIND, content=content, original_filename="a.pdf")
    assert again.duplicate and again.record.state == sr.ARCHIVED and stack.fake.file_count() == 1


def test_concurrent_uploads_of_the_same_bytes_converge_on_one_record_and_one_file(tmp_path, db):
    stack = make_stack(tmp_path)
    content = pdf_bytes("race")
    barrier = threading.Barrier(6)
    results, errors = [], []

    def go():
        session = SessionLocal()
        try:
            barrier.wait(timeout=30)
            results.append(stack.manager.store(session, kind=KIND, content=content, original_filename="a.pdf"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(timeout=90) for t in threads]
    assert not errors, errors
    assert len(results) == 6 and sum(r.created for r in results) == 1
    with SessionLocal() as check:
        assert check.execute(select(func.count()).select_from(DocumentStorageRecord)).scalar() == 1
        assert check.execute(select(DocumentStorageRecord)).scalars().one().state == sr.STORED
    assert stack.fake.file_count() == 1


def test_different_documents_get_different_files(tmp_path, db):
    stack = make_stack(tmp_path)
    a, _ = store(stack, db, "alpha")
    b, _ = store(stack, db, "beta-longer-marker")
    assert a.record.id != b.record.id and a.record.location != b.record.location and stack.fake.file_count() == 2


# ------------------------------------------------------------------------------------------------------------------- interrupted uploads


def _intent(db, stack, content, *, uploaded: bool, spool: bool):
    rid = stack.storage.reserve_id()
    if uploaded:
        stack.storage.upload(KIND, sha(content), "a.pdf", content, reserved_id=rid)
    record = DocumentStorageRecord(
        kind=KIND, sha256=sha(content), original_filename="a.pdf", size=len(content), md5=hashlib.md5(content).hexdigest(),
        location=make_location(rid), state=sr.UPLOADING,
    )
    db.add(record)
    db.commit()
    if spool:
        stack.manager._spool(record, content)
    backdate(db, record)
    return record


def test_reconcile_adopts_a_file_that_reached_drive_before_the_process_died(tmp_path, db):
    stack = make_stack(tmp_path)
    record = _intent(db, stack, pdf_bytes("died-after"), uploaded=True, spool=False)
    report = stack.manager.reconcile(db)
    db.refresh(record)
    assert report.adopted_uploads == 1 and record.state == sr.STORED and stack.fake.file_count() == 1
    assert "ADOPTED_AFTER_INTERRUPTION" in [e.event_type for e in events(db, record)]


def test_reconcile_retries_from_the_spooled_copy_when_nothing_reached_drive(tmp_path, db):
    stack = make_stack(tmp_path)
    content = pdf_bytes("died-before")
    record = _intent(db, stack, content, uploaded=False, spool=True)
    stack.manager.initialise_structure()  # reconcile never creates folders itself: here the structure already exists (see test_drive_guard.py for the case where it does not)
    report = stack.manager.reconcile(db)
    db.refresh(record)
    assert report.reuploaded == 1 and record.state == sr.STORED and stack.fake.file_count() == 1
    assert stack.storage.retrieve(record.location) == content
    assert not list((tmp_path / "spool").glob("*"))


def test_an_upload_that_can_no_longer_be_completed_becomes_a_visible_failure_not_a_silent_success(tmp_path, db):
    stack = make_stack(tmp_path)
    record = _intent(db, stack, pdf_bytes("lost-forever"), uploaded=False, spool=False)
    report = stack.manager.reconcile(db)
    db.refresh(record)
    assert report.failed_uploads == 1 and record.state == sr.FAILED and "uploaded again" in record.last_error
    assert "UPLOAD_ABANDONED" in [e.event_type for e in events(db, record)]


def test_a_recent_upload_still_in_flight_is_left_alone(tmp_path, db):
    stack = make_stack(tmp_path)
    record = _intent(db, stack, pdf_bytes("in-flight"), uploaded=False, spool=True)
    record.created_at = datetime.now(timezone.utc)
    db.commit()
    stack.manager.reconcile(db, stale_after_seconds=300)
    db.refresh(record)
    assert record.state == sr.UPLOADING and stack.fake.file_count() == 0


def test_a_file_with_the_reserved_id_but_different_bytes_is_a_failure_not_an_adoption(tmp_path, db):
    stack = make_stack(tmp_path)
    content = pdf_bytes("mismatch")
    record = _intent(db, stack, content, uploaded=False, spool=False)
    stack.fake._add(parse_location(record.location), "other.pdf", "application/pdf", [stack.folders.active(KIND)], content=b"different bytes entirely")
    stack.manager.reconcile(db)
    db.refresh(record)
    assert record.state == sr.FAILED and "UPLOAD_INTEGRITY_FAILED" in [e.event_type for e in events(db, record)]


# ------------------------------------------------------------------------------------------------------------------- archive / restore


def test_archive_hides_the_document_and_records_who_and_when(tmp_path, db, user):
    stack = make_stack(tmp_path)
    result, _ = store(stack, db, "arch1")
    record = stack.manager.archive(db, result.record.id, user_id=user.id)
    assert record.state == sr.ARCHIVED and record.archived_by_id == user.id and record.archived_at
    assert stack.fake.files[parse_location(record.location)]["parents"] == [stack.folders.archive(KIND)]
    active = db.execute(select(DocumentStorageRecord).where(DocumentStorageRecord.state == sr.STORED)).scalars().all()
    assert active == []  # hidden from the active listing
    audit = db.execute(select(AuditLog).where(AuditLog.action == "DOCUMENT_ARCHIVED")).scalars().all()
    assert len(audit) == 1 and audit[0].user_id == user.id and audit[0].entity_id == str(record.id)
    assert "ARCHIVED" in [e.event_type for e in events(db, record)]
    assert stack.manager.archive(db, record.id, user_id=user.id).state == sr.ARCHIVED  # idempotent
    assert len(db.execute(select(AuditLog).where(AuditLog.action == "DOCUMENT_ARCHIVED")).scalars().all()) == 1


def test_a_failed_drive_move_leaves_the_database_unchanged(tmp_path, db, user):
    stack = make_stack(tmp_path, max_attempts=2)
    result, _ = store(stack, db, "arch-fail")
    stack.fake.fail_next(100, 503)
    with pytest.raises(DriveTransientError):
        stack.manager.archive(db, result.record.id, user_id=user.id)
    db.refresh(result.record)
    assert result.record.state == sr.STORED and result.record.archived_at is None
    assert not db.execute(select(AuditLog).where(AuditLog.action == "DOCUMENT_ARCHIVED")).scalars().all()


def test_archive_preserves_every_piece_of_application_history(tmp_path, db, user):
    stack = make_stack(tmp_path)
    result, content = store(stack, db, "history", user_id=user.id)
    report = ExpenseReport(filename="h.pdf", original_filename="ER-history.pdf", file_hash=sha(content), file_size=len(content), source_path=result.record.location)
    db.add(report)
    db.flush()
    db.add(Expense(expense_report_id=report.id, s_no=1, category="TEST CATEGORY", claimed_amount=123))
    db.add(AuditLog(user_id=user.id, action="DOCUMENT_UPLOADED", entity_type="ExpenseReport", entity_id=f"expense:{report.id}", details="synthetic"))
    db.commit()
    stack.manager.link_document(db, result.record.id, "expense", report.id)
    before = (db.query(ExpenseReport).count(), db.query(Expense).count(), db.query(AuditLog).filter(AuditLog.action == "DOCUMENT_UPLOADED").count())
    stack.manager.archive(db, result.record.id, user_id=user.id)
    stack.fake.external_delete(parse_location(result.record.location))
    stack.manager.reconcile(db)
    db.refresh(result.record)
    assert result.record.state == sr.MISSING_EXTERNAL
    after = (db.query(ExpenseReport).count(), db.query(Expense).count(), db.query(AuditLog).filter(AuditLog.action == "DOCUMENT_UPLOADED").count())
    assert before == after == (1, 1, 1)  # archiving AND an external deletion removed nothing from the application's history
    assert db.get(ExpenseReport, report.id).source_path == result.record.location


def test_restore_returns_an_archived_document_to_the_active_folder_with_history_intact(tmp_path, db, user):
    stack = make_stack(tmp_path)
    result, content = store(stack, db, "restore")
    stack.manager.archive(db, result.record.id, user_id=user.id)
    record = stack.manager.restore(db, result.record.id, user_id=user.id)
    assert record.state == sr.STORED and record.restored_by_id == user.id and record.restored_at and record.archived_at
    assert stack.fake.files[parse_location(record.location)]["parents"] == [stack.folders.active(KIND)]
    assert stack.storage.retrieve(record.location) == content
    assert len(db.execute(select(AuditLog).where(AuditLog.action == "DOCUMENT_RESTORED")).scalars().all()) == 1
    assert stack.manager.restore(db, record.id, user_id=user.id).state == sr.STORED  # idempotent


def test_invalid_transitions_are_refused_with_a_clear_error(tmp_path, db, user):
    stack = make_stack(tmp_path)
    stack.fake.read_only = True
    with pytest.raises(StorageUploadError) as exc:
        stack.manager.store(db, kind=KIND, content=pdf_bytes("x"), original_filename="a.pdf")
    failed = exc.value.record
    with pytest.raises(StorageStateError):
        stack.manager.archive(db, failed.id, user_id=user.id)
    with pytest.raises(StorageStateError):
        stack.manager.restore(db, failed.id, user_id=user.id)
    with pytest.raises(LookupError):
        stack.manager.archive(db, 99999, user_id=user.id)


# ------------------------------------------------------------------------------------------------------------------- external changes


def test_an_external_deletion_is_detected_hidden_and_never_loses_history(tmp_path, db):
    stack = make_stack(tmp_path)
    result, _ = store(stack, db, "ext-del")
    stack.fake.external_trash(parse_location(result.record.location))
    report = stack.manager.reconcile(db)
    db.refresh(result.record)
    assert report.missing_external == 1 and result.record.state == sr.MISSING_EXTERNAL and result.record.missing_detected_at
    assert [e.event_type for e in events(db, result.record)][-1] == "EXTERNAL_DELETE_DETECTED"
    assert not db.execute(select(DocumentStorageRecord).where(DocumentStorageRecord.state == sr.STORED)).scalars().all()  # hidden from active listing
    again = stack.manager.reconcile(db)
    assert again.missing_external == 0  # reported once, not on every pass
    assert len(events(db, result.record, "EXTERNAL_DELETE_DETECTED")) == 1


def test_a_hard_deleted_file_is_detected_too_and_cannot_be_restored(tmp_path, db, user):
    stack = make_stack(tmp_path)
    result, _ = store(stack, db, "ext-hard")
    stack.fake.external_delete(parse_location(result.record.location))
    stack.manager.reconcile(db)
    db.refresh(result.record)
    assert result.record.state == sr.MISSING_EXTERNAL
    with pytest.raises(StorageStateError, match="does not replace it"):  # 3A.2-E: the old text promised that re-uploading repairs it, which it cannot
        stack.manager.restore(db, result.record.id, user_id=user.id)
    db.refresh(result.record)
    assert result.record.state == sr.MISSING_EXTERNAL


def test_a_file_restored_from_the_drive_trash_is_noticed_and_can_be_restored_by_an_admin(tmp_path, db, user):
    stack = make_stack(tmp_path)
    result, _ = store(stack, db, "ext-undo")
    file_id = parse_location(result.record.location)
    stack.fake.external_trash(file_id)
    stack.manager.reconcile(db)
    stack.fake.external_untrash(file_id)
    report = stack.manager.reconcile(db)
    db.refresh(result.record)
    assert report.recovered_externally == 1 and result.record.state == sr.STORED and result.record.missing_detected_at is None
    stack.fake.external_trash(file_id)
    stack.manager.reconcile(db)
    restored = stack.manager.restore(db, result.record.id, user_id=user.id)  # trashed and flagged: an admin brings it back
    assert restored.state == sr.STORED and not stack.fake.files[file_id]["trashed"]


def test_rename_move_and_content_changes_are_flagged_once_without_guessing(tmp_path, db):
    stack = make_stack(tmp_path)
    result, _ = store(stack, db, "drift")
    file_id = parse_location(result.record.location)
    stack.fake.external_rename(file_id, "someone renamed this.pdf")
    stack.fake.external_move(file_id, stack.folders.archive(KIND))
    stack.fake.external_modify(file_id, b"%PDF- edited by someone else")
    first = stack.manager.reconcile(db)
    assert (first.renamed_externally, first.moved_externally, first.content_changed_externally) == (1, 1, 1)
    db.refresh(result.record)
    assert result.record.state == sr.STORED  # flagged for an admin, never auto-"corrected"
    second = stack.manager.reconcile(db)
    assert (second.renamed_externally, second.moved_externally, second.content_changed_externally) == (0, 0, 0)
    assert {e.event_type for e in events(db, result.record)} >= {"NAME_DRIFT", "EXTERNAL_MOVE_DETECTED", "CONTENT_CHANGED_EXTERNALLY"}
    assert file_id in stack.fake.files  # nothing was deleted or moved back by the application


def test_a_drive_outage_during_reconciliation_never_marks_documents_missing(tmp_path, db):
    stack = make_stack(tmp_path, max_attempts=2)
    result, _ = store(stack, db, "outage")
    stack.fake.fail_next(500, 503)
    report = stack.manager.reconcile(db)
    db.refresh(result.record)
    assert report.errors and report.missing_external == 0 and result.record.state == sr.STORED
    assert db.execute(select(AuditLog).where(AuditLog.action == "STORAGE_RECONCILED")).scalars().all()  # the failed pass is itself on record


def test_orphan_files_are_reported_and_never_touched(tmp_path, db):
    stack = make_stack(tmp_path)
    stack.folders.ensure_structure()
    stack.fake._add("orphanfile000000001", "ffffffffffff_orphan.pdf", "application/pdf", [stack.folders.active(KIND)], content=b"%PDF- orphan", app_properties={"sha256": "ab" * 32, "kind": KIND})
    report = stack.manager.reconcile(db)
    assert report.orphans == 1 and "orphanfile000000001" in stack.fake.files and not stack.fake.files["orphanfile000000001"]["trashed"]
    assert events(db, None, "ORPHAN_FILE_FOUND")[0].detail == f"kind={KIND}, sha256={'ab' * 6}"
    assert stack.manager.reconcile(db).orphans == 0  # reported once


# ------------------------------------------------------------------------------------------------------------------- change feed


def test_the_change_feed_detects_external_deletion_and_ignores_unrelated_files(tmp_path, db):
    stack = make_stack(tmp_path)
    result, _ = store(stack, db, "feed")
    stack.folders.ensure_structure()  # on-demand folder creation is itself a Drive change: do it before the baseline
    assert stack.manager.sync_changes(db).baseline
    assert db.get(DriveSyncState, "changes").page_token is not None
    stack.fake._add("unrelatedfile00001", "unrelated.pdf", "application/pdf", [stack.config.root_folder_id], content=b"x")
    stack.fake._changed("unrelatedfile00001")
    stack.fake.external_trash(parse_location(result.record.location))
    report = stack.manager.sync_changes(db)
    db.refresh(result.record)
    assert report.changes_seen == 2 and report.tracked_changes == 1 and result.record.state == sr.MISSING_EXTERNAL
    assert stack.manager.sync_changes(db).changes_seen == 0  # the token advanced


def test_the_change_feed_follows_pages(tmp_path, db):
    stack = make_stack(tmp_path)
    result, _ = store(stack, db, "paged")
    stack.folders.ensure_structure()
    stack.manager.sync_changes(db)
    for i in range(130):
        stack.fake._add(f"filler{i:013d}", "f.pdf", "application/pdf", [stack.config.root_folder_id], content=b"x")
        stack.fake._changed(f"filler{i:013d}")
    stack.fake.external_trash(parse_location(result.record.location))
    report = stack.manager.sync_changes(db)
    db.refresh(result.record)
    assert report.changes_seen == 131 and result.record.state == sr.MISSING_EXTERNAL


def test_an_expired_change_token_triggers_a_full_reconciliation_and_a_new_token(tmp_path, db):
    stack = make_stack(tmp_path)
    result, _ = store(stack, db, "expired")
    stack.folders.ensure_structure()
    stack.manager.sync_changes(db)
    old_token = db.get(DriveSyncState, "changes").page_token
    stack.fake.external_trash(parse_location(result.record.location))  # a change the lost feed can no longer report
    stack.fake.min_valid_change_token = len(stack.fake.changes) + 5
    report = stack.manager.sync_changes(db)
    db.refresh(result.record)
    assert report.token_expired and report.reconcile is not None and report.reconcile["missing_external"] == 1
    assert result.record.state == sr.MISSING_EXTERNAL  # found by the full pass, not lost
    assert events(db, None, "CHANGE_TOKEN_EXPIRED_RECOVERED")
    stack.fake.min_valid_change_token = 0
    db.expire_all()
    assert db.get(DriveSyncState, "changes").page_token != old_token
    assert not stack.manager.sync_changes(db).token_expired


# ------------------------------------------------------------------------------------------------------------------- offline / hygiene


def test_offline_uploads_wait_visibly_and_complete_when_the_network_returns(tmp_path, db):
    stack = make_stack(tmp_path, max_attempts=2)
    stack.storage.reserve_id()  # prime the id pool, so the intent can still be recorded while Drive is down
    stack.fake.drop_connection_next(1000)
    content = pdf_bytes("offline")
    with pytest.raises(StorageUploadError) as exc:
        stack.manager.store(db, kind=KIND, content=content, original_filename="a.pdf")
    assert exc.value.retryable and exc.value.record.state == sr.UPLOADING
    with pytest.raises(DriveTransientError):  # reads fail loudly rather than reporting the file as missing
        stack.storage.exists(exc.value.record.location)
    stack.fake.faults.clear()  # network is back: the periodic reconcile finishes the job from the spooled copy
    backdate(db, exc.value.record)
    stack.manager.initialise_structure()  # the folders never existed (Drive was down from the start): creating them is explicit, reconcile will not
    report = stack.manager.reconcile(db)
    db.refresh(exc.value.record)
    assert report.reuploaded == 1 and exc.value.record.state == sr.STORED and stack.fake.file_count() == 1


def test_no_secret_is_ever_written_to_events_errors_or_audit_rows(tmp_path, db, user):
    stack = make_stack(tmp_path, max_attempts=2)
    stack.storage.reserve_id()  # prime the id pool, so the intent can still be recorded while Drive is down
    stack.fake.fail_next(100, 503)
    with pytest.raises(StorageUploadError):
        stack.manager.store(db, kind=KIND, content=pdf_bytes("s1"), original_filename="a.pdf")
    stack.fake.faults.clear()
    result, _ = store(stack, db, "s2")
    stack.manager.archive(db, result.record.id, user_id=user.id)
    stack.manager.reconcile(db)
    blob = " ".join(
        [e.detail or "" for e in events(db)]
        + [r.last_error or "" for r in db.execute(select(DocumentStorageRecord)).scalars().all()]
        + [a.details or "" for a in db.execute(select(AuditLog)).scalars().all()]
    )
    for secret in ("Bearer", "BEGIN", "fake-token", "eyJ", "private_key", "gdrive:", "fakeid"):
        assert secret not in blob, secret
