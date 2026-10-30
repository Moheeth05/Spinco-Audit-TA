"""Deterministic policy-validation checks. Each function is a pure function
over plain values (never touches the database or an ORM session directly),
so the whole engine is testable independently of FastAPI - see
services/validation/pipeline.py for the orchestrator that wires these
against real Expense/Bill/PolicyMaster rows.

Every check returns a CheckResult with one of exactly four results:
PASS | EXCEPTION | REVIEW_REQUIRED | NOT_APPLICABLE. None of these
functions ever invents a bill amount, a bill date, a vendor, or a policy -
insufficient evidence is always REVIEW_REQUIRED, never a guessed PASS.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from app.services.category_map import (
    expected_bill_label,
    is_multi_service_aggregator,
    normalize_category,
    resolve_catering_amount,
    resolve_catering_mentioned,
    resolve_room_service_food_amount,
    resolve_room_service_food_mentioned,
)
from app.services.extraction.dates import read_dates
from app.services.extraction.money import amounts_match

PASS = "PASS"
EXCEPTION = "EXCEPTION"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass
class CheckResult:
    check_type: str
    result: str
    details: str | None = None
    remarks: str | None = None
    exception_type: str | None = None
    claimed_amount: float | None = None
    policy_limit: float | None = None
    bill_amount: float | None = None
    expense_date: date | None = None
    bill_date: date | None = None
    source_page: int | None = None


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _fmt(amount) -> str:
    d = _to_decimal(amount)
    return f"Rs.{d:,.2f}" if d is not None else "Rs.0.00"


def _fmt_date(d: date | None) -> str:
    return d.strftime("%d/%m/%Y") if d else "unknown date"


# Catering / room-service-food evidence for a bill: the charge read from the
# bill's FULL text at extraction time (stored on the Bill row - raw_text itself
# is capped at 2,000 characters) when there is one, otherwise the detectors on
# raw_text exactly as before. getattr() defaults keep a bill object / stand-in
# that has none of the stored columns (an old row, a unit-test stub) on the old
# path. See category_map.resolve_*.


def _catering_evidence(bill) -> tuple[float | None, bool]:
    amount = resolve_catering_amount(bill.raw_text, getattr(bill, "catering_amount", None))
    return amount, resolve_catering_mentioned(bill.raw_text, getattr(bill, "catering_mentioned", None), amount)


def _room_service_food_evidence(bill) -> tuple[float | None, bool]:
    flag = getattr(bill, "room_service_food_mentioned", None)
    amount = resolve_room_service_food_amount(bill.raw_text, getattr(bill, "room_service_food_amount", None), flag)
    return amount, resolve_room_service_food_mentioned(bill.raw_text, flag, amount)


# ---------- 1. Policy limit ----------


def check_policy_limit(*, claimed_amount, category_label: str, policy) -> CheckResult:
    check_type = "Policy Limit Check"
    if policy is None:
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details=f"Policy configuration was not found for expense category '{category_label}'.",
            claimed_amount=claimed_amount,
        )

    if policy.reimbursement_basis == "ACTUAL":
        return CheckResult(
            check_type=check_type,
            result=NOT_APPLICABLE,
            details=f"{policy.category}: reimbursement basis is ACTUAL - no fixed limit applies.",
            remarks="Not applicable",
            claimed_amount=claimed_amount,
        )

    claimed = _to_decimal(claimed_amount)
    limit = _to_decimal(policy.limit_amount)
    if claimed is None:
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details=f"{policy.category} limit {_fmt(limit)}",
            remarks="Claimed amount could not be reliably determined.",
            policy_limit=float(limit) if limit is not None else None,
        )
    if limit is None:
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details=f"{policy.category}: policy limit is not configured.",
            claimed_amount=claimed_amount,
        )

    if claimed <= limit:
        return CheckResult(
            check_type=check_type,
            result=PASS,
            details=f"{policy.category} limit {_fmt(limit)}",
            remarks="Within limit",
            claimed_amount=claimed_amount,
            policy_limit=float(limit),
        )

    diff = claimed - limit
    return CheckResult(
        check_type=check_type,
        result=EXCEPTION,
        exception_type="OVER LIMIT",
        details=f"{policy.category} limit {_fmt(limit)}",
        remarks=f"Claimed amount {_fmt(claimed)} exceeds the policy limit of {_fmt(limit)} by {_fmt(diff)}.",
        claimed_amount=claimed_amount,
        policy_limit=float(limit),
    )


# ---------- 2. Bill amount ----------


def check_bill_amount(
    *,
    claimed_amount,
    bill,
    bill_match_reasons: str | None = None,
    document_required: bool | None = None,
    category: str | None = None,
) -> CheckResult:
    """`bill` is the best PLAUSIBLE candidate (bill_matcher.py's
    candidate_bill_id), not necessarily a CONFIRMED match - the combined
    ranking score that decides MATCHED/REVIEW_REQUIRED/MULTIPLE_CANDIDATES
    is a candidate-selection signal only (see bill_matcher.py's module
    docstring); it must not gate whether this candidate's own amount
    evidence gets examined. None here means no candidate cleared even the
    minimum plausibility floor (bill_match_status == NO_MATCH)."""
    check_type = "Bill Amount Check"
    if document_required is False:
        # No supporting document is expected for this category at all (e.g.
        # BATTA) - bill matching still runs uniformly across every expense
        # (see bill_pipeline.py) so it may have scored some unrelated bill,
        # but that outcome is meaningless here and must never surface as
        # review-required noise on an expense that was never supposed to
        # have a bill in the first place (spec: BATTA must never be sent
        # through bill-matching review at all).
        return CheckResult(check_type=check_type, result=NOT_APPLICABLE, details="No supporting document is required for this category.", remarks="Not applicable")
    if bill is None:
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details=bill_match_reasons or "No plausible matching bill is available for this expense.",
            claimed_amount=claimed_amount,
        )

    claimed = _to_decimal(claimed_amount)
    bill_amt = _to_decimal(bill.amount)
    if bill_amt is None:
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details="The matched bill's amount could not be reliably extracted.",
            claimed_amount=claimed_amount,
            source_page=bill.page_number,
        )
    if claimed is None:
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details="Claimed amount could not be reliably determined.",
            bill_amount=float(bill_amt),
            source_page=bill.page_number,
        )

    # claimed == bill_amt is an exact match; amounts_match() also accepts
    # either whole-rupee neighbour of a decimal bill amount (an employee
    # commonly enters the bill total rounded/truncated to a whole rupee, in
    # EITHER direction - never a looser percentage-based tolerance, and for
    # an already-whole-rupee bill only that exact value is accepted - see
    # money.py's amounts_match()).
    if amounts_match(claimed, bill_amt):
        remarks = "Amount matches" if claimed == bill_amt else "Amount matches (claimed is the bill total rounded to the nearest rupee)"
        return CheckResult(
            check_type=check_type,
            result=PASS,
            details=f"Claimed {_fmt(claimed)} vs Bill {_fmt(bill_amt)}",
            remarks=remarks,
            claimed_amount=claimed_amount,
            bill_amount=float(bill_amt),
            source_page=bill.page_number,
        )

    # A travel bill's own total is "all inclusive" and can legitimately
    # exceed what's claimed when it bundles in a catering charge (see
    # check_catering below, which separately flags this for review) - if
    # excluding that catering charge brings the bill in line with what was
    # claimed (exactly, or by the same whole-rupee-rounding rule above),
    # this is not a real amount mismatch (Part 17: catering must not break
    # bill matching).
    catering_amount, _ = _catering_evidence(bill)
    if catering_amount is not None:
        catering = _to_decimal(catering_amount)
        if catering is not None and amounts_match(claimed, bill_amt - catering):
            excl = bill_amt - catering
            remarks = (
                "Amount matches once catering is excluded"
                if claimed == excl
                else "Amount matches once catering is excluded (claimed is that total rounded to the nearest rupee)"
            )
            return CheckResult(
                check_type=check_type,
                result=PASS,
                details=f"Claimed {_fmt(claimed)} vs Bill {_fmt(bill_amt)} (matches once catering of {_fmt(catering)} is excluded)",
                remarks=remarks,
                claimed_amount=claimed_amount,
                bill_amount=float(bill_amt),
                source_page=bill.page_number,
            )

    # The same rule for a LODGING bill's room-service / food charge (see
    # check_room_service_food below, which separately flags it for review).
    # Strictly LODGING-only: food on any other category's bill (e.g. STAFF
    # WELFARE, MARKETING EXPENSES - FOOD) is the claimed expense itself, never
    # something to subtract.
    if normalize_category(category) == "LODGING":
        room_food_amount, _ = _room_service_food_evidence(bill)
        if room_food_amount is not None:
            room_food = _to_decimal(room_food_amount)
            if room_food is not None and amounts_match(claimed, bill_amt - room_food):
                excl = bill_amt - room_food
                remarks = (
                    "Amount matches once room service / food is excluded"
                    if claimed == excl
                    else "Amount matches once room service / food is excluded (claimed is that total rounded to the nearest rupee)"
                )
                return CheckResult(
                    check_type=check_type,
                    result=PASS,
                    details=f"Claimed {_fmt(claimed)} vs Bill {_fmt(bill_amt)} (matches once room service / food of {_fmt(room_food)} is excluded)",
                    remarks=remarks,
                    claimed_amount=claimed_amount,
                    bill_amount=float(bill_amt),
                    source_page=bill.page_number,
                )

    return CheckResult(
        check_type=check_type,
        result=EXCEPTION,
        exception_type="AMOUNT MISMATCH",
        details=f"Claimed {_fmt(claimed)} vs Bill {_fmt(bill_amt)}",
        remarks=f"Claimed amount {_fmt(claimed)} does not match bill amount {_fmt(bill_amt)}.",
        claimed_amount=claimed_amount,
        bill_amount=float(bill_amt),
        source_page=bill.page_number,
    )


# ---------- 3. Bill date ----------


def check_bill_date(
    *, expense_date: date | None, bill, bill_match_reasons: str | None = None, document_required: bool | None = None
) -> CheckResult:
    """`bill` is the best plausible candidate, not necessarily confirmed -
    see check_bill_amount's docstring for why."""
    check_type = "Bill Date Check"
    if document_required is False:
        return CheckResult(check_type=check_type, result=NOT_APPLICABLE, details="No supporting document is required for this category.", remarks="Not applicable")
    if bill is None:
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details=bill_match_reasons or "No plausible matching bill is available for this expense.",
            expense_date=expense_date,
        )
    if bill.bill_date is None:
        raw = getattr(bill, "bill_date_raw", None)
        candidates = [c for c in (getattr(bill, "bill_date_candidates", None) or "").split("|") if c]
        if len(candidates) > 1:
            # Ambiguous day/month order that no evidence in the report settles. Both readings are
            # kept for the auditor; the claimed date is NOT used to pick one - a reading that
            # happens to equal it is a coincidence, not evidence of the format.
            note = (
                f"The bill's date '{raw}' is ambiguous ({' or '.join(candidates)}) and nothing in this report "
                "establishes its day/month order."
            )
            if expense_date is not None and expense_date.isoformat() in candidates:
                note += (
                    f" One reading equals the claimed date {expense_date.isoformat()}, but that coincidence is not "
                    "evidence of the order, so the date is not treated as matching."
                )
            return CheckResult(
                check_type=check_type,
                result=REVIEW_REQUIRED,
                details=note,
                remarks=note,
                expense_date=expense_date,
                source_page=bill.page_number,
            )
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details="The matched bill's date could not be reliably extracted.",
            expense_date=expense_date,
            source_page=bill.page_number,
        )
    if expense_date is None:
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details="The expense's own date could not be reliably determined.",
            bill_date=bill.bill_date,
            source_page=bill.page_number,
        )

    if expense_date == bill.bill_date:
        return CheckResult(
            check_type=check_type,
            result=PASS,
            details=f"Expense {_fmt_date(expense_date)} vs Bill {_fmt_date(bill.bill_date)}",
            remarks="Dates match",
            expense_date=expense_date,
            bill_date=bill.bill_date,
            source_page=bill.page_number,
        )

    # A multi-date document (a hotel folio: arrival, departure, invoice date; a fuel slip:
    # print date and transaction times) names one date as THE bill date, but the claimed
    # date can be legitimately printed elsewhere on it. Only a date the document itself
    # establishes counts (unambiguous, or order proven by the document) - an ambiguous
    # token that could be read as the claimed date is never enough, and the claimed date
    # is never used to choose between readings.
    for reading in read_dates(getattr(bill, "raw_text", None)):
        if reading.value == expense_date and reading.method in ("iso", "month_name", "numeric", "cue", "same_document"):
            return CheckResult(
                check_type=check_type,
                result=PASS,
                details=f"Expense {_fmt_date(expense_date)} vs Bill {_fmt_date(bill.bill_date)}",
                remarks=(
                    f"The claimed date {_fmt_date(expense_date)} is printed on the bill ('{reading.raw}'); the bill's "
                    f"primary date field reads {_fmt_date(bill.bill_date)}."
                ),
                expense_date=expense_date,
                bill_date=bill.bill_date,
                source_page=bill.page_number,
            )

    # A bill_date that is present was established from evidence (an unambiguous date,
    # an explicit cue, or same-document / same-template proof of the format), so a
    # difference from the claimed date - with that date nowhere else on the bill - is a
    # real DATE MISMATCH. An unresolved date never reaches here (handled above).
    return CheckResult(
        check_type=check_type,
        result=EXCEPTION,
        exception_type="DATE MISMATCH",
        details=f"Expense {_fmt_date(expense_date)} vs Bill {_fmt_date(bill.bill_date)}",
        remarks=f"Expense date {_fmt_date(expense_date)} does not match bill date {_fmt_date(bill.bill_date)}.",
        expense_date=expense_date,
        bill_date=bill.bill_date,
        source_page=bill.page_number,
    )


# ---------- 3b. Payment status ----------


def check_payment_status(*, bill, document_required: bool | None = None) -> CheckResult:
    """A UPI/wallet screenshot is proof of a TRANSACTION, and only a COMPLETED
    one. A screenshot showing the payment PENDING or FAILED can never support a
    claim, however well its amount and date agree. A page that prints no status
    is not penalised (many real screenshots simply omit one)."""
    check_type = "Payment Status Check"
    status = getattr(bill, "payment_status", None) if bill is not None else None
    if document_required is False or bill is None or status not in ("PENDING", "FAILED"):
        return CheckResult(check_type=check_type, result=NOT_APPLICABLE, details="No pending or failed payment evidence.", remarks="Not applicable")
    note = (
        f"The supporting payment screenshot shows the transaction as {status}; a {status.lower()} transaction is "
        "not proof of a completed payment."
    )
    return CheckResult(
        check_type=check_type, result=REVIEW_REQUIRED, details=note, remarks=note, source_page=bill.page_number
    )


# ---------- 4. Required document ----------


def check_required_document(
    *, bill_match_status: str, policy, document_required: bool | None, bill_match_reasons: str | None = None
) -> CheckResult:
    """Answers only "is a plausible supporting document present" - whether
    that document's evidence actually CONFIRMS the claim is a separate
    question, answered independently by check_bill_amount/check_bill_date/
    check_category_consistency against the same candidate. MATCHED (a
    confident, uniquely-best candidate) and REVIEW_REQUIRED (a single
    plausible candidate that just didn't clear the auto-confirm score) both
    mean a real document was found - PASS here, same as before; the other
    three checks are what surface a genuine field-level problem with it.
    MULTIPLE_CANDIDATES is a materially different kind of uncertainty (which
    bill, if either, is even the right one) and stays REVIEW_REQUIRED here
    regardless of how the top candidate's own fields check out."""
    check_type = "Required Document Check"
    if policy is None:
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details="Document requirement could not be determined - no policy configuration found.",
        )
    if document_required is None:
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details=f"Document requirement '{policy.document_required}' could not be interpreted.",
        )
    if document_required is False:
        return CheckResult(
            check_type=check_type,
            result=NOT_APPLICABLE,
            details=f"{policy.category}: no supporting document is required ({policy.document_required}).",
            remarks="Not applicable",
        )

    if bill_match_status in ("MATCHED", "REVIEW_REQUIRED"):
        return CheckResult(check_type=check_type, result=PASS, details="A plausible supporting bill was found.", remarks="Document present")
    if bill_match_status == "NO_MATCH":
        return CheckResult(
            check_type=check_type,
            result=EXCEPTION,
            exception_type="MISSING BILL",
            details=f"{policy.category} requires a supporting document ({policy.document_required}).",
            remarks=bill_match_reasons or "Required supporting document was not found for this expense.",
        )
    return CheckResult(
        check_type=check_type,
        result=REVIEW_REQUIRED,
        details=bill_match_reasons or "Bill matching for this expense was ambiguous (multiple candidates) - which document applies could not be confidently determined.",
    )


# ---------- 5. Category consistency ----------


def check_category_consistency(
    *,
    category: str | None,
    bill,
    document_required: bool | None = None,
    bill_match_confidence: str | None = None,
    payment_evidence_agrees: bool = False,
) -> CheckResult:
    """`bill` is the best plausible candidate, not necessarily confirmed -
    see check_bill_amount's docstring for why. This check is what prevents
    an amount+date match from auto-verifying a claim whose actual
    service/product contradicts the claimed category (e.g. a flight ticket
    matched by coincidence to a LODGING claim) - it must keep running
    against a plausible candidate even when the combined ranking score
    didn't clear the auto-confirm bar, not only once a match is confirmed."""
    check_type = "Category Consistency Check"
    if document_required is False:
        return CheckResult(check_type=check_type, result=NOT_APPLICABLE, details="No supporting document is required for this category.", remarks="Not applicable")
    if bill is None:
        return CheckResult(
            check_type=check_type,
            result=NOT_APPLICABLE,
            details="No plausible matching bill is available to compare against.",
            remarks="Not applicable",
        )
    if not bill.category_guess:
        # Missing category evidence is a different, weaker kind of
        # uncertainty than CONFLICTING evidence (handled below) - it means
        # the bill's content didn't contain a recognizable category keyword
        # at all (common on a payment-app screenshot that only shows an
        # amount and a payee, e.g. a UPI "Paid to <name>" transfer), not that
        # anything on the bill contradicts the claim. When the bill was
        # otherwise confidently matched (HIGH confidence: amount, date, and
        # usually more all independently agreed), withholding PASS on this
        # one missing, secondary field alone would be exactly the "false
        # REVIEW REQUIRED for an absent optional field" failure mode this
        # was raised to fix - confirmed on a real case (a CAB CHARGES UPI
        # transfer to an individual, a HIGH-confidence amount+date match).
        #
        # EXCEPT when the bill is from a known multi-service travel
        # aggregator (Cleartrip, MakeMyTrip, etc.) - there, "no category
        # evidence" is a meaningfully different, more ambiguous signal: the
        # vendor itself books flights, hotels, AND ground transport under one
        # brand, so an amount+date match alone says nothing about WHICH
        # service this document represents. Auto-passing that would be
        # exactly "pass an expense simply because the vendor is a travel
        # aggregator", explicitly required not to happen - this always stays
        # REVIEW_REQUIRED with a specific reason, regardless of match
        # confidence (confirmed real case: a LODGING claim's matched bill was
        # a PhonePe payment screenshot reading "To Make My Trip" - no hotel/
        # flight/transport term anywhere, genuinely indeterminate).
        #
        # A weaker (MEDIUM/LOW) non-aggregator match with no category
        # evidence at all stays REVIEW_REQUIRED too, unchanged - there the
        # match itself is still uncertain, so withholding evidence doesn't
        # have the same "secondary field on an otherwise-solid match"
        # character.
        if is_multi_service_aggregator(getattr(bill, "vendor", None)) or is_multi_service_aggregator(getattr(bill, "raw_text", None)):
            return CheckResult(
                check_type=check_type,
                result=REVIEW_REQUIRED,
                details=(
                    "The matched bill is from a multi-service travel-booking aggregator and its content did not "
                    "establish which service (flight, lodging, or ground transport) it represents - the vendor "
                    "name alone is not evidence of category."
                ),
                remarks="Aggregator vendor with no service-type evidence - needs manual review",
                source_page=bill.page_number,
            )
        if bill_match_confidence == "HIGH":
            return CheckResult(
                check_type=check_type,
                result=PASS,
                details="The matched bill's content did not contain a recognizable category keyword, but the match itself (amount, date) is HIGH confidence with no conflicting evidence.",
                remarks="Category evidence absent, not conflicting - match is otherwise strong",
                source_page=bill.page_number,
            )
        if payment_evidence_agrees:
            # A completed-payment record is evidence of a transaction, not a merchant invoice: it
            # naturally carries no category wording. With amount AND date agreeing and nothing
            # contradicting the claim, the category the employee listed stands (it is never
            # rewritten from the screenshot). The match may be MEDIUM only because it is an
            # equal-evidence pairing - already flagged as ambiguous in the match reasons.
            return CheckResult(
                check_type=check_type,
                result=PASS,
                details="Payment record with agreeing amount and date; it carries no category wording, so the claimed category is accepted as listed.",
                remarks="Category evidence absent, not conflicting - transaction amount and date agree",
                source_page=bill.page_number,
            )
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details="The matched bill's content was insufficient to verify category consistency.",
            source_page=bill.page_number,
        )

    expected = expected_bill_label(category)
    if expected is None:
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details=f"No known bill-evidence expectation is configured for category '{category}'.",
            source_page=bill.page_number,
        )

    compatible = bill.category_guess == expected or {bill.category_guess, expected} == {"Train", "Bus"}
    if compatible:
        return CheckResult(
            check_type=check_type,
            result=PASS,
            details=f"Expected evidence: {expected}; bill evidence: {bill.category_guess}",
            remarks="Category evidence is consistent",
            source_page=bill.page_number,
        )
    return CheckResult(
        check_type=check_type,
        result=EXCEPTION,
        exception_type="CATEGORY MISMATCH",
        details=f"Expected evidence: {expected}; bill evidence: {bill.category_guess}",
        remarks=(
            f"Supporting bill does not appear consistent with the claimed expense category: claimed "
            f"{normalize_category(category) or category} (expects {expected} evidence), but the bill on page "
            f"{bill.page_number} reads as {bill.category_guess} evidence."
        ),
        source_page=bill.page_number,
    )


# ---------- 5b. Multi-page bill grouping review ----------


def check_bill_grouping_review(*, bill) -> CheckResult:
    """A bill assembled from bill_grouping.py's conservative continuation-
    evidence grouping (2+ PDF pages treated as one logical bill - see
    models/bill.py's Bill.page_number_end) must never be treated as fully
    confirmed on amount/date/category confidence alone. The page grouping
    itself is a judgment call bill_pipeline.py deliberately never trusts
    silently (every grouped bill's own extraction_status starts
    REVIEW_REQUIRED) - this check is what makes that uncertainty actually
    block ACCOUNT VERIFIED, rather than being overridden the moment the
    grouped bill's amount/date/category all happen to look confident
    (confirmed real gap: without this check, a grouped bill with a clean
    amount+date+category match reached ACCOUNT VERIFIED with zero
    exceptions, even though the page-grouping that produced those fields was
    never independently confirmed by a human). Checked against the same
    candidate bill the other amount/date/category checks use - see
    check_bill_amount's docstring for why that's the right evidence source
    even before a match is confirmed."""
    check_type = "Bill Grouping Review Check"
    if bill is None or bill.page_number_end is None:
        return CheckResult(
            check_type=check_type,
            result=NOT_APPLICABLE,
            details="This bill was not assembled from multiple pages.",
            remarks="Not applicable",
        )
    return CheckResult(
        check_type=check_type,
        result=REVIEW_REQUIRED,
        details=(
            f"This bill was assembled from pages {bill.page_number}-{bill.page_number_end} based on continuation "
            "evidence, not a single page - verify the page grouping is correct before treating this as confirmed."
        ),
        remarks="Multi-page bill grouping requires manual confirmation.",
        source_page=bill.page_number,
    )


# ---------- 5c. One bill, several claims, allocation unresolved ----------


def check_shared_bill_review(*, bill_match_status: str, bill, sharing_claims: list, expense, document_required: bool | None = None) -> CheckResult:
    """Several claims whose amount the SAME bill supports, where the matcher could not decide
    which (if any) owns it - e.g. three Rs.1,150 claims and one Rs.1,150 receipt, with other
    same-day pages whose amount is unreadable. Un-allocating them must not hide the possible
    reuse: each such claim stays visible as a duplicate/reuse review item and none is
    verified automatically. A claim the matcher confirmed (MATCHED) is judged by the
    Duplicate Bill Check instead."""
    check_type = "Shared Bill Check"
    if (
        bill is None
        or document_required is False
        or bill_match_status == "MATCHED"
        or len(sharing_claims) < 2
        or expense not in sharing_claims
    ):
        return CheckResult(check_type=check_type, result=NOT_APPLICABLE, details="No unresolved shared bill.", remarks="Not applicable")
    others = ", ".join(
        f"S.No {c.s_no} (date {_fmt_date(c.expense_date)})" for c in sharing_claims if c is not expense
    )
    note = (
        f"The bill on page {bill.page_number} (amount {_fmt(bill.amount)}) also fits the claim(s) {others}. "
        "The evidence cannot show whether it is one document reused for several claims (a duplicate) or "
        "separate bills, so it is not verified automatically - confirm which claim it supports."
    )
    return CheckResult(
        check_type=check_type, result=REVIEW_REQUIRED, details=note, remarks=note, source_page=bill.page_number
    )


# ---------- 6. Duplicate bill (report-wide, see pipeline.py for the grouping) ----------


def check_duplicate_bill(
    *,
    bill_match_status: str,
    bill,
    is_duplicate: bool,
    document_required: bool | None = None,
    original_expense=None,
) -> CheckResult:
    check_type = "Duplicate Bill Check"
    if bill is None or bill_match_status != "MATCHED":
        return CheckResult(check_type=check_type, result=NOT_APPLICABLE, details="No confirmed matching bill.", remarks="Not applicable")
    if document_required is False:
        # A category that needs no supporting document at all (e.g. BATTA) can
        # still incidentally score a match against some unrelated bill on the
        # page (bill matching runs uniformly across every expense - see
        # bill_pipeline.py - broad candidate generation is deliberate, see
        # bill_matcher.py's module docstring). Flagging that incidental match
        # as shared with another expense would be a meaningless finding: the
        # document was never required as evidence for this expense in the
        # first place, so it being "reused" proves nothing about a genuine
        # duplicate submission (post-UAT: this was confirmed to fire ten
        # spurious DUPLICATE BILL exceptions on a real document's BATTA rows
        # once a broader amount-extraction fallback let more low-confidence
        # bills enter the match set).
        return CheckResult(check_type=check_type, result=NOT_APPLICABLE, details="No supporting document is required for this category.", remarks="Not applicable")
    if is_duplicate:
        # The auditor must be told WHICH claim is original, not just that a
        # duplicate exists (Part 13-14) - the earlier expense (lowest s_no,
        # see validation/pipeline.py) is deterministically treated as the
        # original; this one is the duplicate.
        if original_expense is not None:
            original_desc = (
                f"S.No {original_expense.s_no} (claimed {_fmt(original_expense.claimed_amount)}, "
                f"date {_fmt_date(original_expense.expense_date)})"
            )
        else:
            original_desc = "an earlier expense on this report"
        reason = (
            f"This bill (page {bill.page_number}) is already matched to {original_desc}, which is treated as "
            f"the original claim; this expense's claim on the same bill is the duplicate."
        )
        return CheckResult(
            check_type=check_type,
            result=EXCEPTION,
            exception_type="DUPLICATE BILL",
            details=reason,
            remarks=reason,
            source_page=bill.page_number,
        )
    return CheckResult(check_type=check_type, result=PASS, details="This bill is matched to only one expense.", remarks="No duplication detected", source_page=bill.page_number)


# ---------- 7. Catering included in Train/Bus Fare ----------


def check_catering(*, category: str | None, bill, claimed_amount=None) -> CheckResult:
    check_type = "Catering Check"
    if normalize_category(category) != "TRAIN / BUS FARE":
        return CheckResult(check_type=check_type, result=NOT_APPLICABLE, remarks="Not applicable")
    if bill is None:
        return CheckResult(check_type=check_type, result=NOT_APPLICABLE, details="No confirmed matching bill.", remarks="Not applicable")

    # The charge as read from the bill's FULL text at extraction time (stored;
    # raw_text is capped at 2,000 characters), else the raw_text detector. For a
    # classifier-derived bill (document_classifier.py fallback, which doesn't run
    # the same label-pattern regex) the classifier's own catering_amount is still
    # only the fallback behind the raw_text detector, itself already re-parsed
    # through parse_amount and never a guess (see document_classifier.py
    # _validate_response). Still just supporting evidence for the same
    # auditor-review message below, never a different check outcome.
    catering_amount, mentioned = _catering_evidence(bill)
    if catering_amount is None and not mentioned:
        return CheckResult(
            check_type=check_type,
            result=PASS,
            details="No catering charges detected in the supporting bill.",
            remarks="No catering charges found",
            source_page=bill.page_number,
        )
    # The short `remarks` text is what surfaces as the primary exception
    # reason (exception_engine.py's _reason_text() prefers remarks over
    # details) - it must name the catering charge directly and stay short,
    # never a paragraph. Never invents a catering amount ("amount could not
    # be determined" when the charge is mentioned but no nearby figure could
    # be parsed - see detect_catering_amount) and never invents the claimed
    # figure if it's genuinely unavailable.
    if catering_amount is not None:
        catering_text = _fmt(catering_amount)
        if claimed_amount is not None:
            remarks = f"Catering included: {catering_text} of {_fmt(claimed_amount)} total. Requires policy review."
        else:
            remarks = f"Catering included: {catering_text}. Requires policy review."
    else:
        remarks = "Catering charge mentioned; amount could not be determined. Requires policy review."
    # `details` keeps the fuller evidence for the UI's separate details
    # column (DocumentViewerPage.jsx) - the short `remarks` above is what's
    # actually displayed as the reason.
    claimed_text = _fmt(claimed_amount) if claimed_amount is not None else "unknown"
    catering_detail_text = _fmt(catering_amount) if catering_amount is not None else "unable to determine"
    details = (
        f"Total claimed amount: {claimed_text}. Catering amount: {catering_detail_text}. "
        "Catering/service component requires separate policy review."
    )
    # A REVIEW_REQUIRED policy exception on an otherwise-successfully-matched
    # bill, never a confirmed EXCEPTION (per spec: catering is an auditor
    # judgment call, not automatically wrong) - the auditor decides whether
    # to accept/reduce the claim, not the system.
    return CheckResult(
        check_type=check_type,
        result=REVIEW_REQUIRED,
        details=details,
        remarks=remarks,
        claimed_amount=claimed_amount,
        bill_amount=catering_amount,
        source_page=bill.page_number,
    )


# ---------- 7b. Room service / food included in Lodging ----------


def check_room_service_food(*, category: str | None, bill, claimed_amount=None) -> CheckResult:
    """The LODGING counterpart of check_catering: a hotel folio that includes
    room service or food is REVIEW_REQUIRED (an auditor judgement call), never
    a confirmed EXCEPTION. Never invents the charge's amount - a mention whose
    amount can't be read is still flagged, with "amount could not be
    determined"."""
    check_type = "Room Service/Food Check"
    if normalize_category(category) != "LODGING":
        return CheckResult(check_type=check_type, result=NOT_APPLICABLE, remarks="Not applicable")
    if bill is None:
        return CheckResult(check_type=check_type, result=NOT_APPLICABLE, details="No confirmed matching bill.", remarks="Not applicable")

    charge, mentioned = _room_service_food_evidence(bill)
    if charge is None and not mentioned:
        return CheckResult(
            check_type=check_type,
            result=PASS,
            details="No room service or food charges detected in the supporting bill.",
            remarks="No room service / food charges found",
            source_page=bill.page_number,
        )
    if charge is not None:
        if claimed_amount is not None:
            remarks = f"Room service / food included: {_fmt(charge)} of {_fmt(claimed_amount)} total. Requires policy review."
        else:
            remarks = f"Room service / food included: {_fmt(charge)}. Requires policy review."
    else:
        remarks = "Room service / food charge mentioned; amount could not be determined. Requires policy review."
    claimed_text = _fmt(claimed_amount) if claimed_amount is not None else "unknown"
    charge_text = _fmt(charge) if charge is not None else "unable to determine"
    details = (
        f"Total claimed amount: {claimed_text}. Room service / food amount: {charge_text}. "
        "Room service / food component requires separate policy review."
    )
    return CheckResult(
        check_type=check_type,
        result=REVIEW_REQUIRED,
        details=details,
        remarks=remarks,
        claimed_amount=claimed_amount,
        bill_amount=charge,
        source_page=bill.page_number,
    )


# ---------- 8. Batta-to-Trip match (Phase 6) ----------


def check_batta_trip_match(
    *,
    category: str | None,
    expense_date: date | None,
    trip_match_status: str,
    matched_trip,
    employee_mismatch: bool,
) -> CheckResult:
    check_type = "Batta Trip Match Check"
    if normalize_category(category) != "BATTA":
        return CheckResult(check_type=check_type, result=NOT_APPLICABLE, remarks="Not applicable")

    source_page = matched_trip.embedded_source_page if matched_trip is not None and matched_trip.is_embedded else None

    if trip_match_status == "MATCHED":
        if matched_trip is None:
            # expense_date like every other review result of this check: a manual verification flips the
            # persisted status to MATCHED (with no loaded trip), so this is the row the next revalidation
            # produces - it must carry the same typed evidence as the review it was approved on.
            return CheckResult(
                check_type=check_type,
                result=REVIEW_REQUIRED,
                details="A trip match was recorded but the matched trip could not be loaded.",
                expense_date=expense_date,
            )
        if expense_date is None:
            return CheckResult(
                check_type=check_type,
                result=REVIEW_REQUIRED,
                details=f"Matched trip {matched_trip.trip_id or '(unknown trip number)'}, but the expense's own date could not be reliably determined.",
                source_page=source_page,
            )
        if matched_trip.start_date is None or matched_trip.end_date is None:
            return CheckResult(
                check_type=check_type,
                result=REVIEW_REQUIRED,
                details=f"Matched trip {matched_trip.trip_id or '(unknown trip number)'}, but its travel period could not be reliably determined.",
                expense_date=expense_date,
                source_page=source_page,
            )
        if matched_trip.start_date <= expense_date <= matched_trip.end_date:
            return CheckResult(
                check_type=check_type,
                result=PASS,
                details=f"Expense {_fmt_date(expense_date)} falls within trip {matched_trip.trip_id or '(unknown)'} ({_fmt_date(matched_trip.start_date)} - {_fmt_date(matched_trip.end_date)})",
                remarks="Batta expense date is within the matched trip's travel period",
                expense_date=expense_date,
                source_page=source_page,
            )
        return CheckResult(
            check_type=check_type,
            result=EXCEPTION,
            exception_type="TRIP DATE MISMATCH",
            details=f"Expense {_fmt_date(expense_date)} vs trip {matched_trip.trip_id or '(unknown)'} period {_fmt_date(matched_trip.start_date)} - {_fmt_date(matched_trip.end_date)}",
            remarks="Batta expense date falls outside the matched trip's travel period.",
            expense_date=expense_date,
            source_page=source_page,
        )

    if trip_match_status == "NO_MATCH":
        if employee_mismatch:
            return CheckResult(
                check_type=check_type,
                result=EXCEPTION,
                exception_type="TRIP EMPLOYEE MISMATCH",
                details="The only candidate trip(s) found belong to a different employee.",
                remarks="This Batta expense's trip evidence belongs to another employee.",
                expense_date=expense_date,
            )
        return CheckResult(
            check_type=check_type,
            result=EXCEPTION,
            exception_type="TRIP NOT FOUND",
            details="No embedded trip summary or standalone Trip Report could be matched to this Batta expense.",
            remarks="No corresponding Trip Report has been uploaded yet for this Batta claim.",
            expense_date=expense_date,
        )

    if trip_match_status == "MULTIPLE_CANDIDATES":
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details="Multiple candidate trips are plausible matches for this Batta expense.",
            expense_date=expense_date,
        )

    # REVIEW_REQUIRED or NOT_PROCESSED (matching hasn't confidently resolved yet)
    return CheckResult(
        check_type=check_type,
        result=REVIEW_REQUIRED,
        details="Trip match evidence for this Batta expense was too weak to auto-confirm.",
        expense_date=expense_date,
    )


# ---------- 9. Report total ----------


def check_total(*, extracted_sum, reported_total) -> CheckResult:
    check_type = "Total Validation Check"
    if reported_total is None:
        return CheckResult(
            check_type=check_type,
            result=REVIEW_REQUIRED,
            details="The report's own stated total could not be reliably extracted.",
            claimed_amount=extracted_sum,
        )

    total_extracted = _to_decimal(extracted_sum) or Decimal("0.00")
    total_reported = _to_decimal(reported_total)

    if total_extracted == total_reported:
        return CheckResult(
            check_type=check_type,
            result=PASS,
            details=f"Sum of extracted expenses {_fmt(total_extracted)} vs report total {_fmt(total_reported)}",
            remarks="Totals match",
            claimed_amount=float(total_extracted),
        )
    return CheckResult(
        check_type=check_type,
        result=EXCEPTION,
        exception_type="TOTAL MISMATCH",
        details=f"Sum of extracted expenses {_fmt(total_extracted)} vs report total {_fmt(total_reported)}",
        remarks=f"Sum of extracted expenses {_fmt(total_extracted)} does not match the report total {_fmt(total_reported)}.",
        claimed_amount=float(total_extracted),
    )
