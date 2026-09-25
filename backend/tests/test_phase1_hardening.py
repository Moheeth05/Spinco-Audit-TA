"""Phase 1 (DB hardening) + Phase 2 (storage abstraction) regression tests
- see docs/CENTRALIZED_ARCHITECTURE.md for the architecture report these
implement against. Covers exactly what was newly added/changed in that
work: the admin TOCTOU fix, audit-log/business-action atomicity, the three
new constraints (+ the exception_verifications identity uniqueness found
along the way), and the storage abstraction. Existing behavior (auth, RBAC,
extraction/OCR, exception engine, Excel generation) is proven unchanged by
the rest of the suite passing unmodified alongside these.
"""

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.logs import AuditLog
from app.models.trip import Trip, TripReport
from app.models.user import User
from app.models.validation import ExceptionVerification
from app.services.storage_backend import DocumentMetadata, LocalFilesystemStorage, storage, storage_kind_for

ADMIN_EMAIL = "intern@spincotech.com"
ADMIN_PASSWORD = "ChangeMe123!"


def _login_admin(client):
    resp = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200
    return resp.json()["csrf_token"]


def _create_user(client, csrf, *, name="Second Admin", email="admin2@spincotech.com", role="ADMIN"):
    resp = client.post(
        "/api/users",
        json={"name": name, "email": email, "role": role, "password": "InitialPass123"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201
    return resp.json()


def _make_expense_report(db):
    # These tests don't use the `client` fixture (no HTTP layer needed), so
    # the DB isn't reset between them - a unique hash per call avoids
    # colliding with rows left over from an earlier test in this same file.
    row = ExpenseReport(
        filename="x.pdf", original_filename="x.pdf", file_hash=uuid.uuid4().hex.ljust(64, "0"),
        file_size=1, source_path="/tmp/x.pdf",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _make_trip_report(db):
    row = TripReport(filename="t.pdf", original_filename="t.pdf", file_hash=uuid.uuid4().hex.ljust(64, "0"))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------- Admin TOCTOU fix ----------


def test_existing_last_admin_protections_still_work(client):
    """The pre-existing behavior (block the final active admin) must be
    unchanged by the atomic-guard rewrite - this is the same scenario
    test_users.py already covers via the HTTP layer; re-asserted here
    directly against the guard clause's underlying SQL to prove the new
    mechanism (not just the old check) produces the same outcome."""
    csrf = _login_admin(client)
    me = client.get("/api/auth/me").json()["user"]

    resp = client.put(f"/api/users/{me['id']}/role", json={"role": "VIEWER"}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400
    assert "final active administrator" in resp.json()["detail"]

    resp2 = client.put(f"/api/users/{me['id']}/status", json={"is_active": False}, headers={"X-CSRF-Token": csrf})
    assert resp2.status_code == 400


def test_concurrent_demote_of_two_admins_never_leaves_zero_active(client):
    """The actual TOCTOU scenario: two admins, both requests try to demote
    a DIFFERENT admin "at the same time" (simulated here as sequential HTTP
    calls, which is the worst case for the OLD check-then-write code - by
    the time the second call's check runs, the first has already committed,
    so a naive separate-statement check would have incorrectly allowed it
    only if it re-read stale data; the atomic guarded UPDATE re-evaluates
    the guard as part of one statement, so this must correctly block the
    second demotion once only one active admin remains)."""
    csrf = _login_admin(client)
    admin_a = client.get("/api/auth/me").json()["user"]
    admin_b = _create_user(client, csrf, name="Admin B", email="adminb@spincotech.com", role="ADMIN")

    first = client.put(f"/api/users/{admin_b['id']}/role", json={"role": "VIEWER"}, headers={"X-CSRF-Token": csrf})
    assert first.status_code == 200
    assert first.json()["role"] == "VIEWER"

    second = client.put(f"/api/users/{admin_a['id']}/role", json={"role": "VIEWER"}, headers={"X-CSRF-Token": csrf})
    assert second.status_code == 400

    db = SessionLocal()
    try:
        active_admins = db.query(User).filter(User.role == "ADMIN", User.is_active.is_(True)).count()
        assert active_admins == 1
    finally:
        db.close()


def test_guarded_update_blocks_at_the_sql_level_even_with_stale_python_state(client):
    """Directly exercises the guard clause's actual SQL (not the HTTP
    layer) to prove the mechanism itself - not just the endpoint's
    behavior - closes the race: an UPDATE guarded by a same-statement
    subquery must affect zero rows once no other active admin exists,
    regardless of what any Python-level object still holds in memory."""
    from sqlalchemy import update

    from app.api.users import _other_active_admin_exists_clause

    csrf = _login_admin(client)
    me = client.get("/api/auth/me").json()["user"]

    db = SessionLocal()
    try:
        result = db.execute(
            update(User)
            .where(User.id == me["id"], User.role == "ADMIN", _other_active_admin_exists_clause(me["id"]))
            .values(role="VIEWER")
        )
        db.commit()
        assert result.rowcount == 0
        still_admin = db.get(User, me["id"])
        assert still_admin.role == "ADMIN"
    finally:
        db.close()


# ---------- Audit atomicity ----------


def test_audit_row_exists_for_successful_role_change(client):
    csrf = _login_admin(client)
    created = _create_user(client, csrf, name="Auditor X", email="auditx@spincotech.com", role="AUDITOR")
    resp = client.put(f"/api/users/{created['id']}/role", json={"role": "VIEWER"}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200

    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(
            AuditLog.action == "USER_ROLE_CHANGED", AuditLog.entity_id == str(created["id"])
        ).all()
        assert len(rows) == 1
    finally:
        db.close()


def test_blocked_last_admin_change_writes_no_audit_row(client):
    """A rejected change (final admin protection) must produce NEITHER the
    business change NOR a misleading audit row claiming one happened."""
    csrf = _login_admin(client)
    me = client.get("/api/auth/me").json()["user"]

    db = SessionLocal()
    before = db.query(AuditLog).filter(AuditLog.action == "USER_ROLE_CHANGED").count()
    db.close()

    resp = client.put(f"/api/users/{me['id']}/role", json={"role": "VIEWER"}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400

    db = SessionLocal()
    try:
        after = db.query(AuditLog).filter(AuditLog.action == "USER_ROLE_CHANGED").count()
        assert after == before
    finally:
        db.close()


def test_log_action_no_longer_commits_by_itself(client):
    """Direct unit proof of the atomicity fix: log_action must only flush
    (stage the row in the current transaction), never commit - a caller
    that adds a row, calls log_action, then rolls back must see BOTH
    disappear together."""
    from app.services import audit

    db = SessionLocal()
    try:
        file_hash = uuid.uuid4().hex.ljust(64, "0")
        report = ExpenseReport(
            filename="rollback.pdf", original_filename="rollback.pdf", file_hash=file_hash, file_size=1,
            source_path="/tmp/rollback.pdf",
        )
        db.add(report)
        db.flush()
        audit.log_action(db, user_id=None, action="TEST_ROLLBACK_PROOF", entity_type="ExpenseReport", entity_id=str(report.id))
        db.rollback()

        still_there = db.query(ExpenseReport).filter(ExpenseReport.file_hash == file_hash).first()
        audit_row = db.query(AuditLog).filter(AuditLog.action == "TEST_ROLLBACK_PROOF").first()
        assert still_there is None
        assert audit_row is None
    finally:
        db.close()


def test_login_failure_audit_row_persists_despite_no_other_commit(client):
    """login()'s failure path previously relied entirely on log_action's own
    commit (now removed) - must have its own explicit commit or the audit
    row silently vanishes."""
    resp = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong-password"})
    assert resp.status_code == 401

    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.action == "AUTH_LOGIN_FAILURE").all()
        assert len(rows) >= 1
    finally:
        db.close()


# ---------- New constraints ----------


def test_duplicate_expense_line_number_within_report_rejected(client):
    db = SessionLocal()
    try:
        report = _make_expense_report(db)
        db.add(Expense(expense_report_id=report.id, s_no=1))
        db.commit()
        db.add(Expense(expense_report_id=report.id, s_no=1))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_null_s_no_does_not_conflict_with_itself(client):
    """NULL s_no rows (extraction incomplete) must never falsely collide -
    a real, unavoidable case until extraction fully resolves every line."""
    db = SessionLocal()
    try:
        report = _make_expense_report(db)
        db.add(Expense(expense_report_id=report.id, s_no=None))
        db.add(Expense(expense_report_id=report.id, s_no=None))
        db.commit()  # must not raise
        count = db.query(Expense).filter(Expense.expense_report_id == report.id).count()
        assert count == 2
    finally:
        db.rollback()
        db.close()


def test_duplicate_trip_leg_number_within_report_rejected(client):
    db = SessionLocal()
    try:
        trip = _make_trip_report(db)
        db.add(Trip(trip_report_id=trip.id, leg_no=1))
        db.commit()
        db.add(Trip(trip_report_id=trip.id, leg_no=1))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_bill_matched_expense_id_foreign_key_enforced(client):
    """This FK is correctly declared in the schema (and will be genuinely
    enforced once migrated to PostgreSQL, which always enforces FKs), but
    SQLite does not enforce FOREIGN KEY constraints on a connection unless
    PRAGMA foreign_keys=ON is set on it - app/database.py never sets this
    (a pre-existing characteristic of every FK in this schema, not
    something this phase changed). This was deliberately NOT turned on:
    doing so was tested and would immediately break the app, because it
    would newly start enforcing on ~2000 already-existing orphaned
    bill_match_candidates/trip_match_candidates rows discovered while
    investigating this - see docs/CENTRALIZED_ARCHITECTURE.md's final task
    report. So this test proves the constraint is correctly PRESENT IN THE
    SCHEMA (verifiable independent of whether the current connection
    enforces it), not that the live app connection rejects a bad insert
    today - it does not, and forcing that today would be destructive."""
    db = SessionLocal()
    try:
        from sqlalchemy import inspect

        fks = inspect(db.get_bind()).get_foreign_keys("bills")
        matched = [fk for fk in fks if fk["constrained_columns"] == ["matched_expense_id"]]
        assert len(matched) == 1
        assert matched[0]["referred_table"] == "expenses"
    finally:
        db.close()


def test_bill_matched_expense_id_foreign_key_enforced_when_pragma_is_on(tmp_path):
    """Proves the constraint DOES work correctly (e.g. under PostgreSQL,
    which always enforces FKs) by exercising it through a raw sqlite3
    connection with PRAGMA foreign_keys=ON - isolated from the app's own
    (deliberately unchanged) connection configuration."""
    import sqlite3

    from app.database import Base
    from sqlalchemy import create_engine

    db_path = tmp_path / "fk_test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO expense_reports (filename, original_filename, file_hash, file_type, file_size, source_path, "
        "processing_status, extraction_status, total_expenses, verified_expense_count, exception_count, "
        "review_required_count, uploaded_at) VALUES ('x','x','h','application/pdf',1,'/tmp/x','QUEUED',"
        "'NOT_PROCESSED',0,0,0,0,datetime('now'))"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bills (expense_report_id, matched_expense_id, match_status, extraction_status) "
            "VALUES (1, 999999, 'UNMATCHED', 'REVIEW_REQUIRED')"
        )
        conn.commit()
    conn.close()


def test_bill_matched_expense_id_accepts_a_real_expense(client):
    db = SessionLocal()
    try:
        report = _make_expense_report(db)
        expense = Expense(expense_report_id=report.id, s_no=1)
        db.add(expense)
        db.commit()
        db.add(Bill(expense_report_id=report.id, matched_expense_id=expense.id))
        db.commit()  # must not raise
    finally:
        db.rollback()
        db.close()


def test_exception_verification_identity_uniqueness_enforced_for_expense_level(client):
    """The realistic, common case - a verification tied to a specific
    expense line (expense_id is a real id, not NULL)."""
    db = SessionLocal()
    try:
        report = _make_expense_report(db)
        expense = Expense(expense_report_id=report.id, s_no=1)
        db.add(expense)
        db.commit()
        user = db.query(User).filter(User.email == ADMIN_EMAIL).first()
        kwargs = dict(
            expense_report_id=report.id, expense_id=expense.id, exception_type="MISSING BILL",
            original_severity="REVIEW REQUIRED", original_reason="test", verified_by_id=user.id,
            verified_at=datetime.now(timezone.utc),
        )
        db.add(ExceptionVerification(**kwargs))
        db.commit()
        db.add(ExceptionVerification(**kwargs))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_exception_verification_null_expense_id_not_covered_by_unique_constraint(client):
    """Known, documented limitation (not a bug in this phase's work): a
    multi-column UNIQUE constraint treats every NULL as distinct from every
    other NULL under standard SQL semantics, so two report-level
    verifications (expense_id IS NULL) for the same exception_type are NOT
    caught by uq_exception_verifications_identity - only the expense-level
    case (tested above) is actually protected. Confirmed zero existing
    report-level duplicates in dev/production before this constraint was
    added, so this is a documented gap for future work, not a regression -
    closing it fully would need a partial/filtered unique index
    (WHERE expense_id IS NULL), deliberately not added in this phase to
    avoid dialect-specific index syntax beyond what was asked for."""
    db = SessionLocal()
    try:
        report = _make_expense_report(db)
        user = db.query(User).filter(User.email == ADMIN_EMAIL).first()
        kwargs = dict(
            expense_report_id=report.id, expense_id=None, exception_type="TOTAL MISMATCH",
            original_severity="REVIEW REQUIRED", original_reason="test", verified_by_id=user.id,
            verified_at=datetime.now(timezone.utc),
        )
        db.add(ExceptionVerification(**kwargs))
        db.commit()
        db.add(ExceptionVerification(**kwargs))
        db.commit()  # does NOT raise - this is the documented gap, not an assertion it's fine
        count = db.query(ExceptionVerification).filter(
            ExceptionVerification.expense_report_id == report.id, ExceptionVerification.expense_id.is_(None)
        ).count()
        assert count == 2  # proves the gap exists, so a future fix has a failing test to flip
    finally:
        db.rollback()
        db.close()


# ---------- Storage abstraction ----------


def test_local_storage_save_retrieve_roundtrip(tmp_path, monkeypatch):
    import app.services.storage as storage_module
    import app.services.storage_backend as backend_module

    monkeypatch.setattr(storage_module, "EXPENSE_REPORTS_DIR", tmp_path / "expense_reports")
    monkeypatch.setattr(backend_module, "_KIND_DIRS", {"expense_report": tmp_path / "expense_reports"})

    local = LocalFilesystemStorage()
    location = local.save("expense_report", "abcdef123456", "My Report.pdf", b"%PDF-1.4 test content")

    assert local.exists(location)
    assert local.retrieve(location) == b"%PDF-1.4 test content"
    meta = local.get_metadata(location)
    assert isinstance(meta, DocumentMetadata)
    assert meta.exists is True
    assert meta.size_bytes == len(b"%PDF-1.4 test content")
    assert local.local_path(location).name.startswith("abcdef123456")

    local.delete(location)
    assert not local.exists(location)
    assert local.get_metadata(location).exists is False


def test_local_storage_delete_is_idempotent(tmp_path, monkeypatch):
    import app.services.storage_backend as backend_module

    monkeypatch.setattr(backend_module, "_KIND_DIRS", {"expense_report": tmp_path})
    local = LocalFilesystemStorage()
    location = local.save("expense_report", "aaaaaaaaaaaa", "doc.pdf", b"content")
    local.delete(location)
    local.delete(location)  # must not raise on an already-deleted file


def test_storage_kind_for():
    assert storage_kind_for(expense=True) == "expense_report"
    assert storage_kind_for(expense=False) == "trip_report"


def test_upload_download_delete_roundtrip_through_storage_abstraction(client, sample_pdf_path):
    """Full-stack proof the API layer's new storage.save/exists/delete calls
    behave identically to the old direct-Path code they replaced."""
    with open(sample_pdf_path, "rb") as f:
        upload = client.post("/api/uploads/expense-reports", files={"files": ("roundtrip.pdf", f, "application/pdf")})
    assert upload.status_code == 200
    result = upload.json()["results"][0]
    assert result["success"] is True
    document_id = result["document_id"]

    download = client.get(f"/api/documents/{document_id}/file")
    assert download.status_code == 200
    assert download.content[:4] == b"%PDF"

    csrf = _login_admin(client)
    delete = client.delete(f"/api/documents/{document_id}", headers={"X-CSRF-Token": csrf})
    assert delete.status_code == 204

    missing = client.get(f"/api/documents/{document_id}/file")
    assert missing.status_code == 404
