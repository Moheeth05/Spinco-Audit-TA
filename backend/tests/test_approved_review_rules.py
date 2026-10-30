"""Two review rules, stated as policy and tested as such.

1. A bill whose amount could not be read may stay a visible CANDIDATE, but it never - by itself -
   confirms a claim: the matcher does not return MATCHED for it and the claim is never verified.
2. When several claims compete for one underlying bill and the allocation stays under review,
   the possible reuse stays visible (a 'DUPLICATE BILL REVIEW REQUIRED' item on every such claim)
   and none of them is verified automatically.
"""

from datetime import date
from types import SimpleNamespace

from app.services.extraction import bill_matcher
from app.services.validation import checks

D = date


def _bill(bill_id, amount, bill_date=D(2026, 7, 2), page=5):
    return {"id": bill_id, "page_number": page, "amount": amount, "bill_date": bill_date, "date_candidates": [],
            "vendor": "Neha Taxi", "description": "cab", "category_guess": "Cab/Taxi", "bill_number": None, "raw_text": None}


def _claim(amount=275.0, s_no=1):
    return {"s_no": s_no, "expense_rank": s_no, "claimed_amount": amount, "expense_date": D(2026, 7, 2),
            "category": "CAB CHARGES", "expense_details": "Neha Taxi ride"}


# ----------------------------------------------------------------------------- unreadable amount


def test_a_readable_agreeing_bill_is_matched():
    (outcome,) = bill_matcher.allocate_bills([_claim()], [_bill(1, 275.0)], first_bill_page=1)
    assert outcome.status == "MATCHED" and outcome.matched_bill_id == 1


def test_an_unreadable_amount_bill_is_a_candidate_never_a_match():
    """Same date, vendor, category and page position - everything but the amount."""
    (outcome,) = bill_matcher.allocate_bills([_claim()], [_bill(1, None)], first_bill_page=1)
    assert outcome.status == "REVIEW_REQUIRED"
    assert outcome.matched_bill_id is None and outcome.confidence is None
    assert outcome.candidate_bill_id == 1  # still visible to the auditor
    assert "amount could not be read" in " ".join(outcome.reasons)


def test_an_unreadable_amount_bill_does_not_displace_a_readable_one():
    outcomes = bill_matcher.allocate_bills([_claim()], [_bill(1, None, page=4), _bill(2, 275.0, page=6)], first_bill_page=1)
    assert outcomes[0].status == "MATCHED" and outcomes[0].matched_bill_id == 2


# ----------------------------------------------------------------------------- shared bill review


def _claims(*s_nos):
    return [SimpleNamespace(s_no=n, expense_date=D(2026, 9, 21), claimed_amount=1150.0) for n in s_nos]


def _shared(status, claims, expense, **kwargs):
    bill = SimpleNamespace(page_number=15, amount=1150.0)
    return checks.check_shared_bill_review(bill_match_status=status, bill=bill, sharing_claims=claims, expense=expense, **kwargs)


def test_claims_left_unresolved_on_one_bill_each_stay_visible_as_a_reuse_review():
    claims = _claims(4, 11, 38)
    for claim in claims:
        result = _shared("REVIEW_REQUIRED", claims, claim)
        assert result.result == checks.REVIEW_REQUIRED
        assert "reused" in result.details and "duplicate" in result.details
        assert all(f"S.No {c.s_no}" in result.details for c in claims if c is not claim)


def test_a_confirmed_match_is_judged_by_the_duplicate_bill_check_instead():
    claims = _claims(4, 11)
    assert _shared("MATCHED", claims, claims[0]).result == checks.NOT_APPLICABLE


def test_a_bill_with_one_claim_or_no_document_requirement_raises_nothing():
    claims = _claims(4)
    assert _shared("REVIEW_REQUIRED", claims, claims[0]).result == checks.NOT_APPLICABLE
    two = _claims(4, 11)
    assert _shared("REVIEW_REQUIRED", two, two[0], document_required=False).result == checks.NOT_APPLICABLE


def test_pipeline_three_claims_one_bill_unresolved_none_verified_all_flagged(client):
    from app.database import SessionLocal
    from app.models.bill import Bill
    from app.models.expense_report import Expense
    from app.models.validation import ExceptionRecord
    from app.services.exception_engine import evaluate_report
    from app.services.validation.pipeline import validate_expense_report
    from tests.test_validation import _make_report

    db = SessionLocal()
    try:
        report = _make_report(db, er_id="ER-SHARED", total_claimed=3450.0)
        bill = Bill(expense_report_id=report.id, page_number=15, amount=1150.0, bill_date=D(2026, 9, 21),
                    category_guess="Cab/Taxi", extraction_status="EXTRACTED")
        db.add(bill)
        db.commit()
        db.refresh(bill)
        for s_no in (4, 11, 38):
            db.add(Expense(expense_report_id=report.id, s_no=s_no, expense_date=D(2026, 9, 21), category="CAB CHARGES",
                           claimed_amount=1150.0, bill_id=None, candidate_bill_id=bill.id, bill_amount=1150.0,
                           bill_date=D(2026, 9, 21), bill_match_status="REVIEW_REQUIRED"))
        db.commit()

        validate_expense_report(db, report.id)
        evaluate_report(db, report.id)

        expenses = db.query(Expense).filter(Expense.expense_report_id == report.id).all()
        assert {e.validation_status for e in expenses} == {"REVIEW_REQUIRED"}  # none verified
        records = db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report.id).all()
        flagged = {r.expense_id for r in records if r.exception_type == "DUPLICATE BILL REVIEW REQUIRED"}
        assert flagged == {e.id for e in expenses}
    finally:
        db.close()
