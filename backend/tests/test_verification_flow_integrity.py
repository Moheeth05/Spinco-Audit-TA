"""Manual verification against the REAL validation flow (validate_expense_report -> evaluate_report), report deletion,
and authorization. Synthetic fixtures and the disposable test database only: no authentic PDF, no real database.

Part 1: approving a BILL MATCH review flips the persisted bill_match_status to MATCHED. That flip must never be
        mistaken for a matcher-confirmed match: an unresolved Shared Bill review is independent and stays OPEN.
Part 2: deleting a report must not leave live verifications that could attach to a later report reusing SQLite ids.
Part 3: authorization, and BILL / TRIP MATCH approvals through the real flow.
"""

from datetime import date

import pytest

from app.database import SessionLocal
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.validation import ExceptionRecord
from app.services import exception_engine as ee
from app.services.validation.pipeline import validate_expense_report

from test_validation import _make_report
from test_verification_evidence_policy import BILL_MATCH, MANUAL_PREFIX, _expense_row, _verify

SHARED = "DUPLICATE BILL REVIEW REQUIRED"
DAY = date(2026, 9, 21)


def _revalidate(report_id: int) -> None:
    """What every real flow does after a change: the actual validation, then the actual evaluation."""
    db = SessionLocal()
    try:
        validate_expense_report(db, report_id)
        ee.evaluate_report(db, report_id)
    finally:
        db.close()


def _seed_shared_bill_claims(er_id="ER-77501", s_nos=(4, 11, 38), amount=1150.0, status="REVIEW_REQUIRED", page=15) -> tuple[int, dict[int, int], int]:
    """Several cab claims that one bill supports equally, none confirmed by the matcher (REVIEW_REQUIRED) - and the
    bill's date unreadable, so every claim also carries a BILL MATCH review. Validated and evaluated for real."""
    db = SessionLocal()
    try:
        report = _make_report(db, er_id=er_id, total_claimed=amount * len(s_nos))
        bill = Bill(expense_report_id=report.id, page_number=page, amount=amount, bill_date=None, category_guess="Cab/Taxi", extraction_status="EXTRACTED")
        db.add(bill)
        db.commit()
        db.refresh(bill)
        ids = {}
        for s_no in s_nos:
            expense = Expense(expense_report_id=report.id, s_no=s_no, expense_date=DAY, category="CAB CHARGES", claimed_amount=amount, bill_id=None,
                              candidate_bill_id=bill.id, bill_amount=amount, bill_match_status=status, bill_match_reasons="The date is unclear.")
            db.add(expense)
            db.commit()
            ids[s_no] = expense.id
        validate_expense_report(db, report.id)
        ee.evaluate_report(db, report.id)
        return report.id, ids, bill.id
    finally:
        db.close()


def _records(client, report_id: int, expense_id: int) -> dict[str, dict]:
    return {e["exception_type"]: e for e in client.get(f"/api/expense-reports/{report_id}/exceptions").json() if e["expense_id"] == expense_id}


def _statuses_of(report_id: int, expense_id: int) -> tuple[str, str]:
    db = SessionLocal()
    try:
        return db.get(Expense, expense_id).validation_status, db.get(ExpenseReport, report_id).overall_status
    finally:
        db.close()


# ================================================================ PART 1: the Shared Bill review survives a BILL MATCH approval


def test_the_fixture_gives_each_claim_a_bill_match_review_and_a_separate_shared_bill_review(client):
    report_id, ids, _bill = _seed_shared_bill_claims()
    for expense_id in ids.values():
        records = _records(client, report_id, expense_id)
        assert records[BILL_MATCH]["status"] == "OPEN" and records[SHARED]["status"] == "OPEN"
        assert _expense_row(expense_id).bill_match_status == "REVIEW_REQUIRED"


def test_verifying_the_bill_match_does_not_make_the_shared_bill_review_disappear(client):
    report_id, ids, _bill = _seed_shared_bill_claims()
    target = ids[4]
    _verify(client, _records(client, report_id, target)[BILL_MATCH], note="the date was checked by hand")
    assert _expense_row(target).bill_match_status == "MATCHED", "fixture: the approval flipped the persisted status"

    _revalidate(report_id)  # the actual validation + evaluation

    records = _records(client, report_id, target)
    assert records[BILL_MATCH]["status"] == "RESOLVED", "the approved item stays approved"
    assert SHARED in records and records[SHARED]["status"] == "OPEN", "the unresolved shared-bill review must still be open"
    expense_status, report_status = _statuses_of(report_id, target)
    assert expense_status != "VERIFIED" and report_status != "ACCOUNT VERIFIED", "not verified merely because a review item vanished"
    for s_no in (11, 38):  # the other claims on the same bill are untouched
        other = _records(client, report_id, ids[s_no])
        assert other[SHARED]["status"] == "OPEN" and other[BILL_MATCH]["status"] == "OPEN"


def test_the_shared_bill_review_survives_repeated_and_api_revalidation(client):
    report_id, ids, _bill = _seed_shared_bill_claims()
    target = ids[11]
    _verify(client, _records(client, report_id, target)[BILL_MATCH])
    approved = _expense_row(target)
    assert approved.bill_match_status == "MATCHED" and approved.bill_match_reasons == f"{MANUAL_PREFIX} - The date is unclear."

    _revalidate(report_id)
    _revalidate(report_id)
    assert client.post(f"/api/expense-reports/{report_id}/revalidate").status_code == 200  # the real endpoint: validate + evaluate

    records = _records(client, report_id, target)
    assert records[SHARED]["status"] == "OPEN" and records[BILL_MATCH]["status"] == "RESOLVED"
    after = _expense_row(target)
    assert after.bill_match_status == "MATCHED" and after.bill_match_reasons == approved.bill_match_reasons, "the prefix is applied once and never stacks"
    assert _statuses_of(report_id, target)[0] != "VERIFIED"


def test_the_shared_bill_review_also_survives_when_a_multiple_candidates_bill_match_is_approved(client):
    report_id, ids, _bill = _seed_shared_bill_claims(status="MULTIPLE_CANDIDATES")
    target = ids[4]
    _verify(client, _records(client, report_id, target)[BILL_MATCH], note="the date was checked by hand")
    assert _expense_row(target).bill_match_status == "MATCHED"
    for _ in range(3):
        _revalidate(report_id)
        records = _records(client, report_id, target)
        assert records[SHARED]["status"] == "OPEN", "the shared-bill review is independent of the BILL MATCH approval"
        assert _statuses_of(report_id, target)[0] != "VERIFIED" and _statuses_of(report_id, target)[1] != "ACCOUNT VERIFIED"


def test_known_limit_a_multiple_candidates_bill_match_approval_needs_one_re_verification_after_the_first_revalidation(client):
    """FAIL-SAFE LIMITATION (documented, not a defect of this change): for a MULTIPLE_CANDIDATES claim the persisted flip to MATCHED
    changes the Required Document Check from REVIEW_REQUIRED to PASS, so the next real validation produces a BILL MATCH family with
    one row fewer. Its evidence signature then differs from the approved one and the approval is (safely) no longer applied. The
    status is restored as REVIEW_REQUIRED, and a second approval - given on rows that no longer depend on the flip - is stable."""
    report_id, ids, _bill = _seed_shared_bill_claims("ER-77504", s_nos=(4,), status="MULTIPLE_CANDIDATES")
    claim = ids[4]
    assert set(_records(client, report_id, claim)) == {BILL_MATCH}
    _verify(client, _records(client, report_id, claim)[BILL_MATCH], note="first approval")
    assert _statuses_of(report_id, claim) == ("VERIFIED", "ACCOUNT VERIFIED")

    for _ in range(3):  # never flaps: it stays open however often it is revalidated
        _revalidate(report_id)
        stale = _records(client, report_id, claim)[BILL_MATCH]
        assert stale["status"] == "OPEN" and stale["reason"].endswith(f" [{NOTE}]")
        assert _expense_row(claim).bill_match_status == "REVIEW_REQUIRED" and _expense_row(claim).bill_match_reasons == "The date is unclear."
        assert _statuses_of(report_id, claim)[0] != "VERIFIED" and _statuses_of(report_id, claim)[1] != "ACCOUNT VERIFIED"

    _verify(client, stale, note="second approval")
    for _ in range(3):
        _revalidate(report_id)
        assert _records(client, report_id, claim)[BILL_MATCH]["status"] == "RESOLVED"
        assert _statuses_of(report_id, claim) == ("VERIFIED", "ACCOUNT VERIFIED")


def _seed_confirmed_claims(er_id="ER-77502", s_nos=(4, 11), amount=1150.0, total=None) -> tuple[int, dict[int, int]]:
    """Claims the MATCHER confirmed on one bill (bill_id set): judged by the Duplicate Bill Check, never the Shared Bill review."""
    db = SessionLocal()
    try:
        report = _make_report(db, er_id=er_id, total_claimed=amount * len(s_nos) if total is None else total)
        bill = Bill(expense_report_id=report.id, page_number=15, amount=amount, bill_date=DAY, category_guess="Cab/Taxi", extraction_status="EXTRACTED", match_status="MATCHED")
        db.add(bill)
        db.commit()
        db.refresh(bill)
        ids = {}
        for s_no in s_nos:
            expense = Expense(expense_report_id=report.id, s_no=s_no, expense_date=DAY, category="CAB CHARGES", claimed_amount=amount, bill_id=bill.id,
                              candidate_bill_id=bill.id, bill_amount=amount, bill_date=DAY, bill_found=True, bill_match_status="MATCHED")
            db.add(expense)
            db.commit()
            ids[s_no] = expense.id
        validate_expense_report(db, report.id)
        ee.evaluate_report(db, report.id)
        return report.id, ids
    finally:
        db.close()


def test_genuinely_matcher_confirmed_matches_are_still_judged_by_the_duplicate_check_not_the_shared_bill_review(client):
    report_id, ids = _seed_confirmed_claims()
    types = {e["exception_type"] for e in client.get(f"/api/expense-reports/{report_id}/exceptions").json()}
    assert SHARED not in types, "a confirmed match never gets the shared-bill review"
    assert "DUPLICATE BILL" in types, "the duplicate check judges it instead (the later claim on the same confirmed bill)"
    assert "DUPLICATE BILL" in _records(client, report_id, ids[11]) and not _records(client, report_id, ids[4])
    _revalidate(report_id)
    assert SHARED not in {e["exception_type"] for e in client.get(f"/api/expense-reports/{report_id}/exceptions").json()}


def test_the_shared_bill_review_is_resolved_only_by_its_own_verification_in_either_order(client):
    report_id, ids, _bill = _seed_shared_bill_claims()
    first, second, untouched = ids[4], ids[11], ids[38]

    _verify(client, _records(client, report_id, first)[BILL_MATCH])  # BILL MATCH first ...
    _revalidate(report_id)
    assert _statuses_of(report_id, first)[0] != "VERIFIED", "still not verified: the shared-bill review is unresolved"
    _verify(client, _records(client, report_id, first)[SHARED], note="reviewed: separate bills")  # ... then the shared-bill review itself
    _revalidate(report_id)
    both = _records(client, report_id, first)
    assert both[BILL_MATCH]["status"] == "RESOLVED" and both[SHARED]["status"] == "RESOLVED"
    assert _statuses_of(report_id, first)[0] == "VERIFIED", "verified only once BOTH items are independently resolved"

    _verify(client, _records(client, report_id, second)[SHARED])  # the other order: shared-bill review first
    assert _statuses_of(report_id, second)[0] != "VERIFIED", "the BILL MATCH item is still open"
    _verify(client, _records(client, report_id, second)[BILL_MATCH])
    _revalidate(report_id)
    assert _statuses_of(report_id, second)[0] == "VERIFIED"
    assert all(r["status"] == "RESOLVED" for r in _records(client, report_id, second).values())

    other = _records(client, report_id, untouched)
    assert other[SHARED]["status"] == "OPEN" and other[BILL_MATCH]["status"] == "OPEN", "the third claim was never touched"
    assert _statuses_of(report_id, untouched)[1] != "ACCOUNT VERIFIED", "the report is not verified while a claim is open"


# ================================================================ PART 2: deleting a report retires its verifications

from test_verification_evidence_policy import (  # noqa: E402
    NOTE, ROOM, _audit_events, _legacy, _record, _reevaluate, _seed_report_level, _seed_review, _snapshot, _statuses, _verification,
)
from test_verification_history import _change_evidence, _history, _history_snapshot  # noqa: E402
from app.models.logs import AuditLog  # noqa: E402
from app.models.validation import ExceptionVerification  # noqa: E402
from app.services import audit  # noqa: E402


def _own_source(tmp_path, report_id: int):
    """Point the report at a disposable stored file, so the (existing) file removal on delete can only ever touch the temp dir."""
    path = tmp_path / f"report_{report_id}.pdf"
    path.write_bytes(b"%PDF-1.4 synthetic")
    db = SessionLocal()
    try:
        db.get(ExpenseReport, report_id).source_path = str(path)
        db.commit()
    finally:
        db.close()
    return path


def _delete(client, report_id: int):
    return client.delete(f"/api/documents/expense:{report_id}")


def _live_verifications() -> list[dict]:
    db = SessionLocal()
    try:
        return [{c.name: getattr(v, c.name) for c in ExceptionVerification.__table__.columns} for v in db.query(ExceptionVerification).order_by(ExceptionVerification.id)]
    finally:
        db.close()


def _two_approvals(client, report_id: int) -> None:
    """A first approval, then evidence changes and an explicit re-verification: one live row plus one superseded history row."""
    _verify(client, _record(client, report_id, ROOM), note="approved on the first evidence")
    _change_evidence(report_id, 357.0)
    _verify(client, _record(client, report_id, ROOM), note="approved on the changed evidence")


@pytest.mark.parametrize("legacy", [False, True], ids=["signed", "legacy_unsigned"])
def test_deleting_a_report_keeps_its_verifications_as_history_and_leaves_existing_history_untouched(client, tmp_path, legacy):
    report_id, expense_id = _seed_review()
    stored = _own_source(tmp_path, report_id)
    _two_approvals(client, report_id)
    if legacy:
        _legacy(report_id)  # the live approval is unsigned: it would apply on an equal reason alone
    live = _snapshot(_verification(report_id))
    history_before = [_history_snapshot(h) for h in _history()]
    assert [h["verification_note"] for h in history_before] == ["approved on the first evidence"]

    assert _delete(client, report_id).status_code == 204

    assert not stored.exists(), "the existing file removal still happens"
    assert _live_verifications() == [], "no live verification survives its report"
    history_after = [_history_snapshot(h) for h in _history()]
    assert history_after[:1] == history_before, "the existing history row is exactly as it was"
    assert len(history_after) == 2
    retired = history_after[1]
    for column in ("verification_id", "expense_report_id", "expense_id", "exception_type", "original_severity", "original_reason", "evidence_signature",
                   "verified_by_id", "verified_at", "verification_note"):
        assert retired[column] == (live["id"] if column == "verification_id" else live.get(column, retired[column])), column
    assert retired["verification_note"] == "approved on the changed evidence" and retired["expense_report_id"] == report_id and retired["expense_id"] == expense_id
    assert (retired["evidence_signature"] is None) == legacy and retired["superseded_by_id"] is not None and retired["superseded_at"] is not None
    (event,) = _audit_events(audit.DOCUMENT_DELETED)
    assert f"retired_verification_ids=[{live['id']}]" in event.details and "approved on" not in event.details, "ids only - never note text"


def _recreate_with_reused_ids(client, old_report_id: int, old_claim_id: int, **kwargs) -> tuple[int, int]:
    """A brand-new report built by the REAL flow (validate + evaluate, which also wipes any stale derived rows of a reused report id)
    with exactly the shape - and so the evidence - of the one that was deleted."""
    new_report_id, ids, _bill = _seed_shared_bill_claims("ER-77511", s_nos=(4,), **kwargs)
    assert (new_report_id, ids[4]) == (old_report_id, old_claim_id), "precondition: SQLite handed the same ids back"
    return new_report_id, ids[4]


@pytest.mark.parametrize("legacy", [False, True], ids=["signed", "legacy_unsigned"])
def test_a_report_reusing_the_ids_of_a_deleted_report_cannot_inherit_its_approval(client, tmp_path, legacy):
    report_id, ids, _bill = _seed_shared_bill_claims("ER-77510", s_nos=(4,))
    claim = ids[4]
    _own_source(tmp_path, report_id)
    _verify(client, _records(client, report_id, claim)[BILL_MATCH], note="approved on the deleted report")
    assert _statuses_of(report_id, claim) == ("VERIFIED", "ACCOUNT VERIFIED")
    if legacy:
        _legacy(report_id)  # unsigned: such an approval applies on an equal reason alone
    live = _snapshot(_verification(report_id, any_expense=True))

    assert _delete(client, report_id).status_code == 204
    new_report_id, new_claim = _recreate_with_reused_ids(client, report_id, claim)  # same ids, same evidence, same reasons

    fresh = _records(client, new_report_id, new_claim)[BILL_MATCH]
    assert fresh["status"] == "OPEN" and not fresh["verification_note"] and fresh["verified_at"] is None and NOTE not in fresh["reason"]
    assert _statuses_of(new_report_id, new_claim)[0] != "VERIFIED" and _statuses_of(new_report_id, new_claim)[1] != "ACCOUNT VERIFIED"
    assert _expense_row(new_claim).bill_match_status == "REVIEW_REQUIRED", "the old approval's MATCHED flip was not inherited either"
    _revalidate(new_report_id)
    assert _records(client, new_report_id, new_claim)[BILL_MATCH]["status"] == "OPEN"
    assert _live_verifications() == []
    (kept,) = _history()
    assert kept.verification_note == "approved on the deleted report" and kept.verification_id == live["id"] and kept.expense_report_id == report_id, "the retired record is intact"


def test_a_new_verification_reusing_an_old_verification_id_leaves_the_retired_history_untouched(client, tmp_path):
    report_id, _expense_id = _seed_review()
    _own_source(tmp_path, report_id)
    _verify(client, _record(client, report_id, ROOM), note="the old approval")
    old_id = _verification(report_id).id
    assert _delete(client, report_id).status_code == 204
    retired_before = [_history_snapshot(h) for h in _history()]
    assert [h["verification_note"] for h in retired_before] == ["the old approval"]

    new_report_id, _ = _seed_review(bill_amount=957.0)
    _verify(client, _record(client, new_report_id, ROOM), note="a different approval")
    assert _verification(new_report_id).id == old_id, "precondition: the live table reused the verification id"
    assert [_history_snapshot(h) for h in _history()] == retired_before, "nothing rewrote or added to the retired history"
    assert _record(client, new_report_id, ROOM)["verification_note"] == "a different approval"


def test_report_level_verifications_are_retired_too(client, tmp_path):
    report_id, _ids = _seed_confirmed_claims("ER-77520", s_nos=(4,), total=9999.0)  # a report-level TOTAL MISMATCH from the real checks
    _own_source(tmp_path, report_id)
    record = _record(client, report_id, "TOTAL MISMATCH")
    assert record["expense_id"] is None
    _verify(client, record, note="accepted at report level")
    assert _delete(client, report_id).status_code == 204
    assert _live_verifications() == []
    (kept,) = _history()
    assert kept.expense_id is None and kept.exception_type == "TOTAL MISMATCH" and kept.verification_note == "accepted at report level"

    new_report_id, _ = _seed_confirmed_claims("ER-77521", s_nos=(4,), total=9999.0)
    assert new_report_id == report_id, "precondition: the report id was reused"
    assert _record(client, new_report_id, "TOTAL MISMATCH")["status"] == "OPEN", "the report-level approval did not follow the id"
    assert _statuses_of(new_report_id, _ids[4])[1] != "ACCOUNT VERIFIED"


def test_deleting_one_report_leaves_another_reports_verifications_untouched(client, tmp_path):
    keep_id, _ = _seed_review(er_id="ER-77011")
    gone_id, _ = _seed_review(er_id="ER-77012")
    _own_source(tmp_path, gone_id)
    _verify(client, _record(client, keep_id, ROOM), note="kept approval")
    _verify(client, _record(client, gone_id, ROOM), note="deleted approval")
    keep_before = _snapshot(_verification(keep_id))

    assert _delete(client, gone_id).status_code == 204

    assert _live_verifications() == [keep_before]
    assert [(h.expense_report_id, h.verification_note) for h in _history()] == [(gone_id, "deleted approval")]
    _reevaluate(keep_id)
    assert _record(client, keep_id, ROOM)["status"] == "RESOLVED" and _statuses(keep_id) == ("VERIFIED", "ACCOUNT VERIFIED")


def test_deleting_a_report_without_verifications_writes_no_history_and_the_audit_details_are_unchanged(client, tmp_path):
    report_id, _ = _seed_review()
    _own_source(tmp_path, report_id)
    assert _delete(client, report_id).status_code == 204
    assert _history() == [] and _live_verifications() == []
    (event,) = _audit_events(audit.DOCUMENT_DELETED)
    assert event.details == "filename='ER-77001.pdf'"


def test_a_failed_deletion_changes_nothing_and_a_retry_retires_exactly_once(client, tmp_path, monkeypatch):
    report_id, expense_id = _seed_review()
    stored = _own_source(tmp_path, report_id)
    _two_approvals(client, report_id)
    live_before = _live_verifications()
    history_before = [_history_snapshot(h) for h in _history()]
    real_log_action = audit.log_action

    def failing_audit(*args, **kwargs):
        raise RuntimeError("audit write failed during deletion")

    monkeypatch.setattr(audit, "log_action", failing_audit)
    try:
        assert _delete(client, report_id).status_code == 500
    except RuntimeError:
        pass  # the test client re-raises server errors

    assert stored.exists(), "the stored file is only removed after a successful commit"
    assert _live_verifications() == live_before, "the live verification is exactly as it was"
    assert [_history_snapshot(h) for h in _history()] == history_before, "no half-written history row"
    db = SessionLocal()
    try:
        assert db.get(ExpenseReport, report_id) is not None and db.get(Expense, expense_id) is not None
    finally:
        db.close()
    assert _record(client, report_id, ROOM)["status"] == "RESOLVED"

    monkeypatch.setattr(audit, "log_action", real_log_action)  # the retry
    assert _delete(client, report_id).status_code == 204
    assert _live_verifications() == []
    after = [_history_snapshot(h) for h in _history()]
    assert after[:1] == history_before and len(after) == 2, "exactly one retired row, however many attempts"


# ================================================================ PART 3: authorization


def test_a_viewer_cannot_verify_and_creates_no_verification_or_history_even_when_one_exists(client):
    from fastapi.testclient import TestClient

    from app.main import app
    from test_rbac import _make_role_clients

    report_id, _expense_id = _seed_review()
    roles = _make_role_clients(client)
    client.csrf_token = roles["ADMIN"][1]  # the helper logs the admin in again: the fixture's CSRF header must follow the new session
    viewer, viewer_csrf = roles["VIEWER"]
    record = _record(client, report_id, ROOM)

    denied = viewer.post(f"/api/exceptions/{record['id']}/verify", json={"note": "viewer note"}, headers={"X-CSRF-Token": viewer_csrf})
    assert denied.status_code == 403
    assert _live_verifications() == [] and _history() == [] and _audit_events(audit.EXCEPTION_VERIFIED) == []
    assert _record(client, report_id, ROOM)["status"] == "OPEN" and _statuses(report_id) == ("REVIEW_REQUIRED", "REVIEW REQUIRED")

    anonymous = TestClient(app).post(f"/api/exceptions/{record['id']}/verify", json={"note": "anonymous"})
    assert anonymous.status_code in (401, 403)
    assert _live_verifications() == [] and _history() == []

    _verify(client, record, note="admin approved")  # now an approval exists, and goes stale
    _change_evidence(report_id, 357.0)
    stale = _record(client, report_id, ROOM)
    before = _live_verifications()
    for body in ({"note": "viewer re-verification"}, {}):
        assert viewer.post(f"/api/exceptions/{stale['id']}/verify", json=body, headers={"X-CSRF-Token": viewer_csrf}).status_code == 403
    assert _live_verifications() == before and _history() == [], "a refused re-verification writes no history"
    assert [e.details for e in _audit_events(audit.EXCEPTION_VERIFIED)] == [f"{ROOM} on expense_id={record['expense_id']} (report {report_id}); note recorded"]


def test_history_records_which_role_replaced_which_approval(client):
    from app.models.user import User
    from test_rbac import _make_role_clients

    report_id, _expense_id = _seed_review()
    roles = _make_role_clients(client)
    client.csrf_token = roles["ADMIN"][1]
    auditor, auditor_csrf = roles["AUDITOR"]
    record = _record(client, report_id, ROOM)
    assert auditor.post(f"/api/exceptions/{record['id']}/verify", json={"note": "auditor note"}, headers={"X-CSRF-Token": auditor_csrf}).status_code == 200
    _change_evidence(report_id, 357.0)
    _verify(client, _record(client, report_id, ROOM), note="admin note")  # the admin replaces the auditor's approval

    db = SessionLocal()
    try:
        auditor_id = db.query(User).filter(User.email == "rbac-auditor@spincotech.com").one().id
        admin_id = db.query(User).filter(User.email == "intern@spincotech.com").one().id
    finally:
        db.close()
    (kept,) = _history()
    assert kept.verification_note == "auditor note" and kept.verified_by_id == auditor_id and kept.superseded_by_id == admin_id
    assert _verification(report_id).verified_by_id == admin_id


# ================================================================ PART 3: BILL MATCH and TRIP MATCH through the real validation flow


def test_a_bill_match_approval_is_stable_through_real_revalidation(client):
    report_id, ids, _bill = _seed_shared_bill_claims("ER-77601", s_nos=(4,))
    claim = ids[4]
    records = _records(client, report_id, claim)
    assert set(records) == {BILL_MATCH}, "fixture: a single claim has a BILL MATCH review and no shared-bill review"
    _verify(client, records[BILL_MATCH], note="date confirmed by hand")
    approved = _expense_row(claim)
    signature = _verification(report_id).evidence_signature

    for _ in range(3):
        _revalidate(report_id)

    assert _records(client, report_id, claim)[BILL_MATCH]["status"] == "RESOLVED"
    assert _records(client, report_id, claim)[BILL_MATCH]["verification_note"] == "date confirmed by hand"
    assert _statuses_of(report_id, claim) == ("VERIFIED", "ACCOUNT VERIFIED")
    after = _expense_row(claim)
    assert after.bill_match_status == "MATCHED" and after.bill_match_reasons == approved.bill_match_reasons == f"{MANUAL_PREFIX} - The date is unclear."
    assert _verification(report_id).evidence_signature == signature, "no flapping: the real checks reproduce the approved evidence"


def test_a_bill_match_approval_goes_stale_through_real_revalidation_when_the_review_evidence_changes(client):
    report_id, ids, bill_id = _seed_shared_bill_claims("ER-77602", s_nos=(4,))
    claim = ids[4]
    _verify(client, _records(client, report_id, claim)[BILL_MATCH], note="date confirmed by hand")
    assert _statuses_of(report_id, claim) == ("VERIFIED", "ACCOUNT VERIFIED")

    db = SessionLocal()
    try:
        db.get(Bill, bill_id).page_number = 16  # the page the review points at changed
        db.commit()
    finally:
        db.close()
    for _ in range(2):
        _revalidate(report_id)

    stale = _records(client, report_id, claim)[BILL_MATCH]
    assert stale["status"] == "OPEN" and stale["reason"].endswith(f" [{NOTE}]")
    assert _expense_row(claim).bill_match_status == "REVIEW_REQUIRED" and _expense_row(claim).bill_match_reasons == "The date is unclear."
    assert _statuses_of(report_id, claim)[0] != "VERIFIED" and _statuses_of(report_id, claim)[1] != "ACCOUNT VERIFIED"


def test_a_bill_match_approval_whose_evidence_turns_into_a_confirmed_exception_never_verifies_the_claim(client):
    report_id, ids, bill_id = _seed_shared_bill_claims("ER-77603", s_nos=(4,))
    claim = ids[4]
    _verify(client, _records(client, report_id, claim)[BILL_MATCH], note="date confirmed by hand")
    db = SessionLocal()
    try:
        db.get(Bill, bill_id).amount = 999.0  # the bill no longer supports the claim
        db.commit()
    finally:
        db.close()
    _revalidate(report_id)
    records = _records(client, report_id, claim)
    assert records["AMOUNT MISMATCH"]["status"] == "OPEN"
    assert _statuses_of(report_id, claim)[0] == "EXCEPTION" and _statuses_of(report_id, claim)[1] == "EXCEPTION"


def _seed_batta(er_id="ER-77701", status="MULTIPLE_CANDIDATES", expense_date=date(2026, 7, 24), amount=1000.0) -> tuple[int, int]:
    db = SessionLocal()
    try:
        report = _make_report(db, er_id=er_id, total_claimed=amount)
        expense = Expense(expense_report_id=report.id, s_no=1, expense_date=expense_date, category="BATTA", claimed_amount=amount,
                          bill_match_status="NOT_APPLICABLE", trip_match_status=status, trip_report_id=None,
                          trip_match_reasons="Multiple trips are plausible matches; auditor review required.")
        db.add(expense)
        db.commit()
        validate_expense_report(db, report.id)
        ee.evaluate_report(db, report.id)
        return report.id, expense.id
    finally:
        db.close()


TRIP_MATCH = "TRIP MATCH REVIEW REQUIRED"


@pytest.mark.parametrize("status", ["REVIEW_REQUIRED", "MULTIPLE_CANDIDATES"])
def test_a_trip_match_approval_is_stable_through_real_revalidation(client, status):
    report_id, claim = _seed_batta(status=status)
    records = _records(client, report_id, claim)
    assert set(records) == {TRIP_MATCH}, f"fixture: only the trip review is open, got {set(records)}"
    _verify(client, records[TRIP_MATCH], note="trip confirmed by hand")
    approved = _expense_row(claim)
    assert approved.trip_match_status == "MATCHED" and approved.trip_match_reasons.startswith(MANUAL_PREFIX)
    signature = _verification(report_id).evidence_signature

    for _ in range(3):
        _revalidate(report_id)

    assert _records(client, report_id, claim)[TRIP_MATCH]["status"] == "RESOLVED"
    assert _statuses_of(report_id, claim) == ("VERIFIED", "ACCOUNT VERIFIED")
    after = _expense_row(claim)
    assert after.trip_match_status == "MATCHED" and after.trip_match_reasons == approved.trip_match_reasons, "prefix applied once"
    assert _verification(report_id).evidence_signature == signature, "no flapping"


def test_a_trip_match_approval_goes_stale_through_real_revalidation_and_cannot_leave_the_trip_matched(client):
    report_id, claim = _seed_batta()
    _verify(client, _records(client, report_id, claim)[TRIP_MATCH], note="trip confirmed by hand")
    assert _statuses_of(report_id, claim) == ("VERIFIED", "ACCOUNT VERIFIED")

    db = SessionLocal()
    try:
        db.get(Expense, claim).expense_date = date(2026, 7, 25)  # the claim's own date changed
        db.commit()
    finally:
        db.close()
    for _ in range(2):
        _revalidate(report_id)

    stale = _records(client, report_id, claim)[TRIP_MATCH]
    assert stale["status"] == "OPEN" and stale["reason"].endswith(f" [{NOTE}]")
    after = _expense_row(claim)
    assert after.trip_match_status == "REVIEW_REQUIRED", "an approval that no longer applies cannot leave the trip MATCHED"
    assert after.trip_match_reasons == "Multiple trips are plausible matches; auditor review required."
    assert _statuses_of(report_id, claim)[0] != "VERIFIED" and _statuses_of(report_id, claim)[1] != "ACCOUNT VERIFIED"

    _verify(client, stale, note="re-checked on the new date")
    assert _statuses_of(report_id, claim) == ("VERIFIED", "ACCOUNT VERIFIED") and _expense_row(claim).trip_match_status == "MATCHED"


# ================================================================ a BILL MATCH approval overtaken by other evidence: what the MATCHED label does and does not mean


def _checks_of(client, report_id: int, expense_id: int) -> list[tuple]:
    rows = client.get(f"/api/expense-reports/{report_id}/validations").json()
    return sorted((r["check_type"], r["result"], r["exception_type"]) for r in rows if r["expense_id"] == expense_id)


def _change_bill(bill_id: int, **fields) -> None:
    db = SessionLocal()
    try:
        row = db.get(Bill, bill_id)
        for name, value in fields.items():
            setattr(row, name, value)
        db.commit()
    finally:
        db.close()


OVERTAKEN = {
    "amount_mismatch": ({"amount": 999.0}, {"AMOUNT MISMATCH"}),
    "date_mismatch": ({"bill_date": date(2026, 9, 20)}, {"DATE MISMATCH"}),
    "evidence_now_clean": ({"bill_date": DAY}, set()),
}


@pytest.mark.parametrize("mode", list(OVERTAKEN))
def test_a_bill_match_approval_overtaken_by_other_evidence_leaves_a_manual_matched_label_that_no_logic_treats_as_a_real_match(client, mode):
    """OBSERVED BEHAVIOUR (pinned). After a BILL MATCH approval, the evidence changes so that the family's review record no longer
    exists (a confirmed BILL-rooted exception suppresses it, or the review rows simply pass). The approval cannot be applied, so the
    claim is not verified - but there is no OPEN BILL MATCH record to trigger the status revert, so bill_match_status keeps the
    manual 'MATCHED' flip and its reasons prefix. This test pins that, and proves nothing downstream differs from a claim that was
    never approved: the validation rows, exceptions and statuses are identical to an unapproved control."""
    change, expected_exceptions = OVERTAKEN[mode]
    report_id, ids, bill_id = _seed_shared_bill_claims(f"ER-778{len(mode)}", s_nos=(4,))
    claim = ids[4]
    _verify(client, _records(client, report_id, claim)[BILL_MATCH], note="date confirmed by hand")
    assert _statuses_of(report_id, claim) == ("VERIFIED", "ACCOUNT VERIFIED")
    approved_signature = _verification(report_id).evidence_signature

    _change_bill(bill_id, **change)
    _revalidate(report_id)
    _revalidate(report_id)

    records = _records(client, report_id, claim)
    assert BILL_MATCH not in records, "no BILL MATCH record exists any more, so there is nothing for the approval to resolve"
    assert set(records) == expected_exceptions and all(r["status"] == "OPEN" for r in records.values())
    expense_status, report_status = _statuses_of(report_id, claim)
    assert expense_status == ("PASS" if mode == "evidence_now_clean" else "EXCEPTION"), "never VERIFIED by the vanished approval"
    assert (report_status == "ACCOUNT VERIFIED") == (mode == "evidence_now_clean"), "only a report whose checks all genuinely pass is verified"
    assert _verification(report_id).evidence_signature == approved_signature, "the verification row itself is untouched"

    # the label
    after = _expense_row(claim)
    assert after.bill_match_status == "MATCHED" and after.bill_match_reasons == f"{MANUAL_PREFIX} - The date is unclear."
    assert after.bill_id is None and after.bill_found is not True, "but no bill is confirmed: the manual flip never sets one"
    api = client.get(f"/api/expenses/{claim}/bill-match").json()
    assert api["bill_match_status"] == "MATCHED" and api["matched_bill"] is None, "the API shows the label with no matched bill"

    # a never-approved control with the same evidence: the validation outcome is identical
    control_id, control_ids, control_bill = _seed_shared_bill_claims("ER-77899", s_nos=(4,))
    _change_bill(control_bill, **change)
    _revalidate(control_id)
    control = control_ids[4]
    assert _expense_row(control).bill_match_status == "REVIEW_REQUIRED"
    assert _checks_of(client, report_id, claim) == _checks_of(client, control_id, control), "no check reads the manual flip differently"
    assert set(_records(client, control_id, control)) == set(records)
    assert _statuses_of(control_id, control) == _statuses_of(report_id, claim)

    # the only place the label is consumed as a count: analytics groups by the status
    by_status = {p["status"]: p["count"] for p in client.get("/api/analytics/bills").json()["by_status"]}
    assert by_status == {"MATCHED": 1, "REVIEW_REQUIRED": 1}, "the overtaken claim is counted as MATCHED, the control as REVIEW_REQUIRED"


def test_when_the_overtaking_evidence_returns_to_what_was_approved_the_approval_applies_again_without_stacking_the_prefix(client):
    report_id, ids, bill_id = _seed_shared_bill_claims("ER-77810", s_nos=(4,))
    claim = ids[4]
    _verify(client, _records(client, report_id, claim)[BILL_MATCH], note="date confirmed by hand")
    approved = _expense_row(claim)
    _change_bill(bill_id, amount=999.0)
    _revalidate(report_id)
    assert set(_records(client, report_id, claim)) == {"AMOUNT MISMATCH"}

    _change_bill(bill_id, amount=1150.0)  # the evidence is what it was when approved
    _revalidate(report_id)

    assert _records(client, report_id, claim)[BILL_MATCH]["status"] == "RESOLVED"
    assert _statuses_of(report_id, claim) == ("VERIFIED", "ACCOUNT VERIFIED")
    assert _expense_row(claim).bill_match_reasons == approved.bill_match_reasons, "the prefix is not stacked"


# ================================================================ the documented read-only SQL checks for legacy orphaned verifications


def _documented_statement(starts_with: str) -> str:
    import pathlib
    import re

    docs = pathlib.Path(__file__).resolve().parent.parent.parent / "docs" / "RELEASE_AND_UPDATES.md"
    match = re.search(re.escape(starts_with) + r".*?;", docs.read_text(encoding="utf-8"), re.S)
    assert match, f"the documented statement starting {starts_with!r} was not found"
    return " ".join(match.group(0).split())


ORPHAN_SQL_START = "SELECT v.id, v.expense_report_id, v.expense_id, v.exception_type\n  FROM exception_verifications v\n  LEFT JOIN"
REUSED_SQL_START = "SELECT v.id, v.expense_report_id, v.expense_id, v.exception_type, v.verified_at, r.uploaded_at"


def test_the_documented_orphan_queries_find_exactly_the_legacy_rows_and_are_read_only(tmp_path):
    import sqlite3
    from datetime import datetime
    from pathlib import Path

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app import database
    from app.database import Base
    from app.models.user import User
    from app.models.validation import ExceptionVerification

    db_file = tmp_path / "legacy_orphans.db"
    assert tmp_path.resolve() in db_file.resolve().parents and db_file.resolve() != Path(database.DB_PATH).resolve(), "a disposable database only"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    uploaded, approved = datetime(2026, 2, 1), datetime(2026, 3, 1)  # an approval is always made after its report was uploaded

    user = User(name="Orphan", email="orphan@example.com", role="ADMIN")
    session.add(user)
    session.flush()
    verification_ids = {}
    for report_id in (1, 2):  # report 1 stays healthy; report 2 is deleted the way the old delete route did
        session.add(ExpenseReport(id=report_id, filename=f"r{report_id}.pdf", original_filename=f"r{report_id}.pdf", file_hash=f"orphan-{report_id}", file_size=1,
                                  source_path="unused.pdf", extraction_status="EXTRACTED", uploaded_at=uploaded))
        expense = Expense(expense_report_id=report_id, s_no=1, expense_date=DAY, category="LODGING", claimed_amount=1.0)
        session.add(expense)
        session.flush()
        verification = ExceptionVerification(expense_report_id=report_id, expense_id=expense.id, exception_type="BILL MATCH REVIEW REQUIRED",
                                             original_severity="REVIEW REQUIRED", original_reason="r", verified_by_id=user.id, verified_at=approved)
        session.add(verification)
        session.flush()
        verification_ids[report_id] = verification.id
    session.commit()
    session.close()
    with engine.begin() as conn:  # the legacy deletion: the report and its expenses go, its verification stays
        conn.execute(text("DELETE FROM expenses WHERE expense_report_id = 2"))
        conn.execute(text("DELETE FROM expense_reports WHERE id = 2"))

    orphan_sql, reused_sql = _documented_statement(ORPHAN_SQL_START), _documented_statement(REUSED_SQL_START)
    for statement in (orphan_sql, reused_sql):
        words = statement.upper().replace(",", " ").split()
        assert words[0] == "SELECT" and not {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE"} & set(words)

    def run(sql):
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)  # a read-only connection: any write would raise
        try:
            conn.execute("PRAGMA query_only = ON")
            return [tuple(r) for r in conn.execute(sql)]
        finally:
            conn.close()

    # state 1: report 2 is gone and its id is not reused -> the first query finds exactly that row
    assert [r[0] for r in run(orphan_sql)] == [verification_ids[2]], "exactly the orphan; the healthy report's approval is not listed"
    assert run(reused_sql) == []

    # state 2: a NEWER report reuses id 2 -> the first query is blind to it, the second finds it
    reuse = sessionmaker(bind=engine, autoflush=False)()
    reuse.add(ExpenseReport(id=2, filename="new.pdf", original_filename="new.pdf", file_hash="orphan-new", file_size=1, source_path="unused.pdf",
                            extraction_status="EXTRACTED", uploaded_at=datetime(2026, 4, 1)))
    reuse.commit()
    reuse.close()
    assert run(orphan_sql) == [], "the documented limit: once the id is reused the row no longer looks orphaned"
    assert [(r[0], r[1]) for r in run(reused_sql)] == [(verification_ids[2], 2)], "the inherited approval predates the report it is now attached to"
    assert len(run("SELECT id FROM exception_verifications")) == 2, "running either query changed nothing"