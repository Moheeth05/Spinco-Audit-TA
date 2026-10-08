"""Phase 5 policy validation engine tests.

Sections:
  1-25: pure unit tests against services/validation/checks.py (no DB) -
        each check function's PASS/EXCEPTION/REVIEW_REQUIRED/NOT_APPLICABLE
        branches, built with lightweight stand-ins for PolicyMaster/Bill
        rows (SimpleNamespace with just the attributes each check reads).
  26-30: DB/API-level integration tests via services/validation/pipeline.py
        and the /validate, /revalidate, /validations endpoints - duplicate
        detection across expenses, policy-version persistence, no
        duplicate rows on revalidation, the mandatory ER-39832 reference
        case, and a Phase 1-4 regression smoke check.
"""

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.services.validation import checks

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _policy(**kwargs):
    defaults = dict(
        category="OWN CAR PETROL",
        reimbursement_basis="LIMIT",
        limit_amount=8500.0,
        document_required="YES",
        policy_version=1,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _bill(**kwargs):
    defaults = dict(
        page_number=3,
        amount=2000.0,
        bill_date=date(2026, 6, 1),
        category_guess="Fuel",
        raw_text="",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------- 1-5: policy limit ----------


def test_1_policy_limit_within_limit_passes():
    result = checks.check_policy_limit(claimed_amount=5000.0, category_label="OWN CAR PETROL", policy=_policy())
    assert result.result == checks.PASS


def test_2_policy_limit_over_limit_is_exception():
    result = checks.check_policy_limit(claimed_amount=9000.0, category_label="OWN CAR PETROL", policy=_policy())
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "OVER LIMIT"


def test_3_policy_limit_exactly_at_limit_passes():
    result = checks.check_policy_limit(claimed_amount=8500.0, category_label="OWN CAR PETROL", policy=_policy())
    assert result.result == checks.PASS


def test_4_policy_limit_actual_basis_has_no_artificial_limit():
    policy = _policy(category="LODGING", reimbursement_basis="ACTUAL", limit_amount=None, document_required="DA")
    result = checks.check_policy_limit(claimed_amount=50000.0, category_label="LODGING", policy=policy)
    assert result.result == checks.NOT_APPLICABLE


def test_5_policy_limit_unknown_policy_is_review_required():
    result = checks.check_policy_limit(claimed_amount=1000.0, category_label="SOME UNKNOWN CATEGORY", policy=None)
    assert result.result == checks.REVIEW_REQUIRED


# ---------- 6-8: bill amount ----------


def test_6_bill_amount_exact_match_passes():
    result = checks.check_bill_amount(claimed_amount=2000.0, bill=_bill(amount=2000.0))
    assert result.result == checks.PASS


def test_7_bill_amount_mismatch_is_exception():
    result = checks.check_bill_amount(claimed_amount=2000.0, bill=_bill(amount=1800.0))
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "AMOUNT MISMATCH"


def test_8_bill_amount_no_plausible_candidate_is_review_required():
    result = checks.check_bill_amount(claimed_amount=2000.0, bill=None)
    assert result.result == checks.REVIEW_REQUIRED


# ---------- 9-11: bill date ----------


def test_9_bill_date_match_passes():
    d = date(2026, 6, 1)
    result = checks.check_bill_date(expense_date=d, bill=_bill(bill_date=d))
    assert result.result == checks.PASS


def test_10_bill_date_mismatch_is_exception():
    result = checks.check_bill_date(
        expense_date=date(2026, 6, 1), bill=_bill(bill_date=date(2026, 6, 5))
    )
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "DATE MISMATCH"


def test_11_bill_date_no_plausible_candidate_is_review_required():
    result = checks.check_bill_date(expense_date=date(2026, 6, 1), bill=None)
    assert result.result == checks.REVIEW_REQUIRED


# ---------- 12-15: required document ----------


def test_12_required_document_matched_passes():
    policy = _policy(document_required="YES")
    result = checks.check_required_document(bill_match_status="MATCHED", policy=policy, document_required=True)
    assert result.result == checks.PASS


def test_13_required_document_missing_bill_is_exception():
    policy = _policy(document_required="YES")
    result = checks.check_required_document(bill_match_status="NO_MATCH", policy=policy, document_required=True)
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "MISSING BILL"


def test_14_required_document_not_required_is_not_applicable():
    policy = _policy(category="LODGING", document_required="DNA")
    result = checks.check_required_document(bill_match_status="NO_MATCH", policy=policy, document_required=False)
    assert result.result == checks.NOT_APPLICABLE


def test_15_required_document_ambiguous_match_is_review_required():
    policy = _policy(document_required="YES")
    result = checks.check_required_document(
        bill_match_status="MULTIPLE_CANDIDATES", policy=policy, document_required=True
    )
    assert result.result == checks.REVIEW_REQUIRED


# ---------- 16-18: category consistency ----------


def test_16_category_consistency_match_passes():
    result = checks.check_category_consistency(
        category="OWN CAR PETROL", bill=_bill(category_guess="Fuel")
    )
    assert result.result == checks.PASS


def test_17_category_consistency_mismatch_is_exception():
    result = checks.check_category_consistency(
        category="OWN CAR PETROL", bill=_bill(category_guess="Lodging")
    )
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "CATEGORY MISMATCH"


def test_18_category_consistency_train_bus_interchangeable():
    result = checks.check_category_consistency(
        category="TRAIN / BUS FARE", bill=_bill(category_guess="Bus")
    )
    assert result.result == checks.PASS


# ---------- 19-20: duplicate bill ----------


def test_19_duplicate_bill_detected_is_exception():
    result = checks.check_duplicate_bill(bill_match_status="MATCHED", bill=_bill(), is_duplicate=True)
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "DUPLICATE BILL"


def test_20_duplicate_bill_not_duplicate_passes():
    result = checks.check_duplicate_bill(bill_match_status="MATCHED", bill=_bill(), is_duplicate=False)
    assert result.result == checks.PASS


def test_20b_duplicate_bill_not_applicable_when_no_document_required():
    """Post-UAT correctness fix: a category that needs no supporting document
    at all (BATTA) can still incidentally score a low-confidence match
    against some unrelated bill (bill matching runs uniformly - see
    bill_pipeline.py); flagging that as a duplicate is a meaningless finding
    since the document was never required as evidence in the first place.
    Confirmed on a real document: this used to produce ten spurious
    DUPLICATE BILL exceptions on BATTA rows once bill_parser's amount
    extraction got broader."""
    result = checks.check_duplicate_bill(
        bill_match_status="MATCHED", bill=_bill(), is_duplicate=True, document_required=False
    )
    assert result.result == checks.NOT_APPLICABLE


# ---------- 21-23: catering ----------


def test_21_catering_detected_is_review_required():
    """Post-UAT correction 2, Part 15: a REVIEW REQUIRED policy exception on
    an otherwise-matched bill, never a confirmed EXCEPTION."""
    result = checks.check_catering(
        category="TRAIN / BUS FARE", bill=_bill(raw_text="Fare Rs.500.00\nCatering charges Rs.150.00 included")
    )
    assert result.result == checks.REVIEW_REQUIRED


def test_22_catering_not_train_bus_is_not_applicable():
    result = checks.check_catering(category="OWN CAR PETROL", bill=_bill())
    assert result.result == checks.NOT_APPLICABLE


def test_23_catering_no_catering_text_passes():
    result = checks.check_catering(category="TRAIN / BUS FARE", bill=_bill(raw_text="Fare Rs.500.00"))
    assert result.result == checks.PASS


# ---------- 24-25: report total ----------


def test_24_total_match_passes():
    result = checks.check_total(extracted_sum=16845.0, reported_total=16845.0)
    assert result.result == checks.PASS


def test_25_total_mismatch_is_exception():
    result = checks.check_total(extracted_sum=16000.0, reported_total=16845.0)
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "TOTAL MISMATCH"


# ---------- 26-28: pipeline-level integration (DB, no real PDF needed) ----------


def _make_report(db, **kwargs):
    from app.models.expense_report import ExpenseReport

    defaults = dict(
        filename="test.pdf",
        original_filename="test.pdf",
        file_hash=f"hash-{id(kwargs)}-{kwargs.get('er_id', 'x')}",
        file_size=100,
        source_path="unused.pdf",
        extraction_status="EXTRACTED",
        total_claimed=2000.0,
    )
    defaults.update(kwargs)
    row = ExpenseReport(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_26_pipeline_duplicate_bill_detected_across_two_expenses(client):
    from app.database import SessionLocal
    from app.models.bill import Bill
    from app.models.expense_report import Expense
    from app.services.validation.pipeline import validate_expense_report

    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-DUP", total_claimed=4000.0)
        bill = Bill(
            expense_report_id=report.id,
            page_number=5,
            amount=2000.0,
            bill_date=date(2026, 6, 1),
            category_guess="Fuel",
            extraction_status="EXTRACTED",
        )
        db.add(bill)
        db.commit()
        db.refresh(bill)

        for s_no in (1, 2):
            db.add(
                Expense(
                    expense_report_id=report.id,
                    s_no=s_no,
                    expense_date=date(2026, 6, 1),
                    category="OWN CAR PETROL",
                    claimed_amount=2000.0,
                    bill_id=bill.id,
                    candidate_bill_id=bill.id,
                    bill_amount=2000.0,
                    bill_date=date(2026, 6, 1),
                    bill_match_status="MATCHED",
                )
            )
        db.commit()

        validate_expense_report(db, report.id)

        from app.models.validation import ValidationResult

        dup_results = (
            db.query(ValidationResult)
            .filter(ValidationResult.expense_report_id == report.id, ValidationResult.check_type == "Duplicate Bill Check")
            .all()
        )
        assert len(dup_results) == 2
        # Post-UAT correction 2, Parts 13-14: only the later claim (higher
        # s_no) is the duplicate - the earlier one is the original and
        # passes this check like any other uniquely-matched bill, so the
        # auditor isn't shown two symmetric "duplicate" findings for what
        # is really one original + one duplicate.
        by_expense_s_no = {r.expense_id: r for r in dup_results}
        expenses_by_id = {e.id: e for e in db.query(Expense).filter(Expense.expense_report_id == report.id).all()}
        results_by_s_no = {expenses_by_id[eid].s_no: r for eid, r in by_expense_s_no.items()}
        assert results_by_s_no[1].result == "PASS"
        assert results_by_s_no[2].result == "EXCEPTION"
        assert results_by_s_no[2].exception_type == "DUPLICATE BILL"
    finally:
        db.close()


def test_27_pipeline_policy_version_persisted(client):
    from app.database import SessionLocal
    from app.models.expense_report import Expense
    from app.services.validation.pipeline import validate_expense_report

    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-VER", total_claimed=5000.0)
        db.add(
            Expense(
                expense_report_id=report.id,
                s_no=1,
                expense_date=date(2026, 6, 1),
                category="OWN CAR PETROL",
                claimed_amount=5000.0,
                bill_match_status="NO_MATCH",
            )
        )
        db.commit()

        validate_expense_report(db, report.id)

        from app.models.policy import PolicyMaster
        from app.models.validation import ValidationResult

        active_policy = db.query(PolicyMaster).filter(PolicyMaster.category == "OWN CAR PETROL").first()
        limit_result = (
            db.query(ValidationResult)
            .filter(ValidationResult.expense_report_id == report.id, ValidationResult.check_type == "Policy Limit Check")
            .first()
        )
        assert limit_result.policy_version == active_policy.policy_version
        assert limit_result.policy_category == "OWN CAR PETROL"
    finally:
        db.close()


def test_28_revalidate_does_not_duplicate_rows(client):
    from app.database import SessionLocal
    from app.models.expense_report import Expense
    from app.services.validation.pipeline import validate_expense_report

    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-REVAL", total_claimed=1000.0)
        db.add(
            Expense(
                expense_report_id=report.id,
                s_no=1,
                expense_date=date(2026, 6, 1),
                category="COURIER CHARGES",
                claimed_amount=1000.0,
                bill_match_status="NO_MATCH",
            )
        )
        db.commit()
        report_id = report.id
    finally:
        db.close()

    resp1 = client.post(f"/api/expense-reports/{report_id}/revalidate")
    assert resp1.status_code == 200
    resp2 = client.post(f"/api/expense-reports/{report_id}/revalidate")
    assert resp2.status_code == 200

    validations = client.get(f"/api/expense-reports/{report_id}/validations").json()
    # 8 per-expense checks (Phase 6 added Batta Trip Match Check, which
    # correctly returns NOT_APPLICABLE here since this expense isn't BATTA)
    # + 1 report-level total check = 9, not 18.
    assert len(validations) == 9


# ---------- 29: mandatory ER-39832 reference case (real PDF) ----------


def test_29_er39832_reference_case_validation(client, sample_pdf_path):
    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (sample_pdf_path.name, sample_pdf_path.read_bytes(), "application/pdf"))],
    )
    result = resp.json()["results"][0]
    report_id = result["document_id"].split(":")[1]

    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    first = next(e for e in expenses if e["s_no"] == 1)
    assert first["claimed_amount"] == 2000.0

    ev = client.get(f"/api/expenses/{first['id']}/validations").json()
    limit_check = next(v for v in ev if v["check_type"] == "Policy Limit Check")
    assert limit_check["result"] == "PASS"

    amount_check = next(v for v in ev if v["check_type"] == "Bill Amount Check")
    # A fabricated 2500 mismatch must never appear - and given this document's
    # genuine 3-way ambiguity among real Rs.2,000 petrol receipts (see
    # bill_matcher's page-position signal, which narrows but does not
    # eliminate the tie), REVIEW_REQUIRED is the honest, spec-compliant
    # outcome here, not a forced PASS.
    assert amount_check["result"] in ("PASS", "REVIEW_REQUIRED")
    if amount_check["result"] == "PASS":
        assert amount_check["bill_amount"] == 2000.0
    assert "2500" not in (amount_check["remarks"] or "")


# ---------- 30: Phase 1-4 regression smoke ----------


def test_30_phase1_4_regression_still_works(client):
    assert len(client.get("/api/policy-master").json()) == 29

    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", ("ER-39832.pdf", (REPO_ROOT / "ER-39832.pdf").read_bytes(), "application/pdf"))],
    )
    result = resp.json()["results"][0]
    assert result["success"]
    assert result["extraction_status"] == "EXTRACTED"
    report_id = result["document_id"].split(":")[1]
    bills = client.get(f"/api/expense-reports/{report_id}/bills").json()
    assert len(bills) > 0
