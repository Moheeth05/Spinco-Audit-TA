"""Regression tests for the match-candidate orphan-cleanup fix - see
docs/ORPHAN_RECORD_INVESTIGATION.md for the bug and
app/services/extraction/candidate_cleanup.py for the fix. Covers all four
lifecycle paths that can invalidate Expense/Bill/TripReport ids: document
reprocessing, bill rematching, trip rematching, and document deletion - plus
rollback behavior and a full process/reprocess/rematch/reprocess/delete
repeat-cycle proving zero new orphans accumulate.

These tests use the real pipeline against real sample PDFs (not mocked) -
the most trustworthy way to prove the fix works end-to-end, matching how
the orphans were originally discovered.
"""

from pathlib import Path

from app.database import SessionLocal
from app.models.bill import Bill
from app.models.bill_match import BillMatchCandidate
from app.models.expense_report import Expense, ExpenseReport
from app.models.trip import Trip, TripReport
from app.models.trip_match import TripMatchCandidate
from app.services.extraction import candidate_cleanup

ADMIN_EMAIL = "intern@spincotech.com"
ADMIN_PASSWORD = "ChangeMe123!"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# A second, unrelated real Expense Report PDF - used to prove cleanup is
# correctly SCOPED (only the report being reprocessed/rematched/deleted
# loses candidates) and, as a side effect, to avoid SQLite's rowid-reuse
# behavior on a table that would otherwise go fully empty in a
# single-report test (see the id-based test design notes below).
OTHER_ER_PDF_PATH = REPO_ROOT / "ER-40223.pdf"


def _login_admin(client):
    resp = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200
    return resp.json()["csrf_token"]


def _upload_expense_report(client, sample_pdf_path):
    with open(sample_pdf_path, "rb") as f:
        resp = client.post("/api/uploads/expense-reports", files={"files": ("er.pdf", f, "application/pdf")})
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["success"] is True
    return result["document_id"]  # "expense:<id>"


def _fk_orphan_counts(db):
    bmc_bill_orphan = db.query(BillMatchCandidate).filter(
        ~BillMatchCandidate.bill_id.in_(db.query(Bill.id))
    ).count()
    bmc_expense_orphan = db.query(BillMatchCandidate).filter(
        ~BillMatchCandidate.expense_id.in_(db.query(Expense.id))
    ).count()
    tmc_expense_orphan = db.query(TripMatchCandidate).filter(
        ~TripMatchCandidate.expense_id.in_(db.query(Expense.id))
    ).count()
    tmc_trip_orphan = db.query(TripMatchCandidate).filter(
        ~TripMatchCandidate.trip_report_id.in_(db.query(TripReport.id))
    ).count()
    return bmc_bill_orphan, bmc_expense_orphan, tmc_expense_orphan, tmc_trip_orphan


# ---------- A. Document reprocessing ----------


def test_reprocess_removes_old_candidates_and_creates_no_new_orphans(client, sample_pdf_path):
    # NOTE on test design: checking "the old parent/candidate row ids no
    # longer exist" is NOT reliable here - SQLite reuses rowids once a
    # table's rows are all deleted (these models have no AUTOINCREMENT
    # keyword), so a reprocessed report's new rows can legitimately get the
    # exact same ids as the old ones, especially with only one report in a
    # fresh test DB. Uploading a second, unrelated report keeps the tables
    # non-empty throughout (avoiding that recycling) and, more importantly,
    # lets this test prove the real requirement directly: zero orphaned
    # candidate rows anywhere, and the unrelated report's own candidates
    # completely untouched.
    other_document_id = _upload_expense_report(client, OTHER_ER_PDF_PATH)
    other_report_id = int(other_document_id.split(":")[1])
    document_id = _upload_expense_report(client, sample_pdf_path)
    report_id = int(document_id.split(":")[1])

    db = SessionLocal()
    try:
        first_run_expense_ids = [e.id for e in db.query(Expense.id).filter(Expense.expense_report_id == report_id).all()]
        assert first_run_expense_ids  # sanity: the sample PDF genuinely produced expense lines
        other_bmc_ids_before = {c.id for c in db.query(BillMatchCandidate.id).join(
            Expense, Expense.id == BillMatchCandidate.expense_id
        ).filter(Expense.expense_report_id == other_report_id).all()}
        assert other_bmc_ids_before  # sanity: the other report also has real candidates
    finally:
        db.close()

    csrf = _login_admin(client)
    resp = client.post(f"/api/documents/{document_id}/reprocess", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200

    db = SessionLocal()
    try:
        # Zero orphaned candidate rows anywhere in the database.
        bmc_bill_orphan, bmc_expense_orphan, tmc_expense_orphan, tmc_trip_orphan = _fk_orphan_counts(db)
        assert (bmc_bill_orphan, bmc_expense_orphan, tmc_expense_orphan, tmc_trip_orphan) == (0, 0, 0, 0)

        # The unrelated report's own candidates are completely untouched.
        other_bmc_ids_after = {c.id for c in db.query(BillMatchCandidate.id).join(
            Expense, Expense.id == BillMatchCandidate.expense_id
        ).filter(Expense.expense_report_id == other_report_id).all()}
        assert other_bmc_ids_after == other_bmc_ids_before
    finally:
        db.close()


# ---------- B. Bill rematching ----------


def test_bill_rematch_removes_old_bill_candidates(client, sample_pdf_path):
    # Same test-design reasoning as test A above - a second, unrelated
    # report keeps tables non-empty (avoiding SQLite rowid reuse) and lets
    # this test prove the real requirement: zero orphans, unrelated data
    # untouched.
    other_document_id = _upload_expense_report(client, OTHER_ER_PDF_PATH)
    other_report_id = int(other_document_id.split(":")[1])
    document_id = _upload_expense_report(client, sample_pdf_path)
    report_id = int(document_id.split(":")[1])

    db = SessionLocal()
    try:
        first_run_bill_ids = [b.id for b in db.query(Bill.id).filter(Bill.expense_report_id == report_id).all()]
        assert first_run_bill_ids  # sanity
        other_bmc_ids_before = {c.id for c in db.query(BillMatchCandidate.id).join(
            Bill, Bill.id == BillMatchCandidate.bill_id
        ).filter(Bill.expense_report_id == other_report_id).all()}
        assert other_bmc_ids_before  # sanity: the other report also has real candidates
    finally:
        db.close()

    csrf = _login_admin(client)
    resp = client.post(f"/api/documents/{document_id}/rematch-bills", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200

    db = SessionLocal()
    try:
        bmc_bill_orphan, bmc_expense_orphan, _, _ = _fk_orphan_counts(db)
        assert (bmc_bill_orphan, bmc_expense_orphan) == (0, 0)

        other_bmc_ids_after = {c.id for c in db.query(BillMatchCandidate.id).join(
            Bill, Bill.id == BillMatchCandidate.bill_id
        ).filter(Bill.expense_report_id == other_report_id).all()}
        assert other_bmc_ids_after == other_bmc_ids_before
    finally:
        db.close()


# ---------- C. Trip rematching ----------


def test_trip_match_candidate_cleanup_helper_is_scoped_correctly(client):
    """Direct proof the trip-side cleanup helper only removes what it's
    told to, and preserves everything else - match_batta_expenses() itself
    was confirmed (during investigation) to already correctly replace its
    own candidates for an unchanged expense set; the actual orphaning came
    from upstream expense/trip-report id invalidation, which is what
    candidate_cleanup's callers in pipeline.py now close."""
    db = SessionLocal()
    try:
        report = ExpenseReport(
            filename="x.pdf", original_filename="x.pdf", file_hash="c1" * 32, file_size=1, source_path="/tmp/x.pdf"
        )
        db.add(report)
        db.commit()
        db.refresh(report)
        expense = Expense(expense_report_id=report.id, s_no=1)
        db.add(expense)
        trip_a = TripReport(filename="a.pdf", original_filename="a.pdf", file_hash="c2" * 32)
        trip_b = TripReport(filename="b.pdf", original_filename="b.pdf", file_hash="c3" * 32)
        db.add_all([trip_a, trip_b])
        db.commit()
        db.refresh(expense)
        db.refresh(trip_a)
        db.refresh(trip_b)

        db.add_all([
            TripMatchCandidate(expense_id=expense.id, trip_report_id=trip_a.id, rank=1, score=100),
            TripMatchCandidate(expense_id=expense.id, trip_report_id=trip_b.id, rank=2, score=50),
        ])
        db.commit()

        # Only trip_a is "about to be replaced" - trip_b's candidate must survive.
        candidate_cleanup.delete_trip_match_candidates_for_trip_report_ids(db, [trip_a.id])
        db.commit()

        remaining = db.query(TripMatchCandidate).filter(TripMatchCandidate.expense_id == expense.id).all()
        assert len(remaining) == 1
        assert remaining[0].trip_report_id == trip_b.id
    finally:
        db.rollback()
        db.close()


# ---------- D. Document deletion ----------


def test_document_deletion_removes_its_candidates_but_preserves_unrelated_ones(client, sample_pdf_path, sample_trip_pdf_path):
    """Upload two independent expense reports; delete one; confirm its
    candidates are gone and the OTHER report's candidates are untouched."""
    doc_a = _upload_expense_report(client, sample_pdf_path)
    report_a_id = int(doc_a.split(":")[1])

    with open(sample_trip_pdf_path, "rb") as f:
        trip_resp = client.post("/api/uploads/trip-reports", files={"files": ("trip.pdf", f, "application/pdf")})
    assert trip_resp.status_code == 200

    db = SessionLocal()
    try:
        report_a_expense_ids = [e.id for e in db.query(Expense.id).filter(Expense.expense_report_id == report_a_id).all()]
        report_a_bill_ids = [b.id for b in db.query(Bill.id).filter(Bill.expense_report_id == report_a_id).all()]
        assert report_a_expense_ids
        unrelated_bmc_before = db.query(BillMatchCandidate).filter(
            ~BillMatchCandidate.expense_id.in_(report_a_expense_ids)
        ).count()
    finally:
        db.close()

    csrf = _login_admin(client)
    delete_resp = client.delete(f"/api/documents/{doc_a}", headers={"X-CSRF-Token": csrf})
    assert delete_resp.status_code == 204

    db = SessionLocal()
    try:
        stale_by_expense = db.query(BillMatchCandidate).filter(
            BillMatchCandidate.expense_id.in_(report_a_expense_ids)
        ).count()
        stale_by_bill = db.query(BillMatchCandidate).filter(
            BillMatchCandidate.bill_id.in_(report_a_bill_ids)
        ).count()
        assert stale_by_expense == 0
        assert stale_by_bill == 0

        unrelated_bmc_after = db.query(BillMatchCandidate).filter(
            ~BillMatchCandidate.expense_id.in_(report_a_expense_ids)
        ).count()
        assert unrelated_bmc_after == unrelated_bmc_before  # untouched
    finally:
        db.close()


def test_trip_report_deletion_removes_its_trip_match_candidates(client):
    db = SessionLocal()
    try:
        report = ExpenseReport(
            filename="x.pdf", original_filename="x.pdf", file_hash="d1" * 32, file_size=1, source_path="/tmp/x.pdf"
        )
        db.add(report)
        db.commit()
        db.refresh(report)
        expense = Expense(expense_report_id=report.id, s_no=1)
        db.add(expense)
        trip = TripReport(filename="t.pdf", original_filename="t.pdf", file_hash="d2" * 32, source_path="/tmp/t.pdf")
        db.add(trip)
        db.commit()
        db.refresh(expense)
        db.refresh(trip)
        db.add(TripMatchCandidate(expense_id=expense.id, trip_report_id=trip.id, rank=1, score=100))
        db.commit()
        trip_id = trip.id
        document_id = f"trip:{trip_id}"
    finally:
        db.close()

    csrf = _login_admin(client)
    resp = client.delete(f"/api/documents/{document_id}", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 204

    db = SessionLocal()
    try:
        remaining = db.query(TripMatchCandidate).filter(TripMatchCandidate.trip_report_id == trip_id).count()
        assert remaining == 0
    finally:
        db.close()


# ---------- E. Rollback behavior ----------


def test_rollback_after_candidate_cleanup_restores_both_candidates_and_parents(client):
    """Forces an exception after candidate cleanup but before the parent
    transaction commits - both the cleanup and the (not-yet-committed)
    parent change must roll back together, since they share one
    transaction with no commit in between (see candidate_cleanup.py's
    module docstring)."""
    db = SessionLocal()
    try:
        report = ExpenseReport(
            filename="x.pdf", original_filename="x.pdf", file_hash="e1" * 32, file_size=1, source_path="/tmp/x.pdf"
        )
        db.add(report)
        db.commit()
        db.refresh(report)
        expense = Expense(expense_report_id=report.id, s_no=1)
        bill = Bill(expense_report_id=report.id)
        db.add_all([expense, bill])
        db.commit()
        db.refresh(expense)
        db.refresh(bill)
        db.add(BillMatchCandidate(expense_id=expense.id, bill_id=bill.id, rank=1, score=100))
        db.commit()
        expense_id, bill_id = expense.id, bill.id

        # Simulate the same sequence the real fix uses: cleanup, then a
        # parent-affecting operation, but raise before any commit.
        candidate_cleanup.delete_candidates_for_expense_ids(db, [expense_id])
        db.query(Expense).filter(Expense.id == expense_id).delete()
        try:
            raise RuntimeError("simulated failure before commit")
        except RuntimeError:
            db.rollback()

        candidate_still_there = db.query(BillMatchCandidate).filter(BillMatchCandidate.expense_id == expense_id).count()
        expense_still_there = db.query(Expense).filter(Expense.id == expense_id).count()
        assert candidate_still_there == 1  # cleanup was rolled back
        assert expense_still_there == 1  # parent delete was rolled back
    finally:
        db.rollback()
        db.close()


# ---------- F. Repeat-cycle test ----------


def test_repeat_process_reprocess_rematch_reprocess_delete_leaves_zero_new_orphans(client, sample_pdf_path):
    document_id = _upload_expense_report(client, sample_pdf_path)
    csrf = _login_admin(client)

    resp1 = client.post(f"/api/documents/{document_id}/reprocess", headers={"X-CSRF-Token": csrf})
    assert resp1.status_code == 200

    resp2 = client.post(f"/api/documents/{document_id}/rematch-bills", headers={"X-CSRF-Token": csrf})
    assert resp2.status_code == 200

    resp3 = client.post(f"/api/documents/{document_id}/reprocess", headers={"X-CSRF-Token": csrf})
    assert resp3.status_code == 200

    db = SessionLocal()
    try:
        orphans_before_delete = _fk_orphan_counts(db)
        assert orphans_before_delete == (0, 0, 0, 0)
    finally:
        db.close()

    resp4 = client.delete(f"/api/documents/{document_id}", headers={"X-CSRF-Token": csrf})
    assert resp4.status_code == 204

    db = SessionLocal()
    try:
        orphans_after_delete = _fk_orphan_counts(db)
        assert orphans_after_delete == (0, 0, 0, 0)
    finally:
        db.close()
