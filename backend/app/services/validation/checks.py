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

from app.services.category_map import detect_catering_amount, expected_bill_label, mentions_catering, normalize_category
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
    *, claimed_amount, bill, bill_match_reasons: str | None = None, document_required: bool | None = None
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
    catering_amount = detect_catering_amount(bill.raw_text)
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
    *, category: str | None, bill, document_required: bool | None = None
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
        remarks="Supporting bill does not appear consistent with the claimed expense category.",
        source_page=bill.page_number,
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

    catering_amount = detect_catering_amount(bill.raw_text)
    if catering_amount is None and getattr(bill, "catering_amount", None) is not None:
        # No regex-detectable catering line on the bill's own extracted text
        # (e.g. text recovered via the document_classifier.py fallback, which
        # doesn't run the same label-pattern regex) - fall back to the
        # classifier's own catering_amount field, itself already re-parsed
        # through parse_amount and never a guess (see document_classifier.py
        # _validate_response). Still just supporting evidence for the same
        # auditor-review message below, never a different check outcome.
        catering_amount = float(bill.catering_amount)
    if catering_amount is None and not mentions_catering(bill.raw_text):
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
            return CheckResult(check_type=check_type, result=REVIEW_REQUIRED, details="A trip match was recorded but the matched trip could not be loaded.")
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
