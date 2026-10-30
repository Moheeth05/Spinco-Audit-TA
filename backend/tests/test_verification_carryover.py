"""A manual verification must follow its CLAIM through a full reprocess.

Full /reprocess deletes and recreates a report's Expense rows (new ids). These tests pin the
policy implemented in app/services/extraction/verification_carryover.py:

  * confident match (same non-NULL s_no; date, normalized category and amount present and equal)
    -> the existing verification row is re-keyed to the new expense, nothing else about it changes;
  * anything else (changed / missing facts, no row for the s_no, ambiguous) -> the row is left exactly
    as it is, the regenerated claim stays OPEN, and a system audit event says why;
  * never across reports; report-level verifications are untouched;
  * a failure inside carry-over rolls the whole replacement back (nothing partial is committed).

Every reprocessing test uses TWO reports in the disposable database, and asserts the id precondition
it depends on: SQLite hands freed ids straight back when the reprocessed report owns the highest ids,
which hides the defect (and would hide a regression), so "older report reprocessed" and "newest report
reprocessed" are both covered and both state which one they are.

All documents are SYNTHETIC (tests/synthetic_reports.py); the database and storage are the temp ones
conftest.py configures. Nothing here touches a real database or an authentic document.
"""

from datetime import date
from decimal import Decimal

import pytest

import synthetic_reports as sr
from app import database
from app.database import SessionLocal
from app.models.expense_report import Expense, ExpenseReport
from app.models.logs import AuditLog, ProcessingLog
from app.models.user import User
from app.models.validation import ExceptionVerification
from app.services import audit
from app.services.extraction import verification_carryover as vc

CAB = "CAB CHARGES"


# ---------------------------------------------------------------- safety


def test_the_test_database_is_the_disposable_one():
    assert "spinco_test_" in str(database.DB_PATH), "the suite must run on the temp database set up by conftest.py"


# ---------------------------------------------------------------- synthetic documents and helpers


def _case(er_id: str) -> sr.Case:
    """Three claims, no supporting receipts: each one gets a confirmed MISSING BILL exception."""
    claims = [
        sr.Claim(s_no, date(2026, 7, 10 + s_no), CAB, "Local travel", "Vendor", amount, expect="UNSUPPORTED", expect_exception="MISSING BILL")
        for s_no, amount in ((1, 410.0), (2, 520.0), (3, 630.0))
    ]
    return sr.Case("verification_carryover", "dev", "L1", claims, [], er_id=er_id)


def _upload(client, er_id: str) -> int:
    pdf, _ = sr.render_er(_case(er_id))
    resp = client.post("/api/uploads/expense-reports", files=[("files", (f"{er_id}.pdf", pdf, "application/pdf"))])
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["success"] is True, result
    return int(result["document_id"].split(":")[1])


def _expense_ids(report_id: int) -> dict:
    db = SessionLocal()
    try:
        return {e.s_no: e.id for e in db.query(Expense).filter(Expense.expense_report_id == report_id)}
    finally:
        db.close()


def _exceptions(client, report_id: int) -> list[dict]:
    resp = client.get(f"/api/expense-reports/{report_id}/exceptions")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _missing_bill_by_s_no(client, report_id: int) -> dict:
    by_id = {expense_id: s_no for s_no, expense_id in _expense_ids(report_id).items()}
    found = {}
    for record in _exceptions(client, report_id):
        if record["exception_type"] == "MISSING BILL" and record["expense_id"] in by_id:
            found[by_id[record["expense_id"]]] = record
    return found


def _verify(client, report_id: int, s_no: int) -> None:
    record = _missing_bill_by_s_no(client, report_id)[s_no]
    resp = client.post(f"/api/exceptions/{record['id']}/verify", json={"note": f"accepted s_no {s_no} (report {report_id})"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "RESOLVED"


def _verifications(report_id: int) -> list[dict]:
    db = SessionLocal()
    try:
        rows = db.query(ExceptionVerification).filter(ExceptionVerification.expense_report_id == report_id).order_by(ExceptionVerification.id).all()
        return [
            {"id": v.id, "expense_id": v.expense_id, "type": v.exception_type, "by": v.verified_by_id, "at": v.verified_at, "note": v.verification_note,
             "severity": v.original_severity, "reason": v.original_reason}
            for v in rows
        ]
    finally:
        db.close()


def _audit_events(action: str) -> list[AuditLog]:
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.action == action).order_by(AuditLog.id).all()
        for row in rows:
            db.expunge(row)
        return rows
    finally:
        db.close()


def _edit_old_expense(report_id: int, claim_s_no: int, **fields) -> None:
    db = SessionLocal()
    try:
        expense = db.query(Expense).filter(Expense.expense_report_id == report_id, Expense.s_no == claim_s_no).one()
        for name, value in fields.items():
            setattr(expense, name, value)
        db.commit()
    finally:
        db.close()


def _reprocess(client, report_id: int) -> None:
    resp = client.post(f"/api/documents/expense:{report_id}/reprocess")
    assert resp.status_code == 200, resp.text


def _report_state(client, report_id: int) -> dict:
    return {
        "expense_ids": _expense_ids(report_id),
        "verifications": _verifications(report_id),
        "exceptions": sorted((e["expense_id"], e["exception_type"], e["status"], e["verification_note"]) for e in _exceptions(client, report_id)),
    }


@pytest.fixture
def older_target(client):
    """A = the OLDER report (lower ids) that gets reprocessed; B = a newer report that keeps higher ids allocated."""
    report_a = _upload(client, "ER-75001")
    report_b = _upload(client, "ER-75002")
    assert max(_expense_ids(report_a).values()) < min(_expense_ids(report_b).values()), "fixture: B must hold the higher ids"
    return report_a, report_b


# ================================================================ 1. confident match, several verifications


def test_confident_match_moves_each_verification_to_its_claims_new_expense(client, older_target):
    report_a, report_b = older_target
    _verify(client, report_a, 2)
    _verify(client, report_a, 3)
    before_ids = _expense_ids(report_a)
    before = _verifications(report_a)
    before_b = _report_state(client, report_b)
    assert [v["expense_id"] for v in before] == [before_ids[2], before_ids[3]]

    _reprocess(client, report_a)

    after_ids = _expense_ids(report_a)
    assert set(after_ids.values()).isdisjoint(before_ids.values()), "precondition: the expenses really were recreated with NEW ids"
    after = _verifications(report_a)
    assert len(after) == 2, "no duplicate, no deletion"
    for old, new, s_no in zip(before, after, (2, 3)):
        assert new["expense_id"] == after_ids[s_no], "the verification follows ITS claim"
        # everything else about the historical row is exactly as it was
        assert {k: v for k, v in new.items() if k != "expense_id"} == {k: v for k, v in old.items() if k != "expense_id"}
    assert len({v["expense_id"] for v in after}) == 2, "exactly one verification per claim"

    records = _missing_bill_by_s_no(client, report_a)
    assert records[2]["status"] == "RESOLVED" and records[2]["verification_note"] == "accepted s_no 2 (report %d)" % report_a
    assert records[3]["status"] == "RESOLVED" and records[3]["verification_note"] == "accepted s_no 3 (report %d)" % report_a
    assert records[1]["status"] != "RESOLVED", "the claim nobody verified stays open"

    events = _audit_events(audit.VERIFICATION_CARRIED_OVER)
    assert len(events) == 2 and all(e.user_id is None and e.entity_type == "ExceptionVerification" for e in events)
    assert sorted(e.entity_id for e in events) == sorted(str(v["id"]) for v in after)
    for event, row, s_no in zip(events, after, (2, 3)):
        assert f"report_id={report_a}" in event.details and f"verification_id={row['id']}" in event.details
        assert f"old_expense_id={before_ids[s_no]}" in event.details and f"new_expense_id={after_ids[s_no]}" in event.details
        assert "accepted s_no" not in event.details, "the verification note is never copied into the audit text"
    assert _audit_events(audit.VERIFICATION_NOT_CARRIED_OVER) == []

    assert _report_state(client, report_b) == before_b, "the other report is untouched"


def test_a_second_reprocess_does_not_duplicate_anything(client, older_target):
    report_a, _report_b = older_target
    _verify(client, report_a, 2)
    _reprocess(client, report_a)
    once = _verifications(report_a)
    events_once = len(_audit_events(audit.VERIFICATION_CARRIED_OVER))
    _reprocess(client, report_a)  # A is now the newest report: SQLite reuses the ids, so there is nothing to move
    assert _verifications(report_a) == [{**once[0], "expense_id": _expense_ids(report_a)[2]}]
    assert len(_audit_events(audit.VERIFICATION_CARRIED_OVER)) == events_once == 1
    assert _missing_bill_by_s_no(client, report_a)[2]["status"] == "RESOLVED"


# ================================================================ 2-5. changed / missing facts, no row for the s_no


def _assert_left_historical(client, report_a, report_b, before_verifications, verified_s_no, reason, changed=None):
    """The verification of `verified_s_no` stayed exactly as it was and its (regenerated) claim is OPEN."""
    after = {v["id"]: v for v in _verifications(report_a)}
    for old in before_verifications:
        if old["id"] in {v["id"] for v in before_verifications if v["expense_id"] == before_verifications_by_s_no[verified_s_no]}:
            assert after[old["id"]] == old, "the historical verification row is untouched"
    new_ids = _expense_ids(report_a)
    assert new_ids[verified_s_no] not in {v["expense_id"] for v in after.values()}, "no verification was attached to the regenerated claim"
    assert _missing_bill_by_s_no(client, report_a)[verified_s_no]["status"] != "RESOLVED", "a changed claim is never automatically RESOLVED"
    events = [e for e in _audit_events(audit.VERIFICATION_NOT_CARRIED_OVER) if f"report_id={report_a}" in e.details]
    assert len(events) == 1 and e_reason(events[0]) == reason and events[0].user_id is None
    if changed:
        assert f"changed={changed}" in events[0].details
    assert all(f"report_id={report_b}" not in e.details for e in _audit_events(audit.VERIFICATION_NOT_CARRIED_OVER))


before_verifications_by_s_no: dict = {}


def e_reason(event: AuditLog) -> str:
    return next(part.split("=", 1)[1] for part in event.details.split("; ") if part.startswith("reason="))


@pytest.mark.parametrize(
    "field, new_value, changed",
    [("claimed_amount", Decimal("999.00"), "amount"), ("expense_date", date(2026, 1, 1), "date"), ("category", "LODGING", "category")],
    ids=["amount", "date", "category"],
)
def test_a_changed_fact_leaves_the_verification_historical_and_the_claim_open(client, older_target, field, new_value, changed):
    report_a, report_b = older_target
    _verify(client, report_a, 2)
    _verify(client, report_a, 3)
    old_ids = _expense_ids(report_a)
    before = _verifications(report_a)
    before_verifications_by_s_no.clear()
    before_verifications_by_s_no.update({2: old_ids[2], 3: old_ids[3]})
    _edit_old_expense(report_a, 2, **{field: new_value})  # the OLD row no longer agrees with what re-extraction will produce
    before_b = _report_state(client, report_b)

    _reprocess(client, report_a)

    assert set(_expense_ids(report_a).values()).isdisjoint(old_ids.values()), "precondition: new ids"
    _assert_left_historical(client, report_a, report_b, before, 2, vc.FACTS_CHANGED, changed)
    # the unchanged claim still carries its verification
    moved = [v for v in _verifications(report_a) if v["id"] == before[1]["id"]][0]
    assert moved["expense_id"] == _expense_ids(report_a)[3]
    assert _missing_bill_by_s_no(client, report_a)[3]["status"] == "RESOLVED"
    assert _report_state(client, report_b) == before_b


@pytest.mark.parametrize(
    "field, value",
    [("expense_date", None), ("category", None), ("claimed_amount", None), ("s_no", None)],
    ids=["NULL-date", "NULL-category", "NULL-amount", "NULL-s_no"],
)
def test_a_missing_fact_is_never_a_match(client, older_target, field, value):
    report_a, report_b = older_target
    _verify(client, report_a, 2)
    old_ids = _expense_ids(report_a)
    before = _verifications(report_a)
    before_verifications_by_s_no.clear()
    before_verifications_by_s_no.update({2: old_ids[2]})
    _edit_old_expense(report_a, 2, **{field: value})

    _reprocess(client, report_a)

    assert set(_expense_ids(report_a).values()).isdisjoint(old_ids.values())
    _assert_left_historical(client, report_a, report_b, before, 2, vc.MISSING_FACTS)


def test_no_row_for_the_s_no_leaves_the_verification_historical(client, older_target):
    report_a, report_b = older_target
    _verify(client, report_a, 2)
    old_ids = _expense_ids(report_a)
    before = _verifications(report_a)
    before_verifications_by_s_no.clear()
    before_verifications_by_s_no.update({2: old_ids[2]})
    _edit_old_expense(report_a, 2, s_no=99)  # re-extraction yields s_no 1,2,3 - there is no row 99

    _reprocess(client, report_a)

    assert set(_expense_ids(report_a).values()).isdisjoint(old_ids.values())
    _assert_left_historical(client, report_a, report_b, before, 2, vc.NO_ROW_FOR_S_NO)


# ================================================================ 6. never across reports


def test_a_verification_never_follows_or_lands_on_an_identical_claim_of_another_report(client, older_target):
    """Report B holds claims identical in s_no, date, category and amount to A's. Whatever happens to A's
    verifications, B never receives one, and B's own claims stay open."""
    report_a, report_b = older_target
    _verify(client, report_a, 2)
    _verify(client, report_a, 3)
    old_ids = _expense_ids(report_a)
    _edit_old_expense(report_a, 3, claimed_amount=Decimal("1.00"))  # 2 follows its claim, 3 does not
    before_b = _report_state(client, report_b)

    _reprocess(client, report_a)

    b_ids = set(_expense_ids(report_b).values())
    assert set(_expense_ids(report_a).values()).isdisjoint(old_ids.values())
    assert all(v["expense_id"] not in b_ids for v in _verifications(report_a)), "no verification of A points at one of B's expenses"
    assert _verifications(report_b) == []
    assert all(r["status"] != "RESOLVED" for r in _missing_bill_by_s_no(client, report_b).values()), "B's identical claims remain open"
    assert _report_state(client, report_b) == before_b


# ================================================================ 7. ambiguous match (the DB's UNIQUE(report, s_no) forbids it, so the pure planner is tested)


def _facts(s_no, day=date(2026, 7, 12), category=CAB, amount="520.00"):
    return vc.ClaimFacts(s_no, day, category, Decimal(amount))


def _new(s_no, day=date(2026, 7, 12), category=CAB, amount="520.00"):
    return Expense(s_no=s_no, expense_date=day, category=category, claimed_amount=Decimal(amount))


def test_two_new_rows_with_the_same_s_no_are_an_ambiguous_match():
    outcome = vc.plan(vc.Snapshot({5: _facts(2)}, {5}), [_new(2), _new(2)]).outcomes[0]
    assert outcome.carry_to is None and outcome.reason == vc.AMBIGUOUS_MATCH


def test_two_old_claims_may_never_share_one_new_expense():
    snap = vc.Snapshot({5: _facts(2), 6: _facts(2)}, {5, 6})
    outcomes = vc.plan(snap, [_new(2)]).outcomes
    assert [o.reason for o in outcomes] == [vc.AMBIGUOUS_MATCH, vc.AMBIGUOUS_MATCH] and all(o.carry_to is None for o in outcomes)


def test_the_planner_decisions_in_isolation():
    new = [_new(1), _new(2), _new(3, amount="999.00"), _new(4, category=None), _new(5, day=None)]
    snap = vc.Snapshot(
        {1: _facts(1), 2: _facts(2, amount="521.00"), 3: _facts(3), 4: _facts(4), 5: _facts(5), 6: _facts(6), 7: _facts(None)},
        set(range(1, 8)),
    )
    got = {o.old_expense_id: (o.reason, o.changed) for o in vc.plan(snap, new).outcomes}
    assert got[1] == (None, ())
    assert got[2] == (vc.FACTS_CHANGED, ("amount",))
    assert got[3] == (vc.FACTS_CHANGED, ("amount",))
    assert got[4][0] == vc.MISSING_FACTS and got[5][0] == vc.MISSING_FACTS  # a NEW row missing a fact is not a match either
    assert got[6][0] == vc.NO_ROW_FOR_S_NO and got[7][0] == vc.MISSING_FACTS


def test_amounts_compare_as_decimals_to_two_places_never_as_floats():
    assert vc.plan(vc.Snapshot({1: _facts(1, amount="0.30")}, {1}), [_new(1, amount="0.3")]).outcomes[0].carry_to is not None
    near = vc.plan(vc.Snapshot({1: _facts(1, amount="0.30")}, {1}), [_new(1, amount="0.31")]).outcomes[0]
    assert near.carry_to is None and near.changed == ("amount",)
    assert vc.facts_of(Expense(s_no=1, claimed_amount=0.1 + 0.2)).amount == Decimal("0.30")  # binary float noise is normalised away


def test_category_comparison_uses_the_normalized_category():
    noisy = Expense(s_no=1, expense_date=date(2026, 7, 11), category="CAB CHARGES [1]", claimed_amount=Decimal("1.00"))
    assert vc.facts_of(noisy).category == CAB


# ================================================================ 8. report-level verification untouched


def test_a_report_level_verification_is_never_touched(client, older_target):
    report_a, _report_b = older_target
    _verify(client, report_a, 2)
    db = SessionLocal()
    try:
        db.add(ExceptionVerification(expense_report_id=report_a, expense_id=None, exception_type="REPORT TOTAL REVIEW REQUIRED",
                                     original_severity="REVIEW REQUIRED", original_reason="totals differ", verified_by_id=db.query(User).first().id,
                                     verification_note="report level"))
        db.commit()
    finally:
        db.close()
    report_level_before = [v for v in _verifications(report_a) if v["expense_id"] is None]
    assert len(report_level_before) == 1

    _reprocess(client, report_a)

    assert [v for v in _verifications(report_a) if v["expense_id"] is None] == report_level_before
    assert all(str(report_level_before[0]["id"]) != e.entity_id for e in _audit_events(audit.VERIFICATION_CARRIED_OVER) + _audit_events(audit.VERIFICATION_NOT_CARRIED_OVER))


# ================================================================ 9. newest-report control: SQLite reuses the ids


def test_control_reprocessing_the_newest_report_where_sqlite_reuses_ids(client):
    report_b = _upload(client, "ER-75102")  # older
    report_a = _upload(client, "ER-75101")  # NEWEST: it owns the highest ids
    assert max(_expense_ids(report_b).values()) < min(_expense_ids(report_a).values())
    _verify(client, report_a, 2)
    old_ids = _expense_ids(report_a)
    before = _verifications(report_a)

    _reprocess(client, report_a)

    assert _expense_ids(report_a) == old_ids, "precondition: the freed ids were handed straight back (this is the case the old test could not tell apart)"
    assert _verifications(report_a) == before
    assert _missing_bill_by_s_no(client, report_a)[2]["status"] == "RESOLVED"
    assert _audit_events(audit.VERIFICATION_CARRIED_OVER) == [], "nothing needed to move"


def test_a_changed_claim_is_never_resolved_by_an_id_that_sqlite_would_reuse(client):
    """The case the id-reuse masking makes dangerous: the newest report is reprocessed and a verified claim's
    amount changed. Without id reservation the regenerated claim would get its OLD id back and the untouched
    verification would silently RESOLVE it."""
    report_b = _upload(client, "ER-75202")
    report_a = _upload(client, "ER-75201")  # newest
    assert max(_expense_ids(report_b).values()) < min(_expense_ids(report_a).values())
    _verify(client, report_a, 2)
    old_ids = _expense_ids(report_a)
    before = _verifications(report_a)
    _edit_old_expense(report_a, 2, claimed_amount=Decimal("999.00"))

    _reprocess(client, report_a)

    new_ids = _expense_ids(report_a)
    # The guarantee: no regenerated claim may receive the id the untouched verification still points at.
    # (Ids of claims nobody verified may be reused - only the stranded verification's id is protected.)
    assert old_ids[2] not in new_ids.values(), "no new expense was given the id the stranded verification points at"
    assert _verifications(report_a) == before, "the historical row is untouched"
    assert _missing_bill_by_s_no(client, report_a)[2]["status"] != "RESOLVED"
    assert [e.details for e in _audit_events(audit.VERIFICATION_NOT_CARRIED_OVER)].__len__() == 1
    assert _report_state(client, report_b)["verifications"] == []


def test_a_verification_stranded_by_an_earlier_reprocess_is_never_handed_to_a_new_claim(client, older_target):
    """A dormant row (its expense id no longer exists) must not attach to whichever claim later receives that id."""
    report_a, _report_b = older_target
    _verify(client, report_a, 2)
    _edit_old_expense(report_a, 2, claimed_amount=Decimal("999.00"))
    _reprocess(client, report_a)  # strands the verification (facts changed)
    stranded = _verifications(report_a)
    assert len(stranded) == 1 and stranded[0]["expense_id"] not in _expense_ids(report_a).values()

    _reprocess(client, report_a)  # A is now the newest report; the dormant id must still not be reused by a new claim

    assert _verifications(report_a) == stranded
    assert stranded[0]["expense_id"] not in _expense_ids(report_a).values()
    assert _missing_bill_by_s_no(client, report_a)[2]["status"] != "RESOLVED"
    assert len(_audit_events(audit.VERIFICATION_NOT_CARRIED_OVER)) == 1, "the dormant row is not re-reported on every reprocess"


def test_reserve_ids_only_changes_allocation_when_a_verification_stays_put(client):
    db = SessionLocal()
    try:
        rows = [Expense(s_no=1), Expense(s_no=2)]
        vc.reserve_ids(db, rows, vc.Plan([], set()))
        assert [r.id for r in rows] == [None, None], "no protected id: ids are allocated exactly as before"
        vc.reserve_ids(db, rows, vc.Plan([], {77}))
        assert [r.id for r in rows] == [78, 79]
    finally:
        db.close()


# ================================================================ helper: swapped ids cannot trip the unique key


def test_apply_can_swap_two_verifications_between_two_expenses(client):
    db = SessionLocal()
    try:
        report = ExpenseReport(filename="swap.pdf", original_filename="swap.pdf", file_hash="swap-hash", file_size=1, source_path="unused.pdf",
                               extraction_status="EXTRACTED", total_claimed=1.0, er_id="ER-75301")
        db.add(report)
        db.commit()
        first, second = Expense(expense_report_id=report.id, s_no=1), Expense(expense_report_id=report.id, s_no=2)
        db.add_all([first, second])
        db.commit()
        user_id = db.query(User).first().id
        for expense in (first, second):
            db.add(ExceptionVerification(expense_report_id=report.id, expense_id=expense.id, exception_type="MISSING BILL", original_severity="EXCEPTION",
                                         original_reason="r", verified_by_id=user_id, verification_note=f"for {expense.s_no}"))
        db.commit()
        plan = vc.Plan([vc.Outcome(first.id, second), vc.Outcome(second.id, first)], set())

        vc.apply(db, report.id, plan)
        db.commit()

        rows = {v.verification_note: v.expense_id for v in db.query(ExceptionVerification).filter(ExceptionVerification.expense_report_id == report.id)}
        assert rows == {"for 1": second.id, "for 2": first.id}
    finally:
        db.close()


# ================================================================ 11. a failure inside carry-over rolls everything back


def test_a_failure_inside_carry_over_commits_nothing_and_reports_the_extraction_failed(client, older_target, monkeypatch):
    report_a, report_b = older_target
    _verify(client, report_a, 2)
    _verify(client, report_a, 3)
    old_ids = _expense_ids(report_a)
    before_a = _report_state(client, report_a)
    before_b = _report_state(client, report_b)
    real_apply = vc.apply

    def failing_apply(db, report_id, the_plan):
        real_apply(db, report_id, the_plan)  # the re-keying and the audit rows really happen in the session ...
        raise RuntimeError("injected carry-over failure")  # ... and then the carry-over fails

    with monkeypatch.context() as patch:
        patch.setattr(vc, "apply", failing_apply)
        _reprocess(client, report_a)  # run_extraction() catches the error; the route itself returns normally

    # the extraction is reported FAILED, with the real cause in the processing log
    db = SessionLocal()
    try:
        assert db.get(ExpenseReport, report_a).extraction_status == "FAILED"
        log = db.query(ProcessingLog).filter(ProcessingLog.expense_report_id == report_a, ProcessingLog.status == "FAILED").all()
        assert any("injected carry-over failure" in (entry.message or "") for entry in log)
    finally:
        db.close()

    # nothing was committed: the expenses were NOT replaced, no verification moved, no carry-over audit row exists
    assert _expense_ids(report_a) == old_ids, "the original Expense rows are still there, with their original ids"
    assert _verifications(report_a) == before_a["verifications"], "no verification is half-moved"
    assert _audit_events(audit.VERIFICATION_CARRIED_OVER) == [] and _audit_events(audit.VERIFICATION_NOT_CARRIED_OVER) == []
    assert _report_state(client, report_a)["exceptions"] == before_a["exceptions"], "the verified claims are still RESOLVED, none falsely so"
    assert _report_state(client, report_b) == before_b

    # the system recovers: the next reprocess (without the injected failure) carries the verifications over
    _reprocess(client, report_a)
    after_ids = _expense_ids(report_a)
    assert set(after_ids.values()).isdisjoint(old_ids.values())
    assert [v["expense_id"] for v in _verifications(report_a)] == [after_ids[2], after_ids[3]]
