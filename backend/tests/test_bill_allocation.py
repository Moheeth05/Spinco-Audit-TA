"""Regression tests for whole-report bill allocation
(bill_matcher.allocate_bills / reconcile_shared_bill_allocations) and for
which claim is treated as the "original" when a bill is genuinely reused.

Root cause these cover (found by replaying the real pipeline on real
documents, not assumed): every expense used to pick its best bill
independently, and nothing resolved the case where two DIFFERENT claims both
confidently picked the SAME bill. The Duplicate Bill Check then flagged the
claim with the higher S.No - not the claim the bill actually fits - so the
better-supported claim was the one reported as the duplicate, and the claim
that merely resembled it (same amount, but a different date) was left
"confidently matched" to someone else's bill.

Two real cases are reproduced here as deterministic fixtures. The values are
what the pipeline's own extraction produced from the approved source PDFs
(amounts, dates, pages, categories, extraction-derived text); third-party
payee names and document/transaction numbers were replaced with neutral
placeholders because they do not affect any signal asserted below.

  * ER-41115, S.No 20 and 25: both Rs.600 CAB CHARGES, on 2026-08-20 and
    2026-08-21. Only ONE bill (page 34, dated 2026-08-20) has an extracted
    amount. The 2026-08-21 page (44) has no readable amount - and is
    extracted as a Lodging page ("GUEST NAME"), not a cab bill.
  * ER-39832, S.No 5 and 9: both Rs.1,000 OWN CAR PETROL (2026-06-12 and
    2026-06-22). Only ONE bill (page 11, dated 2026-06-22) has an extracted
    amount; the 2026-06-12 fuel bill (page 7) has none.
"""

from datetime import date
from pathlib import Path

from app.services.extraction import bill_matcher

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _bill(bill_id, page, amount, bill_date, vendor=None, description=None, category_guess=None, bill_number=None):
    return {
        "id": bill_id,
        "page_number": page,
        "amount": amount,
        "bill_date": bill_date,
        "vendor": vendor,
        "description": description,
        "category_guess": category_guess,
        "bill_number": bill_number,
        "raw_text": None,
    }


def _expense(s_no, rank, amount, expense_date, category, details):
    return {
        "s_no": s_no,
        "expense_rank": rank,
        "claimed_amount": amount,
        "expense_date": expense_date,
        "category": category,
        "expense_details": details,
    }


def _confident_bill_ids(outcomes):
    return [o.matched_bill_id for o in outcomes if o is not None and o.status == "MATCHED" and o.matched_bill_id is not None]


# ---------- Real case 1: ER-41115 S.No 20 / S.No 25 ----------

ER41115_FIRST_BILL_PAGE = 4
ER41115_BILLS = [
    _bill(23, 28, None, None, "TAMANG TAXI SERVICE", "TAXI", "Cab/Taxi"),
    _bill(27, 34, 600.0, date(2026, 8, 20), "UPI payee A", None, None, "UPI-REF-0001"),
    _bill(35, 44, None, date(2026, 8, 21), "GUEST NAME", "GUEST NAME", "Lodging"),
]
ER41115_EXPENSES = [
    _expense(20, 20, 600.0, date(2026, 8, 20), "CAB CHARGES", "Hotel to Rangpo to Alkem"),
    _expense(25, 25, 600.0, date(2026, 8, 21), "CAB CHARGES", "Cab from ALKEM to hotel to rangpo"),
]


def test_er41115_shared_600_bill_is_owned_by_the_claim_it_fits_best():
    s20, s25 = bill_matcher.allocate_bills(ER41115_EXPENSES, ER41115_BILLS, first_bill_page=ER41115_FIRST_BILL_PAGE)

    # S.No 20: exact amount AND exact date (2026-08-20) -> keeps the bill.
    assert s20.status == "MATCHED"
    assert s20.matched_bill_id == 27

    # S.No 25 is dated a day later: the same bill cannot be *confidently*
    # allocated to it too - that is what manufactured the false DUPLICATE BILL.
    assert s25.status == "REVIEW_REQUIRED"
    assert s25.matched_bill_id is None
    assert _confident_bill_ids([s20, s25]) == [27]
    assert any("S.No 20" in reason for reason in s25.reasons)


def test_er41115_lodging_page_is_not_asserted_as_the_cab_claims_bill():
    """Page 44 shares S.No 25's date and has no readable amount, but it is
    extracted as a Lodging page. It must stay visible to the auditor as a
    candidate, but must NOT be presented as the claim's own bill (that would
    raise a category finding against a page nothing says belongs to this
    claim)."""
    _, s25 = bill_matcher.allocate_bills(ER41115_EXPENSES, ER41115_BILLS, first_bill_page=ER41115_FIRST_BILL_PAGE)
    assert s25.candidate_bill_id != 35
    assert 35 in [c.bill_id for c in s25.candidates]


def test_er41115_outcome_does_not_depend_on_processing_order():
    reversed_outcomes = bill_matcher.allocate_bills(
        list(reversed(ER41115_EXPENSES)), ER41115_BILLS, first_bill_page=ER41115_FIRST_BILL_PAGE
    )
    s25, s20 = reversed_outcomes
    assert s20.matched_bill_id == 27
    assert s25.status == "REVIEW_REQUIRED" and s25.matched_bill_id is None


# ---------- Real case 2: ER-39832 S.No 5 / S.No 9 ----------

ER39832_FIRST_BILL_PAGE = 3
ER39832_BILLS = [
    _bill(4, 6, None, None, "ELECTRICALS & HARDWARES | be Laas"),
    _bill(5, 7, None, date(2026, 6, 12), "PARAMOUNT AGENCIES", "Nozzle", "Fuel", "INV-1"),
    _bill(7, 9, None, date(2026, 6, 17), "DTDC Express Limited", "DTDC", "Courier", "CN-1"),
    _bill(9, 11, 1000.0, date(2026, 6, 22), "pinelabs viga/2q26", "DIESEL", "Fuel", "INV-2"),
]
ER39832_EXPENSES = [
    _expense(5, 5, 1000.0, date(2026, 6, 12), "OWN CAR PETROL", "IIT Palakkad LCMS installation"),
    _expense(9, 9, 1000.0, date(2026, 6, 22), "OWN CAR PETROL", "plant lipids LC BD under AMC"),
]


def test_er39832_earlier_claim_no_longer_takes_the_better_fitting_claims_bill():
    s5, s9 = bill_matcher.allocate_bills(ER39832_EXPENSES, ER39832_BILLS, first_bill_page=ER39832_FIRST_BILL_PAGE)

    # S.No 9 (exact amount + exact date 2026-06-22) owns the bill.
    assert s9.status == "MATCHED"
    assert s9.matched_bill_id == 9

    # S.No 5 had been MATCHED/HIGH to this bill despite a ten-day date gap.
    assert s5.status != "MATCHED"
    assert _confident_bill_ids([s5, s9]) == [9]


def test_er39832_unreadable_amount_bill_is_an_uncertain_candidate_not_a_match():
    """S.No 5's own-date fuel bill (page 7, 2026-06-12, Fuel) has no readable
    amount. It scores well enough to be matched on date/category/position
    alone, but a missing amount is not evidence the amount matches - it must
    be surfaced as the claim's candidate under review, never confirmed."""
    s5, _ = bill_matcher.allocate_bills(ER39832_EXPENSES, ER39832_BILLS, first_bill_page=ER39832_FIRST_BILL_PAGE)
    assert s5.status == "REVIEW_REQUIRED"
    assert s5.matched_bill_id is None
    assert s5.candidate_bill_id == 5


def test_missing_amount_earns_no_amount_credit():
    bill = _bill(1, 5, None, date(2026, 6, 12), "Some Vendor", None, None)
    expense = _expense(1, 1, 1000.0, date(2026, 6, 12), "OWN CAR PETROL", None)
    _, reasons = bill_matcher.score_pair(expense, bill, amount_is_unique=False, first_bill_page=3)
    assert not any(r.startswith("Amount:") for r in reasons)


# ---------- Distinct same-amount bills ----------


def test_two_distinct_same_amount_bills_on_different_dates_each_match_their_own_bill():
    bills = [
        _bill(201, 10, 600.0, date(2026, 8, 1)),
        _bill(202, 11, 600.0, date(2026, 8, 5)),
    ]
    expenses = [
        _expense(1, 1, 600.0, date(2026, 8, 1), "CAB CHARGES", None),
        _expense(2, 2, 600.0, date(2026, 8, 5), "CAB CHARGES", None),
    ]
    a, b = bill_matcher.allocate_bills(expenses, bills, first_bill_page=10)
    assert (a.status, a.matched_bill_id) == ("MATCHED", 201)
    assert (b.status, b.matched_bill_id) == ("MATCHED", 202)


def test_two_distinct_same_amount_bills_on_the_same_date_are_never_both_given_one_bill():
    """Same amount, same date, no vendor evidence on the second claim: the
    evidence cannot tell the two bills apart for it. It must NOT be quietly
    given the bill the first claim already owns, and it must NOT be quietly
    given the other bill just because that one is unclaimed - it stays under
    review."""
    d = date(2026, 8, 19)
    bills = [
        _bill(101, 10, 600.0, d, "Falcon Cabs", "Falcon Cabs Downtown Branch"),
        _bill(102, 11, 600.0, d),
    ]
    expenses = [
        _expense(1, 1, 600.0, d, "CAB CHARGES", "Falcon Cabs Downtown Branch"),
        _expense(2, 2, 600.0, d, "CAB CHARGES", None),
    ]
    a, b = bill_matcher.allocate_bills(expenses, bills, first_bill_page=10)
    assert (a.status, a.matched_bill_id) == ("MATCHED", 101)
    assert b.matched_bill_id is None
    assert b.status in ("MULTIPLE_CANDIDATES", "REVIEW_REQUIRED")
    assert len(_confident_bill_ids([a, b])) == len(set(_confident_bill_ids([a, b])))


# ---------- One bill, two claims of different strength (synthetic) ----------


def test_weaker_claim_for_a_shared_bill_goes_to_review_with_its_own_compatible_candidate():
    """Claim A fits bill X exactly (amount + date). Claim B has the same
    amount but is dated a day later; the only other bill dated that day (Y,
    a Cab/Taxi page) has an unreadable amount. B must not keep X confidently,
    and Y - category-compatible and date-matching - is its uncertain
    candidate."""
    bills = [
        _bill(1, 10, 600.0, date(2026, 8, 20), "UPI payee A"),
        _bill(2, 11, None, date(2026, 8, 21), "Metro Taxis", "TAXI", "Cab/Taxi"),
    ]
    expenses = [
        _expense(1, 1, 600.0, date(2026, 8, 20), "CAB CHARGES", "Cab ride"),
        _expense(2, 2, 600.0, date(2026, 8, 21), "CAB CHARGES", "Cab ride"),
    ]
    a, b = bill_matcher.allocate_bills(expenses, bills, first_bill_page=10)
    assert (a.status, a.matched_bill_id) == ("MATCHED", 1)
    assert b.status == "REVIEW_REQUIRED" and b.matched_bill_id is None
    assert b.candidate_bill_id == 2


# ---------- Genuine reuse of the same bill ----------


def test_genuine_reuse_of_the_same_bill_is_still_confirmed_for_both_claims():
    """Two claims with identical, exact amount+date evidence and no other bill
    anywhere: that IS the same document reused. Both stay confidently matched
    to it so the Duplicate Bill Check can flag it."""
    d = date(2026, 8, 19)
    bills = [_bill(301, 10, 600.0, d, "Falcon Cabs", "Falcon Cabs")]
    expenses = [
        _expense(1, 1, 600.0, d, "CAB CHARGES", "Falcon Cabs"),
        _expense(2, 2, 600.0, d, "CAB CHARGES", "Falcon Cabs"),
    ]
    a, b = bill_matcher.allocate_bills(expenses, bills, first_bill_page=10)
    assert _confident_bill_ids([a, b]) == [301, 301]


def test_identical_claims_with_an_independent_same_day_candidate_stay_under_review():
    """Same as above, but another bill dated that day exists with an
    unreadable amount - the second claim might be backed by it. The evidence
    cannot distinguish 'reused document' from 'two separate bills', so
    neither claim is confidently allocated and neither is called a duplicate."""
    d = date(2026, 8, 19)
    bills = [
        _bill(301, 10, 600.0, d, "Falcon Cabs", "Falcon Cabs"),
        _bill(302, 11, None, d, "Falcon Cabs", "Falcon Cabs", "Cab/Taxi"),
    ]
    expenses = [
        _expense(1, 1, 600.0, d, "CAB CHARGES", "Falcon Cabs"),
        _expense(2, 2, 600.0, d, "CAB CHARGES", "Falcon Cabs"),
    ]
    a, b = bill_matcher.allocate_bills(expenses, bills, first_bill_page=10)
    assert _confident_bill_ids([a, b]) == []
    assert a.status == "REVIEW_REQUIRED" and b.status == "REVIEW_REQUIRED"


# ---------- Split payments ----------


def test_split_payment_allocation_still_excludes_bills_claimed_by_an_earlier_expense():
    d12, d13 = date(2026, 8, 12), date(2026, 8, 13)
    bills = [
        _bill(8, 13, 1500.0, d12),
        _bill(11, 18, 1500.0, d13),
        _bill(12, 19, 2000.0, d13),
    ]
    expenses = [
        _expense(1, 1, 1500.0, d12, "CAB CHARGES", None),
        _expense(2, 2, 3500.0, d13, "CAB CHARGES", "Car booking"),
    ]
    first, second = bill_matcher.allocate_bills(expenses, bills, first_bill_page=13)
    assert (first.status, first.matched_bill_id) == ("MATCHED", 8)
    assert second.status == "REVIEW_REQUIRED"
    assert second.split_payment_bill_ids == (11, 12)


def test_batta_style_none_entries_are_skipped_and_do_not_shift_ranks():
    d = date(2026, 8, 19)
    bills = [_bill(1, 5, 600.0, d, "Falcon Cabs", "Falcon Cabs")]
    expenses = [None, _expense(2, 2, 600.0, d, "CAB CHARGES", "Falcon Cabs")]
    skipped, matched = bill_matcher.allocate_bills(expenses, bills, first_bill_page=4)
    assert skipped is None
    assert matched.matched_bill_id == 1


# ---------- Which claim is the "original" of a genuinely shared bill ----------


def test_original_claim_of_a_shared_bill_is_the_best_fit_not_the_lowest_sno(client):
    from app.database import SessionLocal
    from app.models.bill import Bill
    from app.models.expense_report import Expense
    from app.models.validation import ValidationResult
    from app.models.expense_report import ExpenseReport
    from app.services.validation.pipeline import validate_expense_report

    db = SessionLocal()
    try:
        report = ExpenseReport(
            filename="test.pdf",
            original_filename="test.pdf",
            file_hash="hash-ER-ORIG",
            file_size=100,
            source_path="unused.pdf",
            extraction_status="EXTRACTED",
            total_claimed=4000.0,
            er_id="ER-ORIG",
        )
        db.add(report)
        db.commit()
        db.refresh(report)
        bill = Bill(
            expense_report_id=report.id,
            page_number=5,
            amount=2000.0,
            bill_date=date(2026, 6, 22),
            category_guess="Fuel",
            extraction_status="EXTRACTED",
        )
        db.add(bill)
        db.commit()
        db.refresh(bill)

        # S.No 1 matches this bill poorly (dated 10 days earlier); S.No 2 is
        # the claim it actually fits (exact date). Both are confirmed on it.
        for s_no, claim_date, score in ((1, date(2026, 6, 12), 72), (2, date(2026, 6, 22), 110)):
            db.add(
                Expense(
                    expense_report_id=report.id,
                    s_no=s_no,
                    expense_date=claim_date,
                    category="OWN CAR PETROL",
                    claimed_amount=2000.0,
                    bill_id=bill.id,
                    candidate_bill_id=bill.id,
                    bill_amount=2000.0,
                    bill_date=date(2026, 6, 22),
                    bill_match_status="MATCHED",
                    bill_match_score=score,
                )
            )
        db.commit()

        validate_expense_report(db, report.id)

        by_s_no = {
            e.s_no: r
            for e in db.query(Expense).filter(Expense.expense_report_id == report.id).all()
            for r in db.query(ValidationResult).filter(
                ValidationResult.expense_id == e.id, ValidationResult.check_type == "Duplicate Bill Check"
            )
        }
        assert by_s_no[2].result == "PASS"  # the better fit is the original
        assert by_s_no[1].result == "EXCEPTION"
        assert by_s_no[1].exception_type == "DUPLICATE BILL"
    finally:
        db.close()


# ---------- Repeat processing must never preserve an obsolete bill assignment ----------


def _bill_assignment_state(report_id):
    """(violations, {bill page -> matched S.No}) for one report, read from the
    disposable test database."""
    from app.database import SessionLocal
    from app.models.bill import Bill
    from app.models.expense_report import Expense

    db = SessionLocal()
    try:
        bills = db.query(Bill).filter(Bill.expense_report_id == report_id).all()
        expenses = db.query(Expense).filter(Expense.expense_report_id == report_id).all()
        expenses_by_id = {e.id: e for e in expenses}
        bill_ids = {b.id for b in bills}
        claimants: dict[int, list] = {}
        for e in expenses:
            if e.bill_id is not None:
                claimants.setdefault(e.bill_id, []).append(e)

        violations = []
        for b in bills:
            owner = expenses_by_id.get(b.matched_expense_id) if b.matched_expense_id is not None else None
            if b.matched_expense_id is not None and (owner is None or owner.bill_id != b.id):
                violations.append(f"bill p{b.page_number}: matched_expense_id points at an expense that does not hold it")
            if (b.match_status == "MATCHED") != (b.id in claimants):
                violations.append(f"bill p{b.page_number}: match_status={b.match_status} but claimants={len(claimants.get(b.id, []))}")
            if b.id not in claimants and b.matched_expense_id is not None:
                violations.append(f"bill p{b.page_number}: unmatched bill still records an owner")
            if len(claimants.get(b.id, [])) > 1:
                best = max(claimants[b.id], key=lambda e: (e.bill_match_score or 0, -(e.s_no or 0)))
                if b.matched_expense_id != best.id:
                    violations.append(f"bill p{b.page_number}: shared bill's recorded owner is not its best-fit claimant")
                # More than one confident claimant is only legitimate for
                # genuine reuse: every claimant has the bill's readable amount
                # and exact date.
                for e in claimants[b.id]:
                    supported = (
                        b.amount is not None
                        and e.claimed_amount is not None
                        and abs(float(b.amount) - float(e.claimed_amount)) < 1.0
                        and b.bill_date is not None
                        and b.bill_date == e.expense_date
                    )
                    if not supported:
                        violations.append(f"bill p{b.page_number}: confidently shared with S.No {e.s_no}, whose evidence is not exact amount + exact date")
        for e in expenses:
            if e.bill_id is not None and e.bill_id not in bill_ids:
                violations.append(f"S.No {e.s_no}: bill_id points at a bill outside this report's current bills")
        mapping = {b.page_number: expenses_by_id[b.matched_expense_id].s_no for b in bills if b.matched_expense_id is not None}
        return violations, mapping
    finally:
        db.close()


def test_no_real_document_confidently_shares_a_bill_between_independent_claims(client):
    """Through the real pipeline on every real report tracked in the repo:
    no bill is confidently allocated to more than one claim unless each
    claimant has exact amount + exact date evidence (genuine reuse)."""
    reports = {}
    for name in ("ER-39832.pdf", "ER-40223.pdf", "ER-40706 (1).pdf", "ER-40782.pdf"):
        path = REPO_ROOT / name
        resp = client.post(
            "/api/uploads/expense-reports",
            files=[("files", (name, path.read_bytes(), "application/pdf"))],
        )
        result = resp.json()["results"][0]
        assert result["success"], name
        reports[name] = int(result["document_id"].split(":")[1])
    for name, report_id in reports.items():
        violations, mapping = _bill_assignment_state(report_id)
        assert violations == [], (name, violations)
        # Non-vacuous: every one of these reports yields confirmed matches when
        # extraction/OCR works, so "no violations" cannot be satisfied merely
        # because nothing was extracted.
        assert mapping, f"{name}: no bills were matched at all - extraction/OCR did not run"


def test_unreadable_amount_match_can_never_verify_the_expense(client, sample_pdf_path):
    """A bill whose own amount could not be read may stay a visible CANDIDATE (date, vendor and
    position can point at it) but is never confirmation of the claim: the matcher no longer
    returns MATCHED for it, and the claim must never come out verified either (the Bill
    Amount Check cannot pass without a readable bill amount). Real report ER-39832, S.No 7."""
    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (sample_pdf_path.name, sample_pdf_path.read_bytes(), "application/pdf"))],
    )
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]
    expenses = client.get(f"/api/expense-reports/{report_id}/expenses").json()
    assert not [e for e in expenses if e["bill_match_status"] == "MATCHED" and e["bill_amount"] is None]
    seven = next(e for e in expenses if e["s_no"] == 7)
    assert seven["bill_match_status"] == "REVIEW_REQUIRED", seven
    assert seven["validation_status"] in ("EXCEPTION", "REVIEW REQUIRED", "REVIEW_REQUIRED"), seven


def test_rematch_and_reprocess_never_preserve_an_obsolete_bill_assignment(client, sample_pdf_path):
    from app.database import SessionLocal
    from app.models.bill import Bill

    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (sample_pdf_path.name, sample_pdf_path.read_bytes(), "application/pdf"))],
    )
    doc_id = resp.json()["results"][0]["document_id"]
    report_id = int(doc_id.split(":")[1])

    violations, baseline = _bill_assignment_state(report_id)
    assert violations == []
    assert baseline  # the real document does produce confirmed matches

    # Plant an obsolete assignment on an unmatched bill (what a stale row from
    # an earlier run would look like), then re-run matching.
    db = SessionLocal()
    try:
        unmatched = db.query(Bill).filter(Bill.expense_report_id == report_id, Bill.matched_expense_id.is_(None)).first()
        assert unmatched is not None
        stale_page = unmatched.page_number
        any_expense_id = next(iter(db.execute(__import__("sqlalchemy").text("select id from expenses where expense_report_id = :r"), {"r": report_id})))[0]
        unmatched.match_status = "MATCHED"
        unmatched.matched_expense_id = any_expense_id
        db.commit()
    finally:
        db.close()

    for _ in range(2):
        assert client.post(f"/api/documents/{doc_id}/rematch-bills").status_code == 200
        violations, mapping = _bill_assignment_state(report_id)
        assert violations == []
        assert mapping == baseline
        assert stale_page not in mapping

    assert client.post(f"/api/documents/{doc_id}/reprocess").status_code == 200
    violations, mapping = _bill_assignment_state(report_id)
    assert violations == []
    assert mapping == baseline


# ---------- Real document, end to end through the real pipeline (ER-39832 is tracked in the repo) ----------


def test_er39832_real_document_no_longer_flags_the_better_fitting_claim_as_a_duplicate(client, sample_pdf_path):
    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (sample_pdf_path.name, sample_pdf_path.read_bytes(), "application/pdf"))],
    )
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]

    expenses = {e["s_no"]: e for e in client.get(f"/api/expense-reports/{report_id}/expenses").json()}
    assert expenses[9]["bill_match_status"] == "MATCHED"
    assert expenses[5]["bill_match_status"] != "MATCHED"

    exceptions = client.get(f"/api/expense-reports/{report_id}/exceptions").json()
    duplicates = [e for e in exceptions if e["exception_type"] == "DUPLICATE BILL"]
    assert duplicates == [], duplicates
