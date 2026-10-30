"""A manual verification applies only while the exception still rests on the evidence it was given on.

Policy (app/services/exception_engine.py, app/api/exceptions.py):

  * verify_exception stores an evidence_signature: SHA-256 of the TYPED fields of the rows behind the
    exception - check_type, result, claimed_amount, bill_amount, policy_limit, expense_date, bill_date,
    source_page. Never wording, never a row id.
  * evaluate_report re-applies an approval only while the regenerated exception's signature equals it.
    Otherwise the verification row is retained as history, the exception stays OPEN (so the expense is not
    VERIFIED and the report cannot become ACCOUNT VERIFIED on its account) and a stable note is appended
    to the regenerated reason - rebuilt from the base reason every evaluation, never accumulating.
  * An UNSIGNED legacy row (verified before the column existed) is applied only while original_reason equals
    the current reason, and is then stamped. Otherwise it is left open.
  * Re-verifying updates the same row: verified_at, evidence_signature, original_reason, original_severity;
    its id is kept, and so is its note unless a new one is supplied.

Part 1 seeds ValidationResult rows directly in the disposable database so each typed field can be varied in
isolation. Part 2 drives the real upload / reprocess / verify routes with synthetic PDFs (tests/synthetic_reports.py).
Nothing here touches a real database or an authentic document.
"""

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import synthetic_reports as sr
from app import database
from app import models  # noqa: F401  (registers every model on Base)
from app.database import Base, SessionLocal
from app.models.bill import Bill
from app.models.bill_match import BillMatchCandidate
from app.models.expense_report import Expense, ExpenseReport
from app.models.logs import AuditLog, ProcessingLog
from app.models.user import User
from app.models.validation import ExceptionRecord, ExceptionVerification, ValidationResult
from app.services import audit, db_migrations
from app.services import exception_engine as ee
from app.services.extraction import verification_carryover as vc
from app.services.validation.pipeline import validate_expense_report

ROOM = "ROOM SERVICE / FOOD INCLUDED"
NOTE = ee.STALE_APPROVAL_NOTE


def test_the_test_database_is_the_disposable_one():
    assert "spinco_test_" in str(database.DB_PATH), "the suite must run on the temp database set up by conftest.py"


# ================================================================ PART 1: the policy, on seeded rows


def _row(**overrides) -> ValidationResult:
    fields = dict(
        check_type="Room Service/Food Check", result="REVIEW_REQUIRED", claimed_amount=5100.0, bill_amount=957.0, policy_limit=None,
        expense_date=date(2026, 7, 24), bill_date=date(2026, 7, 24), source_page=5,
        remarks="Room service / food included: Rs.957.00 of Rs.5,100.00 total. Requires policy review.", details="d",
    )
    fields.update(overrides)
    fields.setdefault("expense_report_id", 1)
    return ValidationResult(**fields)


# ---------- signature stability and sensitivity


def test_the_signature_is_a_deterministic_64_hex_digest_independent_of_order_wording_and_ids():
    rows = [_row(), _row(check_type="Bill Amount Check", result="REVIEW_REQUIRED", bill_amount=None, remarks="x")]
    first = ee.evidence_signature(rows)
    assert len(first) == 64 and int(first, 16) >= 0
    assert ee.evidence_signature(list(reversed(rows))) == first, "row order is irrelevant"
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    assert ee.evidence_signature(shuffled) == first
    reworded = [_row(remarks="completely different wording", details="other"), _row(check_type="Bill Amount Check", bill_amount=None, remarks="y", details="z")]
    reworded[0].id, reworded[1].id = 901, 902
    assert ee.evidence_signature(reworded) == first, "neither wording nor row ids are part of the signature"
    assert ee.evidence_signature(rows) == first, "stable across calls"


def test_amounts_are_normalised_to_two_places_so_float_noise_cannot_change_the_signature():
    assert ee.evidence_signature([_row(bill_amount=957.0)]) == ee.evidence_signature([_row(bill_amount=Decimal("957.00"))])
    assert ee.evidence_signature([_row(claimed_amount=0.1 + 0.2)]) == ee.evidence_signature([_row(claimed_amount=Decimal("0.30"))])


@pytest.mark.parametrize(
    "field, changed",
    [
        ("claimed_amount", 5200.0), ("bill_amount", 357.0), ("policy_limit", 4000.0), ("expense_date", date(2026, 7, 25)),
        ("bill_date", date(2026, 7, 23)), ("source_page", 6), ("check_type", "Catering Check"), ("result", "EXCEPTION"),
    ],
)
def test_every_typed_field_changes_the_signature(field, changed):
    assert ee.evidence_signature([_row(**{field: changed})]) != ee.evidence_signature([_row()])


def test_a_none_and_a_missing_value_are_distinguished_from_zero_and_the_row_count_matters():
    assert ee.evidence_signature([_row(bill_amount=None)]) != ee.evidence_signature([_row(bill_amount=0.0)])
    assert ee.evidence_signature([_row()]) != ee.evidence_signature([_row(), _row()])


# ---------- seeding a report whose single expense carries one room-service review


def _seed_review(*, bill_amount=957.0, remarks=None, claimed=5100.0, er_id="ER-77001") -> tuple[int, int]:
    db = SessionLocal()
    try:
        report = ExpenseReport(filename=f"{er_id}.pdf", original_filename=f"{er_id}.pdf", file_hash=f"hash-{er_id}", file_size=1, source_path="unused.pdf",
                               extraction_status="EXTRACTED", total_claimed=claimed, er_id=er_id)
        db.add(report)
        db.commit()
        expense = Expense(expense_report_id=report.id, s_no=1, expense_date=date(2026, 7, 24), category="LODGING", claimed_amount=claimed,
                          bill_match_status="MATCHED", trip_match_status="NOT_PROCESSED", validation_status="REVIEW_REQUIRED")
        db.add(expense)
        db.commit()
        db.add(_row(expense_report_id=report.id, expense_id=expense.id, bill_amount=bill_amount, claimed_amount=claimed,
                    remarks=remarks or f"Room service / food included: Rs.{bill_amount:,.2f} of Rs.{claimed:,.2f} total. Requires policy review."))
        db.commit()
        ee.evaluate_report(db, report.id)
        return report.id, expense.id
    finally:
        db.close()


def _seed_report_level(*, result="EXCEPTION", claimed=5100.0, er_id="ER-77002") -> int:
    """A report with no expenses and ONE report-level check (expense_id NULL), like the Total Validation Check."""
    db = SessionLocal()
    try:
        report = ExpenseReport(filename=f"{er_id}.pdf", original_filename=f"{er_id}.pdf", file_hash=f"hash-{er_id}", file_size=1, source_path="unused.pdf",
                               extraction_status="EXTRACTED", total_claimed=claimed, er_id=er_id)
        db.add(report)
        db.commit()
        db.add(ValidationResult(
            expense_report_id=report.id, expense_id=None, check_type="Total Validation Check", result=result,
            exception_type="TOTAL MISMATCH" if result == "EXCEPTION" else None, claimed_amount=claimed,
            remarks=f"Sum of extracted expenses Rs.{claimed:,.2f} does not match the report total Rs.9,999.00.", details="d",
        ))
        db.commit()
        ee.evaluate_report(db, report.id)
        return report.id
    finally:
        db.close()


def _record(client, report_id: int, exception_type: str) -> dict:
    found = [e for e in client.get(f"/api/expense-reports/{report_id}/exceptions").json() if e["exception_type"] == exception_type]
    assert len(found) == 1, found
    return found[0]


def _verify(client, record: dict, note=None):
    body = {"note": note} if note is not None else {}
    resp = client.post(f"/api/exceptions/{record['id']}/verify", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _edit_validation_row(report_id: int, **fields) -> None:
    db = SessionLocal()
    try:
        row = db.query(ValidationResult).filter(ValidationResult.expense_report_id == report_id).one()
        for name, value in fields.items():
            setattr(row, name, value)
        db.commit()
    finally:
        db.close()


def _reevaluate(report_id: int) -> None:
    """What every real flow does: validate_expense_report resets each expense to its raw status, then evaluate_report runs."""
    db = SessionLocal()
    try:
        for expense in db.query(Expense).filter(Expense.expense_report_id == report_id):
            expense.validation_status = "REVIEW_REQUIRED"
        db.commit()
        ee.evaluate_report(db, report_id)
    finally:
        db.close()


def _verification(report_id: int, expense_id=None, *, any_expense=True) -> ExceptionVerification:
    db = SessionLocal()
    try:
        query = db.query(ExceptionVerification).filter(ExceptionVerification.expense_report_id == report_id)
        if not any_expense:
            query = query.filter(ExceptionVerification.expense_id.is_(None) if expense_id is None else ExceptionVerification.expense_id == expense_id)
        row = query.one()
        db.expunge(row)
        return row
    finally:
        db.close()


def _statuses(report_id: int) -> tuple[str | None, str | None]:
    db = SessionLocal()
    try:
        expense = db.query(Expense).filter(Expense.expense_report_id == report_id).first()
        return (expense.validation_status if expense else None), db.get(ExpenseReport, report_id).overall_status
    finally:
        db.close()


def _snapshot(row: ExceptionVerification) -> dict:
    return {c.name: getattr(row, c.name) for c in ExceptionVerification.__table__.columns}


# ---------- changed typed evidence leaves the exception open; unchanged or reworded evidence does not


def test_unchanged_evidence_keeps_the_approval_and_the_expense_and_report_are_verified(client):
    report_id, _expense_id = _seed_review()
    assert _statuses(report_id) == ("REVIEW_REQUIRED", "REVIEW REQUIRED")
    _verify(client, _record(client, report_id, ROOM), note="checked")
    assert _statuses(report_id) == ("VERIFIED", "ACCOUNT VERIFIED")
    _reevaluate(report_id)  # a revalidation that changes nothing
    record = _record(client, report_id, ROOM)
    assert record["status"] == "RESOLVED" and record["verification_note"] == "checked" and NOTE not in record["reason"]
    assert _statuses(report_id) == ("VERIFIED", "ACCOUNT VERIFIED")
    assert _verification(report_id).evidence_signature is not None


def test_a_wording_only_change_does_not_void_the_approval(client):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="checked")
    _edit_validation_row(report_id, remarks="Reworded: the room service / food charge of Rs.957.00 needs policy review.", details="reworded details")
    _reevaluate(report_id)
    record = _record(client, report_id, ROOM)
    assert record["status"] == "RESOLVED" and "Reworded" in record["reason"] and NOTE not in record["reason"]


@pytest.mark.parametrize(
    "field, changed",
    [("bill_amount", 357.0), ("claimed_amount", 5200.0), ("policy_limit", 4000.0), ("expense_date", date(2026, 7, 25)),
     ("bill_date", date(2026, 7, 20)), ("source_page", 9)],
)
def test_a_typed_evidence_change_leaves_the_exception_open_and_the_history_untouched(client, field, changed):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="checked")
    before = _snapshot(_verification(report_id))
    _edit_validation_row(report_id, **{field: changed})
    _reevaluate(report_id)

    record = _record(client, report_id, ROOM)
    assert record["status"] == "OPEN" and not record["verification_note"], "the obsolete approval is not applied"
    assert record["reason"].endswith(f" [{NOTE}]") and record["reason"].count(NOTE) == 1
    assert _snapshot(_verification(report_id)) == before, "the verification row is retained exactly as it was (nothing deleted, nothing rewritten)"
    expense_status, report_status = _statuses(report_id)
    assert expense_status != "VERIFIED" and report_status != "ACCOUNT VERIFIED", "the stale approval cannot verify the expense or the report"


def test_the_stale_note_is_stable_and_never_accumulates(client):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM))
    _edit_validation_row(report_id, bill_amount=357.0, remarks="Room service / food included: Rs.357.00 of Rs.5,100.00 total. Requires policy review.")
    reasons = []
    for _ in range(3):
        _reevaluate(report_id)
        reasons.append(_record(client, report_id, ROOM)["reason"])
    assert reasons[0] == reasons[1] == reasons[2]
    assert reasons[0] == "Room service / food included: Rs.357.00 of Rs.5,100.00 total. Requires policy review." + f" [{NOTE}]"


def test_the_stale_note_never_pushes_the_reason_past_its_column_length(client):
    report_id, _ = _seed_review(remarks="x" * 990)
    _verify(client, _record(client, report_id, ROOM))
    _edit_validation_row(report_id, bill_amount=357.0)
    _reevaluate(report_id)
    reason = _record(client, report_id, ROOM)["reason"]
    assert len(reason) <= 1000 and reason.endswith(f" [{NOTE}]")
    _reevaluate(report_id)
    assert _record(client, report_id, ROOM)["reason"] == reason


def test_evidence_that_returns_to_what_was_approved_re_applies_the_approval(client):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="checked")
    _edit_validation_row(report_id, bill_amount=357.0)
    _reevaluate(report_id)
    assert _record(client, report_id, ROOM)["status"] == "OPEN"
    _edit_validation_row(report_id, bill_amount=957.0)
    _reevaluate(report_id)
    assert _record(client, report_id, ROOM)["status"] == "RESOLVED", "same evidence as approved: the retained approval applies again"


# ---------- explicit re-verification


def test_re_verifying_updates_the_same_row_and_strips_the_note_from_the_approved_reason(client):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="first look")
    first = _snapshot(_verification(report_id))
    # make the stored row look older, with a sentinel severity, so rewriting is observable
    db = SessionLocal()
    try:
        row = db.query(ExceptionVerification).one()
        row.verified_at = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
        row.original_severity = "STALE-SEVERITY"
        db.commit()
    finally:
        db.close()
    _edit_validation_row(report_id, bill_amount=357.0, remarks="Room service / food included: Rs.357.00 of Rs.5,100.00 total. Requires policy review.")
    _reevaluate(report_id)
    stale = _record(client, report_id, ROOM)
    assert stale["status"] == "OPEN" and NOTE in stale["reason"]

    _verify(client, stale, note="second look")

    row = _snapshot(_verification(report_id))
    assert row["id"] == first["id"], "the SAME row: its id is preserved"
    assert row["verified_at"] > datetime(2026, 1, 1), "verified_at was updated"
    assert row["original_severity"] == "REVIEW REQUIRED", "original_severity was updated"
    assert row["original_reason"] == "Room service / food included: Rs.357.00 of Rs.5,100.00 total. Requires policy review.", "no stale note is ever stored as the approved reason"
    assert row["evidence_signature"] != first["evidence_signature"] and row["evidence_signature"] == ee.signature_for_record(SessionLocal(), _orm_record(report_id, ROOM))
    assert row["verification_note"] == "second look"
    assert _record(client, report_id, ROOM)["status"] == "RESOLVED" and _statuses(report_id) == ("VERIFIED", "ACCOUNT VERIFIED")
    db = SessionLocal()
    try:
        assert db.query(ExceptionVerification).count() == 1, "never a second row"
    finally:
        db.close()


def _orm_record(report_id: int, exception_type: str) -> ExceptionRecord:
    db = SessionLocal()
    try:
        record = db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report_id, ExceptionRecord.exception_type == exception_type).one()
        db.expunge(record)
        return record
    finally:
        db.close()


def test_re_verifying_without_a_new_note_keeps_the_existing_note(client):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="the original note")
    _edit_validation_row(report_id, bill_amount=357.0)
    _reevaluate(report_id)
    _verify(client, _record(client, report_id, ROOM))  # no note supplied
    assert _verification(report_id).verification_note == "the original note"


def test_the_signature_stored_by_verify_equals_the_one_evaluate_computes(client):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM))
    db = SessionLocal()
    try:
        stored = db.query(ExceptionVerification).one().evidence_signature
        assert stored == ee.signature_for_record(db, db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report_id, ExceptionRecord.exception_type == ROOM).one())
    finally:
        db.close()


# ---------- legacy (unsigned) verifications


def _legacy(report_id: int) -> None:
    """Make the stored row look like one written before the column existed."""
    db = SessionLocal()
    try:
        row = db.query(ExceptionVerification).filter(ExceptionVerification.expense_report_id == report_id).one()
        row.evidence_signature = None
        db.commit()
    finally:
        db.close()


def test_an_unsigned_row_is_applied_when_the_reason_is_unchanged_and_is_then_stamped(client):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="legacy approval")
    _legacy(report_id)
    assert _verification(report_id).evidence_signature is None

    _reevaluate(report_id)

    assert _record(client, report_id, ROOM)["status"] == "RESOLVED"
    db = SessionLocal()
    try:
        stamped = db.query(ExceptionVerification).one()
        assert stamped.evidence_signature == ee.signature_for_record(db, db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report_id, ExceptionRecord.exception_type == ROOM).one())
    finally:
        db.close()


def test_an_unsigned_row_is_not_applied_when_the_reason_differs_and_stays_untouched(client):
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM), note="legacy approval")
    _legacy(report_id)
    before = _snapshot(_verification(report_id))
    _edit_validation_row(report_id, remarks="A different reason text for the same evidence.")
    _reevaluate(report_id)

    record = _record(client, report_id, ROOM)
    assert record["status"] == "OPEN" and record["reason"].endswith(f" [{NOTE}]")
    assert _snapshot(_verification(report_id)) == before, "left exactly as it was - still unsigned"
    assert _statuses(report_id)[1] != "ACCOUNT VERIFIED"

    _verify(client, record, note="re-verified")  # an explicit re-verification signs it
    assert _verification(report_id).evidence_signature is not None
    assert _record(client, report_id, ROOM)["status"] == "RESOLVED"


def test_known_limit_an_unsigned_row_compares_text_only(client):
    """For a row with no signature the reason text is the only record of what was approved, so a typed change that
    leaves the reason text identical cannot be seen. Rows verified after this change carry a signature and do not have this limit."""
    report_id, _ = _seed_review()
    _verify(client, _record(client, report_id, ROOM))
    _legacy(report_id)
    _edit_validation_row(report_id, source_page=42)  # typed change, identical reason text
    _reevaluate(report_id)
    assert _record(client, report_id, ROOM)["status"] == "RESOLVED"


# ---------- report-level exceptions (expense_id NULL)


@pytest.mark.parametrize("result, severity", [("EXCEPTION", "EXCEPTION"), ("REVIEW_REQUIRED", "REVIEW REQUIRED")])
def test_report_level_approvals_follow_the_same_policy(client, result, severity):
    report_id = _seed_report_level(result=result)
    exception_type = "TOTAL MISMATCH" if result == "EXCEPTION" else "REPORT TOTAL REVIEW REQUIRED"
    record = _record(client, report_id, exception_type)
    assert record["severity"] == severity and record["expense_id"] is None
    _verify(client, record, note="accepted at report level")  # an EXCEPTION needs a note
    assert _statuses(report_id)[1] == "ACCOUNT VERIFIED"

    _reevaluate(report_id)  # unchanged
    assert _record(client, report_id, exception_type)["status"] == "RESOLVED"

    _edit_validation_row(report_id, remarks="reworded but the same evidence")
    _reevaluate(report_id)
    assert _record(client, report_id, exception_type)["status"] == "RESOLVED"

    before = _snapshot(_verification(report_id))
    _edit_validation_row(report_id, claimed_amount=7777.0)  # the extracted sum changed
    _reevaluate(report_id)
    stale = _record(client, report_id, exception_type)
    assert stale["status"] == "OPEN" and stale["reason"].endswith(f" [{NOTE}]")
    assert _snapshot(_verification(report_id)) == before
    assert _statuses(report_id)[1] != "ACCOUNT VERIFIED"


# ---------- schema: fresh database and additive migration of an existing one


def test_a_fresh_database_has_the_column(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(bind=engine)
    assert "evidence_signature" in {c["name"] for c in inspect(engine).get_columns("exception_verifications")}


def test_an_existing_database_is_migrated_additively_and_idempotently(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        report = ExpenseReport(filename="l.pdf", original_filename="l.pdf", file_hash="legacy-sig", file_size=1, source_path="unused.pdf", extraction_status="EXTRACTED", total_claimed=1.0)
        session.add(report)
        session.commit()
        user = User(name="U", email="legacy@example.com", role="ADMIN")
        session.add(user)
        session.commit()
        session.add(ExceptionVerification(expense_report_id=report.id, expense_id=None, exception_type="TOTAL MISMATCH", original_severity="EXCEPTION",
                                          original_reason="legacy reason", verified_by_id=user.id, verification_note="legacy note"))
        session.commit()
    finally:
        session.close()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE exception_verifications DROP COLUMN evidence_signature"))
    assert "evidence_signature" not in {c["name"] for c in inspect(engine).get_columns("exception_verifications")}, "fixture must be a real legacy schema"

    db_migrations.run_light_migrations(engine)
    db_migrations.run_light_migrations(engine)  # a second run is a no-op

    assert "evidence_signature" in {c["name"] for c in inspect(engine).get_columns("exception_verifications")}
    with engine.connect() as conn:
        row = conn.execute(text("SELECT original_reason, verification_note, evidence_signature FROM exception_verifications")).one()
    assert tuple(row) == ("legacy reason", "legacy note", None), "the old row is intact and unsigned"


# ================================================================ PART 2: end to end through upload / reprocess / verify

LODGING_DAY = date(2026, 7, 24)


def _folio() -> sr.Receipt:
    return sr.Receipt(
        "r1", "folio_room_service",
        ["AURANGABAD GYMKHANA HOTEL", "TAX INVOICE", "Invoice No 2526/2923", f"Check-in {LODGING_DAY.strftime('%d/%m/%Y')}", "Check-out 25/07/2026",
         "CATERING SERVICE", "ROOM RENT 1607.00", "Room Charges 2,550.00", "ROOM SERVICE Restaurant Charges RS26/3238 357.00",
         "Room Charges 2,550.00", "Total 5,457.00"],
        5457.0, LODGING_DAY, None, "GYMKHANA", kind="invoice", category_evidence="Lodging",
    )


def _lodging_case(er_id: str, *, with_cab: bool = False) -> sr.Case:
    claims = [sr.Claim(1, LODGING_DAY, "LODGING", "Hotel stay", "Aurangabad Gymkhana", 5100.0, expect="SUPPORTED", acceptable={"r1"})]
    if with_cab:
        claims.append(sr.Claim(2, date(2026, 7, 25), "CAB CHARGES", "Local travel", "Vendor", 520.0, expect="UNSUPPORTED", expect_exception="MISSING BILL"))
    return sr.Case("verification_evidence", "dev", "L1", claims, [_folio()], er_id=er_id)


def _upload(client, case: sr.Case) -> int:
    pdf, _ = sr.render_er(case)
    resp = client.post("/api/uploads/expense-reports", files=[("files", (f"{case.er_id}.pdf", pdf, "application/pdf"))])
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
    return client.get(f"/api/expense-reports/{report_id}/exceptions").json()


def _exception_of(client, report_id: int, exception_type: str, s_no: int) -> dict:
    expense_id = _expense_ids(report_id)[s_no]
    found = [e for e in _exceptions(client, report_id) if e["exception_type"] == exception_type and e["expense_id"] == expense_id]
    assert len(found) == 1, (exception_type, s_no, _exceptions(client, report_id))
    return found[0]


def _bills(report_id: int) -> list[tuple]:
    db = SessionLocal()
    try:
        return [(b.id, float(b.amount) if b.amount is not None else None, float(b.room_service_food_amount) if b.room_service_food_amount is not None else None,
                 b.room_service_food_mentioned, float(b.catering_amount) if b.catering_amount is not None else None, b.catering_mentioned)
                for b in db.query(Bill).filter(Bill.expense_report_id == report_id).order_by(Bill.id)]
    finally:
        db.close()


def _candidates(report_id: int) -> list[tuple]:
    db = SessionLocal()
    try:
        ids = [e.id for e in db.query(Expense.id).filter(Expense.expense_report_id == report_id)]
        return [(c.expense_id, c.bill_id, c.rank) for c in db.query(BillMatchCandidate).filter(BillMatchCandidate.expense_id.in_(ids)).order_by(BillMatchCandidate.id)]
    finally:
        db.close()


def _claim_row(report_id: int, s_no: int) -> Expense:
    db = SessionLocal()
    try:
        row = db.query(Expense).filter(Expense.expense_report_id == report_id, Expense.s_no == s_no).one()
        db.expunge(row)
        return row
    finally:
        db.close()


def _orphans() -> int:
    db = SessionLocal()
    try:
        return (db.query(BillMatchCandidate).filter(~BillMatchCandidate.bill_id.in_(db.query(Bill.id))).count()
                + db.query(BillMatchCandidate).filter(~BillMatchCandidate.expense_id.in_(db.query(Expense.id))).count())
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


def _reprocess(client, report_id: int) -> None:
    resp = client.post(f"/api/documents/expense:{report_id}/reprocess")
    assert resp.status_code == 200, resp.text


def _other_state(client, report_id: int) -> dict:
    return {
        "expense_ids": _expense_ids(report_id), "bills": _bills(report_id), "candidates": _candidates(report_id),
        "exceptions": sorted((e["expense_id"], e["exception_type"], e["status"], e["verification_note"], e["reason"]) for e in _exceptions(client, report_id)),
        "status": _statuses(report_id),
    }


def _stale_inject_and_verify(client, report_id: int, note: str) -> dict:
    """Make the stored room-service evidence read Rs.957.00 (what an older detector stored), re-validate as the app does,
    and verify the review it produces."""
    db = SessionLocal()
    try:
        bill = db.query(Bill).filter(Bill.expense_report_id == report_id).one()
        bill.room_service_food_amount = 957.00
        bill.room_service_food_mentioned = True
        db.commit()
        validate_expense_report(db, report_id)
        ee.evaluate_report(db, report_id)
    finally:
        db.close()
    review = _exception_of(client, report_id, ROOM, 1)
    assert "Rs.957.00" in review["reason"]
    return _verify(client, review, note=note)


@pytest.fixture
def older_lodging_report(client):
    """A = the OLDER report (it gets reprocessed); B = a newer one that keeps the higher ids allocated. B is a lodging report
    too, with its own bill, candidates and a claim identical in facts to A's - so it also proves A's reprocess never reaches it."""
    report_a = _upload(client, _lodging_case("ER-78001"))
    report_b = _upload(client, _lodging_case("ER-78002"))
    assert max(_expense_ids(report_a).values()) < min(_expense_ids(report_b).values()), "fixture: B must hold the higher ids"
    return report_a, report_b


# ---------- A. full reprocess, room-service/food folio unchanged


def test_a_full_reprocess_with_an_unchanged_folio_keeps_the_approval(client, older_lodging_report):
    report_a, report_b = older_lodging_report
    review = _exception_of(client, report_a, ROOM, 1)
    assert "Rs.357.00" in review["reason"]
    _verify(client, review, note="checked against the folio")
    old_ids = _expense_ids(report_a)
    old_bills = _bills(report_a)
    verification_before = _snapshot(_verification(report_a))
    other_before = _other_state(client, report_b)
    assert _statuses(report_a)[1] == "ACCOUNT VERIFIED" or any(e["status"] == "OPEN" for e in _exceptions(client, report_a))

    _reprocess(client, report_a)

    new_ids = _expense_ids(report_a)
    assert set(new_ids.values()).isdisjoint(old_ids.values()), "precondition: the expenses really were recreated with new ids"
    bills = _bills(report_a)
    assert len(bills) == 1 and bills[0][0] != old_bills[0][0], "a new bill row"
    assert bills[0][1:] == (5457.0, 357.0, True, None, True), "room-service 357 and the flag are stored again; catering is a mention only"
    claim = _claim_row(report_a, 1)
    assert claim.id == new_ids[1] and claim.bill_id == bills[0][0] and claim.candidate_bill_id == bills[0][0] and claim.bill_match_status == "MATCHED"
    assert _candidates(report_a) and all(e_id in new_ids.values() and b_id == bills[0][0] for e_id, b_id, _rank in _candidates(report_a))
    assert _orphans() == 0

    row = _snapshot(_verification(report_a))
    assert row["id"] == verification_before["id"] and row["expense_id"] == new_ids[1], "the same verification row followed its claim"
    assert {k: v for k, v in row.items() if k != "expense_id"} == {k: v for k, v in verification_before.items() if k != "expense_id"}, "signature included: unchanged"
    review_after = _exception_of(client, report_a, ROOM, 1)
    assert review_after["status"] == "RESOLVED" and review_after["verification_note"] == "checked against the folio"
    assert "Rs.357.00" in review_after["reason"] and NOTE not in review_after["reason"]
    assert not [e for e in _exceptions(client, report_a) if e["exception_type"] == "AMOUNT MISMATCH"]
    if not [e for e in _exceptions(client, report_a) if e["status"] == "OPEN"]:
        assert _statuses(report_a) == ("VERIFIED", "ACCOUNT VERIFIED")

    assert _other_state(client, report_b) == other_before
    carried = _audit_events(audit.VERIFICATION_CARRIED_OVER)
    assert len(carried) == 1 and f"new_expense_id={new_ids[1]}" in carried[0].details and "checked against the folio" not in carried[0].details


# ---------- B. full reprocess, the room-service evidence changed


def test_a_full_reprocess_with_changed_room_service_evidence_does_not_keep_the_old_approval(client, older_lodging_report):
    report_a, report_b = older_lodging_report
    _stale_inject_and_verify(client, report_a, note="approved at Rs.957")
    old_ids = _expense_ids(report_a)
    verification_before = _snapshot(_verification(report_a))
    other_before = _other_state(client, report_b)
    assert _exception_of(client, report_a, ROOM, 1)["status"] == "RESOLVED"

    _reprocess(client, report_a)

    new_ids = _expense_ids(report_a)
    assert set(new_ids.values()).isdisjoint(old_ids.values()), "precondition: new expense ids"
    assert _bills(report_a)[0][1:] == (5457.0, 357.0, True, None, True), "the stale 957 is gone: the evidence was regenerated"

    review = _exception_of(client, report_a, ROOM, 1)
    assert review["status"] == "OPEN" and not review["verification_note"], "the obsolete approval is not applied"
    assert "Rs.357.00" in review["reason"] and "Rs.957.00" not in review["reason"] and review["reason"].endswith(f" [{NOTE}]")
    assert not [e for e in _exceptions(client, report_a) if e["exception_type"] == "AMOUNT MISMATCH"], "the false mismatch is gone with the stale value"
    expense_status, report_status = _statuses(report_a)
    assert expense_status != "VERIFIED" and report_status != "ACCOUNT VERIFIED", "the stale approval cannot verify the expense or the report"

    row = _snapshot(_verification(report_a))
    assert row["id"] == verification_before["id"] and row["expense_id"] == new_ids[1], "the row followed its claim (the carry-over still works)"
    assert {k: v for k, v in row.items() if k != "expense_id"} == {k: v for k, v in verification_before.items() if k != "expense_id"}, "and is otherwise untouched history"
    assert "Rs.957.00" in row["original_reason"] and row["evidence_signature"] == verification_before["evidence_signature"]
    assert _other_state(client, report_b) == other_before
    assert _orphans() == 0

    # the auditor re-verifies on the evidence as it now stands: the SAME row is updated
    resp = _verify(client, review, note="re-checked at Rs.357")
    assert resp["status"] == "RESOLVED"
    after = _snapshot(_verification(report_a))
    assert after["id"] == row["id"]
    assert after["verified_at"] >= row["verified_at"] and after["evidence_signature"] != row["evidence_signature"]
    assert "Rs.357.00" in after["original_reason"] and NOTE not in after["original_reason"] and after["verification_note"] == "re-checked at Rs.357"
    assert _exception_of(client, report_a, ROOM, 1)["status"] == "RESOLVED"


# ---------- C. reserved expense ids together with a synthetic bill and bill-to-expense matching


def test_reserved_expense_ids_keep_bill_matching_and_the_exclusion_intact(client):
    report_a = _upload(client, _lodging_case("ER-79001", with_cab=True))
    report_b = _upload(client, _lodging_case("ER-79002"))
    assert max(_expense_ids(report_a).values()) < min(_expense_ids(report_b).values())
    _verify(client, _exception_of(client, report_a, ROOM, 1), note="room service ok")
    _verify(client, _exception_of(client, report_a, "MISSING BILL", 2), note="no receipt, accepted")
    old_ids = _expense_ids(report_a)
    old_bill = _bills(report_a)[0]
    verification_2_before = _snapshot(_verification_of(report_a, old_ids[2]))
    _edit_expense(report_a, 2, claimed_amount=Decimal("999.00"))  # its facts changed: that verification will stay put, so ids are reserved
    other_before = _other_state(client, report_b)

    _reprocess(client, report_a)

    new_ids = _expense_ids(report_a)
    b_max = max(_expense_ids(report_b).values())
    assert old_ids[2] not in new_ids.values(), "no new expense received the id the stranded verification still points at"
    assert list(new_ids.values()) == sorted(new_ids.values()) and all(i > b_max for i in new_ids.values()), "reserved ids: ascending in s_no order and above everything allocated"

    bills = _bills(report_a)
    assert len(bills) == 1 and bills[0][0] != old_bill[0] and bills[0][1:] == (5457.0, 357.0, True, None, True)
    claim1, claim2 = _claim_row(report_a, 1), _claim_row(report_a, 2)
    assert claim1.bill_id == bills[0][0] and claim1.candidate_bill_id == bills[0][0] and claim1.bill_match_status == "MATCHED", "bill-to-expense matching used the reserved ids"
    assert claim2.bill_id is None, "the CAB claim has no bill"
    assert _candidates(report_a) and all(e in new_ids.values() and b == bills[0][0] for e, b, _r in _candidates(report_a))
    assert _orphans() == 0
    assert not [e for e in _exceptions(client, report_a) if e["exception_type"] == "AMOUNT MISMATCH"], "the room-service exclusion still applies"

    # claim 1: same facts, same evidence -> carried and still resolved
    assert _exception_of(client, report_a, ROOM, 1)["status"] == "RESOLVED"
    assert _verification_of(report_a, new_ids[1]).exception_type == ROOM
    # claim 2: changed facts -> the verification is untouched on its old id and the regenerated claim is OPEN
    assert _snapshot(_verification_of(report_a, old_ids[2])) == verification_2_before
    assert _exception_of(client, report_a, "MISSING BILL", 2)["status"] == "OPEN"
    assert len(_audit_events(audit.VERIFICATION_CARRIED_OVER)) == 1 and len(_audit_events(audit.VERIFICATION_NOT_CARRIED_OVER)) == 1
    assert "reason=facts_changed" in _audit_events(audit.VERIFICATION_NOT_CARRIED_OVER)[0].details
    assert _other_state(client, report_b) == other_before


def _verification_of(report_id: int, expense_id: int) -> ExceptionVerification:
    db = SessionLocal()
    try:
        row = db.query(ExceptionVerification).filter(ExceptionVerification.expense_report_id == report_id, ExceptionVerification.expense_id == expense_id).one()
        db.expunge(row)
        return row
    finally:
        db.close()


def _edit_expense(report_id: int, s_no: int, **fields) -> None:
    db = SessionLocal()
    try:
        expense = db.query(Expense).filter(Expense.expense_report_id == report_id, Expense.s_no == s_no).one()
        for name, value in fields.items():
            setattr(expense, name, value)
        db.commit()
    finally:
        db.close()


# ---------- D. a failure inside the carry-over, with a room-service bill present


def test_a_carry_over_failure_with_a_room_service_bill_leaves_everything_as_it_was(client, older_lodging_report, monkeypatch):
    report_a, report_b = older_lodging_report
    _verify(client, _exception_of(client, report_a, ROOM, 1), note="checked")
    before_a = _other_state(client, report_a)
    before_b = _other_state(client, report_b)
    verification_before = _snapshot(_verification(report_a))
    real_apply = vc.apply

    def failing_apply(db, report_id, the_plan):
        real_apply(db, report_id, the_plan)
        raise RuntimeError("injected carry-over failure")

    with monkeypatch.context() as patch:
        patch.setattr(vc, "apply", failing_apply)
        _reprocess(client, report_a)

    db = SessionLocal()
    try:
        assert db.get(ExpenseReport, report_a).extraction_status == "FAILED"
        assert any("injected carry-over failure" in (e.message or "") for e in db.query(ProcessingLog).filter(ProcessingLog.expense_report_id == report_a, ProcessingLog.status == "FAILED"))
    finally:
        db.close()
    assert _other_state(client, report_a) == before_a, "same expense ids, same bill row and stored evidence (357 / flag), same candidates, same exceptions"
    assert _snapshot(_verification(report_a)) == verification_before
    assert _audit_events(audit.VERIFICATION_CARRIED_OVER) == [] and _audit_events(audit.VERIFICATION_NOT_CARRIED_OVER) == []
    assert _other_state(client, report_b) == before_b

    _reprocess(client, report_a)  # without the injected failure it succeeds, with the approval applied (same evidence)
    assert _exception_of(client, report_a, ROOM, 1)["status"] == "RESOLVED"
    assert _bills(report_a)[0][1:] == (5457.0, 357.0, True, None, True)


# ---------- the verify route's existing contract, exercised synthetically (the equivalent tests in test_exception_engine.py use an authentic sample PDF)


def test_verifying_an_already_verified_condition_is_rejected_and_never_duplicated(client):
    report_id, _ = _seed_review()
    record = _record(client, report_id, ROOM)
    _verify(client, record, note="first")
    again = client.post(f"/api/exceptions/{_record(client, report_id, ROOM)['id']}/verify", json={"note": "second"})
    assert again.status_code == 400
    db = SessionLocal()
    try:
        rows = db.query(ExceptionVerification).filter(ExceptionVerification.expense_report_id == report_id).all()
        assert len(rows) == 1 and rows[0].verification_note == "first"
    finally:
        db.close()


def test_a_confirmed_exception_needs_a_note_and_no_verification_is_stored_without_one(client):
    report_id = _seed_report_level(result="EXCEPTION")
    record = _record(client, report_id, "TOTAL MISMATCH")
    rejected = client.post(f"/api/exceptions/{record['id']}/verify", json={"note": None})
    assert rejected.status_code == 400
    db = SessionLocal()
    try:
        assert db.query(ExceptionVerification).count() == 0
    finally:
        db.close()
    assert _verify(client, record, note="acceptable difference")["status"] == "RESOLVED"
    assert _verification(report_id).evidence_signature is not None


def test_a_verification_is_audit_logged_with_the_exception_id_but_never_its_note(client):
    report_id, expense_id = _seed_review()
    record = _record(client, report_id, ROOM)
    _verify(client, record, note="audit trail check - a free-text note")
    events = _audit_events(audit.EXCEPTION_VERIFIED)
    assert len(events) == 1 and events[0].entity_id == str(record["id"]) and events[0].user_id is not None
    assert events[0].details == f"{ROOM} on expense_id={expense_id} (report {report_id}); note recorded"
    assert "audit trail check" not in events[0].details and "free-text" not in events[0].details, "the note text never reaches the audit event"
    assert _verification(report_id).verification_note == "audit trail check - a free-text note", "the note lives on the verification record"
    assert _record(client, report_id, ROOM)["verification_note"] == "audit trail check - a free-text note", "and is returned by the exceptions API"


def test_a_verification_without_a_note_logs_no_note_marker(client):
    report_id, expense_id = _seed_review()
    _verify(client, _record(client, report_id, ROOM))
    assert _audit_events(audit.EXCEPTION_VERIFIED)[0].details == f"{ROOM} on expense_id={expense_id} (report {report_id})"


def test_historical_audit_rows_are_never_rewritten_and_a_re_verification_note_stays_out_of_new_ones(client):
    report_id, expense_id = _seed_review()
    historical = AuditLog(user_id=None, action=audit.EXCEPTION_VERIFIED, entity_type="ExceptionRecord", entity_id="9999",
                          details="OLD TYPE on expense_id=1 (report 1); note: a note written before this change")
    db = SessionLocal()
    try:
        db.add(historical)
        db.commit()
        historical_id, historical_details, historical_at = historical.id, historical.details, historical.created_at
    finally:
        db.close()
    _verify(client, _record(client, report_id, ROOM), note="first note")
    _edit_validation_row(report_id, bill_amount=357.0)
    _reevaluate(report_id)
    _verify(client, _record(client, report_id, ROOM), note="second note (replaces the first on the record)")

    db = SessionLocal()
    try:
        old = db.get(AuditLog, historical_id)
        assert (old.details, old.created_at) == (historical_details, historical_at), "a historical audit row is exactly as it was written"
    finally:
        db.close()
    new_events = [e for e in _audit_events(audit.EXCEPTION_VERIFIED) if e.id != historical_id]
    assert len(new_events) == 2
    assert all("note" not in e.details.replace("note recorded", "") for e in new_events)
    assert all("first note" not in e.details and "second note" not in e.details for e in new_events)


# ================================================================ BILL MATCH family: a multi-row review, and the match status an approval flips

BILL_MATCH = "BILL MATCH REVIEW REQUIRED"
MANUAL_PREFIX = "Manually verified by auditor"


def _seed_bill_match(*, er_id="ER-77101", reasons="Amount agrees; the date is unclear.") -> tuple[int, int]:
    """One expense whose bill match needs review: THREE review rows that collapse into the single BILL MATCH family record, plus
    rows that must NOT be part of it (a PASS row, and a review row of a different family - CATERING INCLUDED)."""
    db = SessionLocal()
    try:
        report = ExpenseReport(filename=f"{er_id}.pdf", original_filename=f"{er_id}.pdf", file_hash=f"hash-{er_id}", file_size=1, source_path="unused.pdf",
                               extraction_status="EXTRACTED", total_claimed=5100.0, er_id=er_id)
        db.add(report)
        db.commit()
        expense = Expense(expense_report_id=report.id, s_no=1, expense_date=date(2026, 7, 24), category="TRAIN / BUS FARE", claimed_amount=5100.0,
                          bill_match_status="REVIEW_REQUIRED", bill_match_reasons=reasons, trip_match_status="NOT_PROCESSED", validation_status="REVIEW_REQUIRED")
        db.add(expense)
        db.commit()
        common = dict(expense_report_id=report.id, expense_id=expense.id)
        db.add_all([
            _row(**common, check_type="Bill Amount Check", claimed_amount=5100.0, bill_amount=5457.0, remarks="The bill amount needs review.", source_page=5),
            _row(**common, check_type="Bill Date Check", claimed_amount=None, bill_amount=None, bill_date=None, remarks="The bill date is unclear.", source_page=5),
            _row(**common, check_type="Category Consistency Check", claimed_amount=None, bill_amount=None, remarks="The category is unclear.", source_page=5),
            _row(**common, check_type="Policy Limit Check", result="PASS", claimed_amount=5100.0, bill_amount=None, remarks="Within limit", policy_limit=9000.0),
            _row(**common, check_type="Catering Check", bill_amount=150.0, remarks="Catering included: Rs.150.00 of Rs.5,100.00 total. Requires policy review."),
        ])
        db.commit()
        ee.evaluate_report(db, report.id)
        return report.id, expense.id
    finally:
        db.close()


def _edit_check(report_id: int, check_type: str, **fields) -> None:
    db = SessionLocal()
    try:
        row = db.query(ValidationResult).filter(ValidationResult.expense_report_id == report_id, ValidationResult.check_type == check_type).one()
        for name, value in fields.items():
            setattr(row, name, value)
        db.commit()
    finally:
        db.close()


def _expense_row(expense_id: int) -> Expense:
    db = SessionLocal()
    try:
        row = db.get(Expense, expense_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def _family_rows(report_id: int, check_types: tuple) -> list[ValidationResult]:
    db = SessionLocal()
    try:
        rows = db.query(ValidationResult).filter(ValidationResult.expense_report_id == report_id, ValidationResult.check_type.in_(check_types)).all()
        for row in rows:
            db.expunge(row)
        return rows
    finally:
        db.close()


FAMILY_CHECKS = ("Bill Amount Check", "Bill Date Check", "Category Consistency Check")


def test_a_multi_row_bill_match_approval_is_not_invalidated_by_re_evaluation(client):
    report_id, _ = _seed_bill_match()
    record = _record(client, report_id, BILL_MATCH)
    assert "needs review" in record["reason"] and "unclear" in record["reason"], "fixture: three rows collapsed into one record"
    assert [e["exception_type"] for e in client.get(f"/api/expense-reports/{report_id}/exceptions").json()].count(BILL_MATCH) == 1

    resp = _verify(client, record, note="match confirmed")
    assert resp["status"] == "RESOLVED"
    row = _verification(report_id)
    family = _family_rows(report_id, FAMILY_CHECKS)
    assert len(family) == 3
    assert row.evidence_signature == ee.evidence_signature(family), "the stored signature covers exactly the three family rows - not the PASS row, not the CATERING row"

    _reevaluate(report_id)  # an immediate re-evaluation, as every revalidate / rematch / reprocess does
    again = _record(client, report_id, BILL_MATCH)
    assert again["status"] == "RESOLVED" and again["verification_note"] == "match confirmed" and NOTE not in again["reason"]
    assert _verification(report_id).evidence_signature == row.evidence_signature

    _reevaluate(report_id)
    assert _record(client, report_id, BILL_MATCH)["status"] == "RESOLVED"


def test_other_families_and_passing_rows_neither_invalidate_nor_are_invalidated_by_a_bill_match_approval(client):
    report_id, _ = _seed_bill_match()
    _verify(client, _record(client, report_id, BILL_MATCH))
    _edit_check(report_id, "Catering Check", bill_amount=999.0)  # a DIFFERENT family changes
    _edit_check(report_id, "Policy Limit Check", policy_limit=1.0)  # and a PASS row
    _reevaluate(report_id)
    assert _record(client, report_id, BILL_MATCH)["status"] == "RESOLVED", "the approved family's own evidence did not change"
    assert _record(client, report_id, "CATERING INCLUDED")["status"] == "OPEN"

    _edit_check(report_id, "Bill Date Check", bill_date=date(2026, 7, 1))  # ONE of the three family rows changes
    _reevaluate(report_id)
    stale = _record(client, report_id, BILL_MATCH)
    assert stale["status"] == "OPEN" and stale["reason"].endswith(f" [{NOTE}]")
    assert _record(client, report_id, "CATERING INCLUDED")["status"] == "OPEN"


def test_a_stale_bill_match_approval_does_not_leave_the_match_status_matched(client):
    report_id, expense_id = _seed_bill_match(reasons="Amount agrees; the date is unclear.")
    _verify(client, _record(client, report_id, BILL_MATCH))
    approved = _expense_row(expense_id)
    assert approved.bill_match_status == "MATCHED" and approved.bill_match_reasons == f"{MANUAL_PREFIX} - Amount agrees; the date is unclear.", "fixture: the approval flipped the match"
    _reevaluate(report_id)  # unchanged evidence: the flip is applied once and never stacks
    assert _expense_row(expense_id).bill_match_reasons == approved.bill_match_reasons

    _edit_check(report_id, "Bill Amount Check", bill_amount=1234.0)  # the evidence behind the match changed; matching was NOT re-run (a revalidate-only flow)
    _reevaluate(report_id)

    assert _record(client, report_id, BILL_MATCH)["status"] == "OPEN"
    after = _expense_row(expense_id)
    assert after.bill_match_status == "REVIEW_REQUIRED", "a match confirmed only by an approval that no longer applies must not stay MATCHED"
    assert after.bill_match_reasons == "Amount agrees; the date is unclear.", "the reasons are restored without the manual-verification prefix"
    expense_status, report_status = _statuses(report_id)
    assert expense_status != "VERIFIED" and report_status != "ACCOUNT VERIFIED"

    _verify(client, _record(client, report_id, BILL_MATCH), note="re-checked")  # re-verifying on the new evidence confirms the match again, once
    again = _expense_row(expense_id)
    assert again.bill_match_status == "MATCHED" and again.bill_match_reasons == f"{MANUAL_PREFIX} - Amount agrees; the date is unclear."


def test_a_stale_bill_match_approval_without_original_reasons_restores_none(client):
    report_id, expense_id = _seed_bill_match(reasons=None)
    _verify(client, _record(client, report_id, BILL_MATCH))
    assert _expense_row(expense_id).bill_match_reasons == f"{MANUAL_PREFIX}."
    _edit_check(report_id, "Bill Amount Check", bill_amount=1234.0)
    _reevaluate(report_id)
    after = _expense_row(expense_id)
    assert after.bill_match_status == "REVIEW_REQUIRED" and after.bill_match_reasons is None


def test_a_match_that_matching_itself_confirmed_is_never_downgraded_by_a_stale_approval(client):
    """Only a match status that the manual-verification flip set is reverted. A genuinely MATCHED expense keeps its status."""
    report_id, expense_id = _seed_bill_match(reasons="Exact amount and date match.")
    _verify(client, _record(client, report_id, BILL_MATCH))
    db = SessionLocal()
    try:
        expense = db.get(Expense, expense_id)
        expense.bill_match_status, expense.bill_match_reasons = "MATCHED", "Exact amount and date match."  # as the matcher itself would set it
        db.commit()
    finally:
        db.close()
    _edit_check(report_id, "Bill Amount Check", bill_amount=1234.0)
    _reevaluate(report_id)
    after = _expense_row(expense_id)
    assert after.bill_match_status == "MATCHED" and after.bill_match_reasons == "Exact amount and date match."


# ================================================================ the verification-column migration through the real startup hook


@pytest.fixture
def startup_on(monkeypatch):
    from app import main as app_main

    seeded: list[int] = []

    def _run(engine):
        monkeypatch.setattr(app_main, "engine", engine)
        monkeypatch.setattr(app_main, "seed_all", lambda: seeded.append(1))
        app_main.on_startup()  # the application's real hook: run_light_migrations -> create_all -> seed (seeding recorded, not run)
        return seeded

    return _run


def test_startup_migrates_a_legacy_verification_table_and_a_legacy_row_is_then_evaluated_correctly(tmp_path, startup_on):
    from pathlib import Path

    db_file = tmp_path / "legacy_verifications.db"
    assert tmp_path.resolve() in db_file.resolve().parents and db_file.resolve() != Path(database.DB_PATH).resolve()
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        report = ExpenseReport(filename="s.pdf", original_filename="s.pdf", file_hash="startup-sig", file_size=1, source_path="unused.pdf", extraction_status="EXTRACTED", total_claimed=5100.0, er_id="ER-77201")
        session.add(report)
        session.commit()
        expense = Expense(expense_report_id=report.id, s_no=1, expense_date=date(2026, 7, 24), category="LODGING", claimed_amount=5100.0, bill_match_status="MATCHED",
                          trip_match_status="NOT_PROCESSED", validation_status="REVIEW_REQUIRED")
        user = User(name="Legacy", email="startup@example.com", role="ADMIN")
        session.add_all([expense, user])
        session.commit()
        remarks = "Room service / food included: Rs.357.00 of Rs.5,100.00 total. Requires policy review."
        session.add(_row(expense_report_id=report.id, expense_id=expense.id, bill_amount=357.0, remarks=remarks))
        session.add(ExceptionVerification(expense_report_id=report.id, expense_id=expense.id, exception_type=ROOM, original_severity="REVIEW REQUIRED",
                                          original_reason=remarks, verified_by_id=user.id, verification_note="legacy approval"))
        session.commit()
        report_id = report.id
    finally:
        session.close()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE exception_verifications DROP COLUMN evidence_signature"))
    assert "evidence_signature" not in {c["name"] for c in inspect(engine).get_columns("exception_verifications")}, "fixture must be a real legacy schema"

    seeded = startup_on(engine)
    startup_on(engine)  # a second launch is a no-op

    assert seeded == [1, 1] and "evidence_signature" in {c["name"] for c in inspect(engine).get_columns("exception_verifications")}
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        legacy = session.query(ExceptionVerification).one()
        assert legacy.evidence_signature is None and legacy.verification_note == "legacy approval", "the old row is intact and unsigned"
        ee.evaluate_report(session, report_id)  # the unsigned row, equal reason: applied and stamped
        record = session.query(ExceptionRecord).filter(ExceptionRecord.exception_type == ROOM).one()
        assert record.status == "RESOLVED" and record.verification_note == "legacy approval"
        stamped = session.query(ExceptionVerification).one()
        assert stamped.evidence_signature == ee.signature_for_record(session, record)
    finally:
        session.close()


# ================================================================ legacy (unsigned) approvals through a full reprocess


def test_a_legacy_unsigned_approval_with_an_equal_reason_survives_a_full_reprocess_and_is_stamped(client, older_lodging_report):
    report_a, report_b = older_lodging_report
    _verify(client, _exception_of(client, report_a, ROOM, 1), note="legacy approval")
    _legacy(report_a)
    before = _snapshot(_verification(report_a))
    assert before["evidence_signature"] is None
    other_before = _other_state(client, report_b)
    old_ids = _expense_ids(report_a)

    _reprocess(client, report_a)

    new_ids = _expense_ids(report_a)
    assert set(new_ids.values()).isdisjoint(old_ids.values()), "precondition: the expenses were recreated"
    review = _exception_of(client, report_a, ROOM, 1)
    assert review["status"] == "RESOLVED" and review["verification_note"] == "legacy approval" and NOTE not in review["reason"]
    after = _snapshot(_verification(report_a))
    assert after["id"] == before["id"] and after["expense_id"] == new_ids[1], "the carry-over moved the same row"
    assert after["evidence_signature"] is not None, "stamped"
    assert {k: v for k, v in after.items() if k not in ("expense_id", "evidence_signature")} == {k: v for k, v in before.items() if k not in ("expense_id", "evidence_signature")}
    assert _other_state(client, report_b) == other_before


def test_a_legacy_unsigned_approval_with_a_changed_reason_is_not_applied_after_a_full_reprocess(client, older_lodging_report):
    report_a, report_b = older_lodging_report
    _stale_inject_and_verify(client, report_a, note="approved at Rs.957")  # the approval quotes Rs.957.00
    _legacy(report_a)
    before = _snapshot(_verification_of(report_a, _expense_ids(report_a)[1]))
    assert before["evidence_signature"] is None and "Rs.957.00" in before["original_reason"]
    other_before = _other_state(client, report_b)

    _reprocess(client, report_a)

    new_ids = _expense_ids(report_a)
    review = _exception_of(client, report_a, ROOM, 1)
    assert "Rs.357.00" in review["reason"] and "Rs.957.00" not in review["reason"]
    assert review["status"] == "OPEN" and not review["verification_note"] and review["reason"].endswith(f" [{NOTE}]")
    after = _snapshot(_verification(report_a))
    assert after["id"] == before["id"] and after["expense_id"] == new_ids[1]
    assert after["evidence_signature"] is None, "left unsigned: it was not applied, so it was not stamped"
    assert {k: v for k, v in after.items() if k != "expense_id"} == {k: v for k, v in before.items() if k != "expense_id"}, "otherwise exactly as it was"
    expense_status, report_status = _statuses(report_a)
    assert expense_status != "VERIFIED" and report_status != "ACCOUNT VERIFIED"
    assert _other_state(client, report_b) == other_before

    _verify(client, review, note="re-verified on the current evidence")  # an explicit re-verification signs it
    signed = _snapshot(_verification(report_a))
    assert signed["id"] == before["id"] and signed["evidence_signature"] is not None and "Rs.357.00" in signed["original_reason"]
    assert _exception_of(client, report_a, ROOM, 1)["status"] == "RESOLVED"


# ================================================================ TRIP MATCH family: a stale approval must not leave trip_match_status MATCHED

TRIP_MATCH = "TRIP MATCH REVIEW REQUIRED"
TRIP_CHECK = "Batta Trip Match Check"


def _seed_trip_match(*, status="REVIEW_REQUIRED", reasons="Trip evidence was too weak to auto-confirm.", er_id="ER-77401", with_bill_review=False) -> tuple[int, int]:
    """One BATTA expense whose trip match needs review (optionally with an independent BILL MATCH review on the same expense)."""
    db = SessionLocal()
    try:
        report = ExpenseReport(filename=f"{er_id}.pdf", original_filename=f"{er_id}.pdf", file_hash=f"hash-{er_id}", file_size=1, source_path="unused.pdf",
                               extraction_status="EXTRACTED", total_claimed=500.0, er_id=er_id)
        db.add(report)
        db.commit()
        expense = Expense(expense_report_id=report.id, s_no=1, expense_date=date(2026, 7, 24), category="BATTA", claimed_amount=500.0,
                          bill_match_status="REVIEW_REQUIRED" if with_bill_review else "MATCHED",
                          bill_match_reasons="Amount agrees; the date is unclear." if with_bill_review else None,
                          trip_match_status=status, trip_match_reasons=reasons, validation_status="REVIEW_REQUIRED")
        db.add(expense)
        db.commit()
        common = dict(expense_report_id=report.id, expense_id=expense.id)
        db.add_all([
            _row(**common, check_type=TRIP_CHECK, claimed_amount=None, bill_amount=None, bill_date=None, remarks="The trip match needs review.", source_page=3),
            _row(**common, check_type="Policy Limit Check", result="PASS", claimed_amount=500.0, bill_amount=None, remarks="Within limit", policy_limit=900.0),
        ])
        if with_bill_review:
            db.add(_row(**common, check_type="Bill Amount Check", claimed_amount=500.0, bill_amount=510.0, remarks="The bill amount needs review.", source_page=4))
        db.commit()
        ee.evaluate_report(db, report.id)
        return report.id, expense.id
    finally:
        db.close()


def test_verifying_a_trip_match_review_flips_the_status_once_and_re_evaluation_keeps_it(client):
    report_id, expense_id = _seed_trip_match(status="REVIEW_REQUIRED", reasons="Trip evidence was too weak to auto-confirm.")
    _verify(client, _record(client, report_id, TRIP_MATCH), note="trip confirmed")
    approved = _expense_row(expense_id)
    assert approved.trip_match_status == "MATCHED" and approved.trip_match_reasons == f"{MANUAL_PREFIX} - Trip evidence was too weak to auto-confirm."
    for _ in range(2):
        _reevaluate(report_id)
    assert _record(client, report_id, TRIP_MATCH)["status"] == "RESOLVED"
    again = _expense_row(expense_id)
    assert again.trip_match_status == "MATCHED" and again.trip_match_reasons == approved.trip_match_reasons, "the prefix is applied once and never stacks"


@pytest.mark.parametrize("status", ["REVIEW_REQUIRED", "MULTIPLE_CANDIDATES"])
def test_a_stale_trip_match_approval_does_not_leave_the_trip_status_matched(client, status):
    report_id, expense_id = _seed_trip_match(status=status, reasons="Multiple trips are plausible matches; auditor review required.")
    _verify(client, _record(client, report_id, TRIP_MATCH))
    assert _expense_row(expense_id).trip_match_status == "MATCHED", "fixture: the approval flipped the match"

    _edit_check(report_id, TRIP_CHECK, source_page=9)  # the evidence behind the trip match changed; matching was NOT re-run (a revalidate-only flow)
    _reevaluate(report_id)

    stale = _record(client, report_id, TRIP_MATCH)
    assert stale["status"] == "OPEN" and stale["reason"].endswith(f" [{NOTE}]")
    after = _expense_row(expense_id)
    assert after.trip_match_status != "MATCHED", "a trip confirmed only by an approval that no longer applies must not stay MATCHED"
    assert after.trip_match_status == "REVIEW_REQUIRED", "the pre-approval status is not stored: an unconfirmed trip always comes back as REVIEW_REQUIRED"
    assert after.trip_match_reasons == "Multiple trips are plausible matches; auditor review required.", "the reasons are restored without the prefix"
    expense_status, report_status = _statuses(report_id)
    assert expense_status != "VERIFIED" and report_status != "ACCOUNT VERIFIED"

    _verify(client, stale, note="re-checked")  # re-verifying on the new evidence confirms it again, once
    assert _expense_row(expense_id).trip_match_status == "MATCHED"
    assert _expense_row(expense_id).trip_match_reasons == f"{MANUAL_PREFIX} - Multiple trips are plausible matches; auditor review required."


def test_a_stale_trip_match_approval_without_original_reasons_restores_none(client):
    report_id, expense_id = _seed_trip_match(reasons=None)
    _verify(client, _record(client, report_id, TRIP_MATCH))
    assert _expense_row(expense_id).trip_match_reasons == f"{MANUAL_PREFIX}."
    _edit_check(report_id, TRIP_CHECK, source_page=9)
    _reevaluate(report_id)
    after = _expense_row(expense_id)
    assert after.trip_match_status == "REVIEW_REQUIRED" and after.trip_match_reasons is None


def test_a_trip_match_the_matcher_itself_confirmed_is_never_downgraded_by_a_stale_approval(client):
    """The review here is about the matched trip's data (e.g. its dates), not about whether the trip matched: the status is the matcher's."""
    report_id, expense_id = _seed_trip_match(status="MATCHED", reasons="Embedded trip summary matched exactly.")
    _verify(client, _record(client, report_id, TRIP_MATCH))
    assert _expense_row(expense_id).trip_match_reasons == "Embedded trip summary matched exactly.", "a MATCHED status is not flipped, so it is not prefixed"
    _edit_check(report_id, TRIP_CHECK, source_page=9)
    _reevaluate(report_id)
    assert _record(client, report_id, TRIP_MATCH)["status"] == "OPEN"
    after = _expense_row(expense_id)
    assert after.trip_match_status == "MATCHED" and after.trip_match_reasons == "Embedded trip summary matched exactly."


def test_a_stale_trip_approval_does_not_touch_the_bill_match_and_vice_versa(client):
    report_id, expense_id = _seed_trip_match(with_bill_review=True)
    _verify(client, _record(client, report_id, TRIP_MATCH))
    _verify(client, _record(client, report_id, BILL_MATCH))
    both = _expense_row(expense_id)
    assert both.trip_match_status == "MATCHED" and both.bill_match_status == "MATCHED"

    _edit_check(report_id, TRIP_CHECK, source_page=9)  # only the TRIP evidence changes
    _reevaluate(report_id)
    assert _record(client, report_id, TRIP_MATCH)["status"] == "OPEN" and _record(client, report_id, BILL_MATCH)["status"] == "RESOLVED"
    after = _expense_row(expense_id)
    assert after.trip_match_status == "REVIEW_REQUIRED" and after.bill_match_status == "MATCHED"
    assert after.bill_match_reasons == f"{MANUAL_PREFIX} - Amount agrees; the date is unclear."

    _verify(client, _record(client, report_id, TRIP_MATCH))
    _edit_check(report_id, "Bill Amount Check", bill_amount=999.0)  # now only the BILL evidence changes
    _reevaluate(report_id)
    assert _record(client, report_id, BILL_MATCH)["status"] == "OPEN" and _record(client, report_id, TRIP_MATCH)["status"] == "RESOLVED"
    final = _expense_row(expense_id)
    assert final.bill_match_status == "REVIEW_REQUIRED" and final.trip_match_status == "MATCHED"
