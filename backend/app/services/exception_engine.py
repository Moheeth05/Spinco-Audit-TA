"""Phase 7 - deterministic Exception Engine and report-level status.

Consumes the ValidationResult rows already produced by
services/validation/pipeline.py (which itself already folds in Phase 4 bill
matching and Phase 6 Batta/trip matching outcomes via check_bill_amount,
check_bill_date, check_required_document, check_category_consistency,
check_duplicate_bill, check_batta_trip_match, etc.) - this module never
re-runs or re-implements any of that detection logic. It only aggregates,
classifies, deduplicates and persists:

  1. ExceptionRecord rows (one per confirmed EXCEPTION ValidationResult, plus
     one per REVIEW_REQUIRED "family" per expense - see _review_family - so
     the same underlying uncertainty never appears as several near-identical
     rows).
  2. The report-level verdict on ExpenseReport: overall_status,
     verified_expense_count, exception_count, review_required_count.

Report-level status values - "ACCOUNT VERIFIED" | "EXCEPTION" |
"REVIEW REQUIRED" - deliberately match the exact strings already used by
Phase 1's dashboard/analytics KPI queries and the Expense Reports month
page's status filter (ExpenseReport.overall_status == "ACCOUNT VERIFIED" /
"EXCEPTION"), even though this phase's own spec text sometimes writes the
plural "EXCEPTIONS" loosely - using the already-committed singular avoids
silently breaking that already-wired filter/KPI infrastructure.

A single confirmed EXCEPTION anywhere in the report (on any expense, or the
report-level Total Validation Check) means the whole report is EXCEPTION -
never ACCOUNT VERIFIED, no matter how many other expenses passed cleanly.
Absent any confirmed exception, any REVIEW_REQUIRED anywhere means REVIEW
REQUIRED. Only when every applicable check is PASS or NOT_APPLICABLE does
the report become ACCOUNT VERIFIED.
"""

import hashlib
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models.expense_report import Expense, ExpenseReport
from app.models.validation import ExceptionRecord, ExceptionVerification, ValidationResult

ACCOUNT_VERIFIED = "ACCOUNT VERIFIED"
EXCEPTION = "EXCEPTION"
REVIEW_REQUIRED = "REVIEW REQUIRED"

# check_type -> the review-required "family" label used when that check
# comes back REVIEW_REQUIRED. Checks sharing a family collapse into one
# ExceptionRecord per expense (e.g. Bill Amount Check + Bill Date Check both
# being REVIEW_REQUIRED because there's no confirmed matching bill is one
# underlying problem, not two).
#
# Required Document Check only ever returns REVIEW_REQUIRED for genuine
# uncertainty (ambiguous/multiple-candidate matching, or an unresolved
# policy/document-requirement lookup) - it returns a confirmed EXCEPTION
# ("MISSING BILL") separately, on its own, when a document is actually
# absent (bill_match_status == NO_MATCH). So its REVIEW_REQUIRED case must
# never be labelled "REQUIRED DOCUMENT MISSING" (that name implies physical
# absence, which post-UAT review found this exact family wrongly implying -
# see validation/checks.py::check_required_document's REVIEW_REQUIRED
# branches, all of which are about uncertainty, none about absence). It
# shares "BILL MATCH REVIEW REQUIRED" with the other ambiguous-match checks
# instead, so the two collapse into one ExceptionRecord per expense rather
# than reporting the same ambiguous-match uncertainty twice under two
# different-sounding names.
_REVIEW_FAMILY_BY_CHECK_TYPE = {
    "Bill Amount Check": "BILL MATCH REVIEW REQUIRED",
    "Bill Date Check": "BILL MATCH REVIEW REQUIRED",
    "Payment Status Check": "BILL MATCH REVIEW REQUIRED",
    "Category Consistency Check": "BILL MATCH REVIEW REQUIRED",
    "Duplicate Bill Check": "BILL MATCH REVIEW REQUIRED",
    "Shared Bill Check": "DUPLICATE BILL REVIEW REQUIRED",
    "Required Document Check": "BILL MATCH REVIEW REQUIRED",
    "Batta Trip Match Check": "TRIP MATCH REVIEW REQUIRED",
    "Total Validation Check": "REPORT TOTAL REVIEW REQUIRED",
    # A policy exception on an otherwise-successfully-matched bill (Part
    # 15-17 of the post-UAT correction) - kept in its own family, never
    # folded into "BILL MATCH REVIEW REQUIRED", since it is not a matching
    # problem at all: the bill matched fine, the auditor just needs to see
    # that catering was bundled into the fare.
    "Catering Check": "CATERING INCLUDED",
    # Lodging's counterpart of the catering family above, same reasoning.
    "Room Service/Food Check": "ROOM SERVICE / FOOD INCLUDED",
    # Its own family, not folded into "BILL MATCH REVIEW REQUIRED" - a
    # grouped bill can be a confident, uniquely-matched MATCH (no matching
    # ambiguity at all) while still needing a human to confirm the page-
    # grouping itself was correct; that's a different kind of uncertainty
    # from "which bill, if any, actually matches this expense".
    "Bill Grouping Review Check": "BILL GROUPING REVIEW REQUIRED",
}

# Coarser "root cause" grouping used only to suppress a review-required
# family when a confirmed exception in that same family already exists for
# the same expense - e.g. MISSING BILL (from check_required_document, when
# bill_match_status == NO_MATCH) and BILL MATCH REVIEW REQUIRED (from
# check_bill_amount/check_bill_date, which fire REVIEW_REQUIRED for that
# exact same NO_MATCH condition) are two checks independently detecting the
# *same* missing-bill problem - the spec's mandatory dedup rule (one logical
# MISSING BILL exception, not several near-duplicate rows) applies here.
_EXCEPTION_TYPE_ROOT = {
    "AMOUNT MISMATCH": "BILL",
    "DATE MISMATCH": "BILL",
    "MISSING BILL": "BILL",
    "CATEGORY MISMATCH": "BILL",
    "DUPLICATE BILL": "BILL",
    "TOTAL MISMATCH": "TOTAL",
    "TRIP NOT FOUND": "TRIP",
    "TRIP DATE MISMATCH": "TRIP",
    "TRIP EMPLOYEE MISMATCH": "TRIP",
}
_REVIEW_FAMILY_ROOT = {
    "BILL MATCH REVIEW REQUIRED": "BILL",
    "DUPLICATE BILL REVIEW REQUIRED": "BILL",
    "TRIP MATCH REVIEW REQUIRED": "TRIP",
    "REPORT TOTAL REVIEW REQUIRED": "TOTAL",
    # Same root as the other bill-evidence families - if this expense
    # already has a confirmed BILL-rooted EXCEPTION (AMOUNT MISMATCH,
    # MISSING BILL, etc.), a separate "also verify the page grouping" review
    # row would just be redundant noise on top of the already-surfaced
    # problem; the auditor is already looking at this bill.
    "BILL GROUPING REVIEW REQUIRED": "BILL",
}


def _review_family(vr: ValidationResult) -> str:
    if vr.check_type == "Policy Limit Check":
        # policy_category is set from the looked-up PolicyMaster row (see
        # validation/pipeline.py::_persist_check) - None means no policy was
        # found at all, a materially different, more specific condition than
        # "found the policy but couldn't verify the limit against it".
        return "POLICY NOT FOUND" if vr.policy_category is None else "POLICY LIMIT REVIEW REQUIRED"
    return _REVIEW_FAMILY_BY_CHECK_TYPE.get(vr.check_type, "REVIEW REQUIRED")


def _reason_text(rows: list[ValidationResult]) -> str:
    texts = [r.remarks or r.details for r in rows if (r.remarks or r.details)]
    # dict.fromkeys dedupes while preserving order - several rows in the same
    # family often carry the identical "no confirmed matching bill" text.
    unique = list(dict.fromkeys(texts))
    return "; ".join(unique) if unique else "Review required - insufficient evidence to confirm this automatically."


# ---------------------------------------------------------------------------
# Evidence signature of an approval.
#
# A manual verification is applied to a regenerated exception only while the
# exception is still built on the SAME material evidence it was approved on.
# The signature is a SHA-256 over the typed fields of the ValidationResult rows
# behind the exception - never their wording (a reworded reason must not void an
# approval) and never a row id (ids change on every reprocess):
#
#     check_type, result, claimed_amount, bill_amount, policy_limit,
#     expense_date, bill_date, source_page
#
# Amounts are normalised to two places, dates to ISO, and the rows are sorted, so
# the value is deterministic. What it CANNOT capture: anything that is not one of
# those typed fields - e.g. which OTHER claim shares a bill (Shared / Duplicate
# Bill reviews), the report's own stated total (only the extracted sum is typed),
# or vendor/payment details that appear only in the remarks text.
# ---------------------------------------------------------------------------

STALE_APPROVAL_NOTE = "A previous manual verification no longer applies because the evidence changed; re-verify."
_STALE_SUFFIX = f" [{STALE_APPROVAL_NOTE}]"
_REASON_MAX = 1000  # ExceptionRecord.reason is String(1000)


def _norm_amount(value) -> str:
    if value is None:
        return ""
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return str(value)


def evidence_signature(rows: list[ValidationResult]) -> str:
    material = sorted(
        [
            r.check_type or "",
            r.result or "",
            _norm_amount(r.claimed_amount),
            _norm_amount(r.bill_amount),
            _norm_amount(r.policy_limit),
            r.expense_date.isoformat() if r.expense_date else "",
            r.bill_date.isoformat() if r.bill_date else "",
            "" if r.source_page is None else str(r.source_page),
        ]
        for r in rows
    )
    return hashlib.sha256(json.dumps(material, separators=(",", ":")).encode("utf-8")).hexdigest()


def rows_for_record(db: Session, record: ExceptionRecord) -> list[ValidationResult]:
    """The ValidationResult rows an existing ExceptionRecord was built from - the same rows
    evaluate_report() groups (a single row for a confirmed EXCEPTION, the whole review family
    of that expense - or of the report, when expense_id is NULL - for a review)."""
    if record.severity == EXCEPTION:
        row = db.get(ValidationResult, record.validation_result_id) if record.validation_result_id else None
        return [row] if row is not None else []
    query = db.query(ValidationResult).filter(
        ValidationResult.expense_report_id == record.expense_report_id, ValidationResult.result == "REVIEW_REQUIRED"
    )
    query = (
        query.filter(ValidationResult.expense_id.is_(None))
        if record.expense_id is None
        else query.filter(ValidationResult.expense_id == record.expense_id)
    )
    return [r for r in query.all() if _review_family(r) == record.exception_type]


def signature_for_record(db: Session, record: ExceptionRecord) -> str:
    return evidence_signature(rows_for_record(db, record))


def strip_stale_note(reason: str | None) -> str | None:
    """The reason as the checks produced it, without the 'approval no longer applies' note."""
    if reason and reason.endswith(_STALE_SUFFIX):
        return reason[: -len(_STALE_SUFFIX)]
    return reason


_MANUAL_MATCH_NOTE = "Manually verified by auditor"  # prefix evaluate_report() puts on the reasons of a manually confirmed match
# review family -> the (status, reasons) columns of Expense that a manual verification of that family flips
_MATCH_STATUS_COLUMNS = {
    "BILL MATCH REVIEW REQUIRED": ("bill_match_status", "bill_match_reasons"),
    "TRIP MATCH REVIEW REQUIRED": ("trip_match_status", "trip_match_reasons"),
}


def _with_stale_note(reason: str) -> str:
    base = strip_stale_note(reason) or ""
    return base[: _REASON_MAX - len(_STALE_SUFFIX)] + _STALE_SUFFIX  # rebuilt from the base every time: never duplicated


def _approval_applies(verification: ExceptionVerification, reason: str, current_signature: str) -> bool:
    if verification.evidence_signature is not None:
        return verification.evidence_signature == current_signature
    # Unsigned (verified before signatures existed): the only evidence of what was approved is the reason text.
    return verification.original_reason == reason


def _build_record(
    *,
    report: ExpenseReport,
    expense: Expense | None,
    primary: ValidationResult,
    exception_type: str,
    severity: str,
    all_rows: list[ValidationResult],
) -> ExceptionRecord:
    return ExceptionRecord(
        expense_report_id=report.id,
        expense_id=expense.id if expense else None,
        exception_type=exception_type,
        severity=severity,
        reason=_reason_text(all_rows),
        source_pdf=report.original_filename,
        source_page=primary.source_page,
        validation_result_id=primary.id,
        bill_id=expense.bill_id if expense else None,
        trip_report_id=expense.trip_report_id if expense else None,
        source_document_id=primary.source_document_id,
        status="OPEN",
    )


def evaluate_report(db: Session, expense_report_id: int) -> None:
    """Recomputes this report's ExceptionRecord rows and overall_status from
    its current ValidationResult rows. Safe to call repeatedly (e.g. after a
    revalidation, a bill/trip rematch, or newly-arrived trip evidence
    resolves a prior TRIP NOT FOUND) - always replaces wholesale, never
    accumulates, and is driven entirely by the CURRENT state of
    ValidationResult, so a resolved condition's exception simply stops being
    regenerated rather than needing to be explicitly cleared."""
    report = db.get(ExpenseReport, expense_report_id)
    if report is None:
        raise ValueError(f"No expense report with id {expense_report_id}")

    db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report.id).delete()
    db.commit()

    all_results = (
        db.query(ValidationResult).filter(ValidationResult.expense_report_id == report.id).all()
    )
    by_expense: dict[int | None, list[ValidationResult]] = defaultdict(list)
    for vr in all_results:
        by_expense[vr.expense_id].append(vr)

    expenses_by_id = {
        e.id: e for e in db.query(Expense).filter(Expense.expense_report_id == report.id).all()
    }

    records: list[ExceptionRecord] = []
    signatures: dict[int, str] = {}  # id(record) -> the evidence signature of the rows it was built from
    for expense_id, rows in by_expense.items():
        expense = expenses_by_id.get(expense_id) if expense_id is not None else None

        exception_type_roots: set[str] = set()
        for vr in rows:
            if vr.result == "EXCEPTION":
                exception_type = vr.exception_type or vr.check_type
                root = _EXCEPTION_TYPE_ROOT.get(exception_type)
                if root:
                    exception_type_roots.add(root)
                # Never rename an existing exception_type - it's already the
                # precise label the originating check assigned.
                record = _build_record(
                    report=report,
                    expense=expense,
                    primary=vr,
                    exception_type=exception_type,
                    severity=EXCEPTION,
                    all_rows=[vr],
                )
                signatures[id(record)] = evidence_signature([vr])
                records.append(record)

        review_rows = [r for r in rows if r.result == "REVIEW_REQUIRED"]
        groups: dict[str, list[ValidationResult]] = defaultdict(list)
        for vr in review_rows:
            groups[_review_family(vr)].append(vr)
        for family, group_rows in groups.items():
            # Suppressed: a confirmed exception in the same root (e.g.
            # MISSING BILL) already explains this expense's bill situation -
            # a "BILL MATCH REVIEW REQUIRED" alongside it would just be the
            # same underlying problem restated by a second check.
            if _REVIEW_FAMILY_ROOT.get(family) in exception_type_roots:
                continue
            record = _build_record(
                report=report,
                expense=expense,
                primary=group_rows[0],
                exception_type=family,
                severity=REVIEW_REQUIRED,
                all_rows=group_rows,
            )
            signatures[id(record)] = evidence_signature(group_rows)
            records.append(record)

    # Re-apply any standing manual verifications (see models/validation.py's
    # ExceptionVerification) onto the freshly-rebuilt records - this table
    # was just wiped and recreated above, so without this step a prior
    # "Verify" action would silently vanish on the next (re)evaluation
    # (see the module-level docstring's dedup rationale, and the post-UAT
    # correction spec's explicit "do not delete audit history" requirement).
    # Applies to both REVIEW_REQUIRED and EXCEPTION severity - an auditor can
    # now explicitly accept a confirmed EXCEPTION too (e.g. a legitimate
    # rounding/GST difference), not just resolve an ambiguous REVIEW
    # REQUIRED condition; api/exceptions.py's verify_exception() is the only
    # writer of ExceptionVerification and requires a note specifically for
    # EXCEPTION severity, since accepting one overrides a real policy
    # violation finding rather than just confirming an uncertain match.
    verifications = {
        (v.expense_id, v.exception_type): v
        for v in db.query(ExceptionVerification).filter(ExceptionVerification.expense_report_id == report.id).all()
    }
    #
    # An approval is re-applied only while the regenerated exception rests on the SAME
    # evidence it was approved on (see evidence_signature above). Otherwise the verification
    # row is kept untouched as history, the exception stays OPEN - so the expense is not
    # VERIFIED and the report cannot become ACCOUNT VERIFIED on its account - and a stable
    # note is appended to the reason (rebuilt from the base reason every evaluation, so it
    # can never accumulate). No audit event is written here: this runs on every evaluation.
    for rec in records:
        if rec.severity not in (REVIEW_REQUIRED, EXCEPTION):
            continue
        v = verifications.get((rec.expense_id, rec.exception_type))
        if v is None:
            continue
        current_signature = signatures[id(rec)]
        if _approval_applies(v, rec.reason, current_signature):
            if v.evidence_signature is None:
                v.evidence_signature = current_signature  # an unsigned row whose reason is unchanged: stamp it
            rec.status = "RESOLVED"
            rec.verified_by_id = v.verified_by_id
            rec.verified_at = v.verified_at
            rec.verification_note = v.verification_note
        else:
            rec.reason = _with_stale_note(rec.reason)

    for rec in records:
        db.add(rec)
    db.commit()

    # An expense's validation_status was already set by validate_expense_report()
    # (see validation/pipeline.py::_aggregate_status) before this function
    # runs - override it here only when every one of that expense's
    # REVIEW_REQUIRED and EXCEPTION conditions has since been manually
    # verified/accepted, so "VERIFIED" is visible everywhere the status is
    # read (Expense Summary, dashboard, analytics, Excel) rather than the
    # UI continuing to show REVIEW REQUIRED/EXCEPTION for something a human
    # already resolved (Part 22, later extended to cover accepted
    # EXCEPTIONs too). Kept distinct from "PASS" (which means every check
    # cleanly passed on its own) so the fact that this expense DID require
    # human review remains visible, per the "do not delete audit history"
    # requirement - it is a resolved state, not an erased one.
    records_by_expense: dict[int | None, list[ExceptionRecord]] = defaultdict(list)
    for rec in records:
        records_by_expense[rec.expense_id].append(rec)
    for expense_id, expense in expenses_by_id.items():
        if expense.validation_status not in ("REVIEW_REQUIRED", "EXCEPTION"):
            continue
        expense_records = records_by_expense.get(expense_id, [])
        has_unverified_review = any(r.severity == REVIEW_REQUIRED and r.status == "OPEN" for r in expense_records)
        has_unverified_exception = any(r.severity == EXCEPTION and r.status == "OPEN" for r in expense_records)
        if not has_unverified_review and not has_unverified_exception:
            expense.validation_status = "VERIFIED"

    # Part 3 of the post-UAT correction: a manually-accepted ambiguous-match
    # review must actually flip the underlying match status the rest of the
    # app reads (Expense Summary's Match Status column, Excel's bill/trip
    # match columns, analytics' match-summary counts) - not just show a
    # "Verified" tag in the Exceptions tab while bill_match_status/
    # trip_match_status keep reporting REVIEW_REQUIRED/MULTIPLE_CANDIDATES
    # forever. Deliberately narrow: only the ambiguous-match family
    # ("...MATCH REVIEW REQUIRED", ie there WAS a real candidate, the
    # matcher just wasn't confident enough) is eligible - never MISSING
    # BILL/TRIP NOT FOUND, where there was no candidate at all and "MATCHED"
    # would be a false claim; those stay whatever they are, verification
    # there only changes validation_status (handled above), never invents a
    # match that doesn't exist.
    for expense_id, expense in expenses_by_id.items():
        for rec in records_by_expense.get(expense_id, []):
            # The reverse of the flip below: a BILL / TRIP MATCH approval that no longer applies (its
            # evidence changed) must not leave the match status MATCHED. Only a status that the
            # manual-verification flip itself set is reverted - recognised by its reasons prefix, and
            # only where a verification row exists - so a match the matcher confirmed is untouched.
            # The pre-approval status (REVIEW_REQUIRED vs MULTIPLE_CANDIDATES) is not stored; it
            # is restored as REVIEW_REQUIRED, which is still an unconfirmed match.
            match_columns = _MATCH_STATUS_COLUMNS.get(rec.exception_type)
            if match_columns and rec.status == "OPEN" and (expense_id, rec.exception_type) in verifications:
                status_attr, reasons_attr = match_columns
                reasons = getattr(expense, reasons_attr) or ""
                if getattr(expense, status_attr) == "MATCHED" and reasons.startswith(_MANUAL_MATCH_NOTE):
                    rest = reasons[len(_MANUAL_MATCH_NOTE):]
                    setattr(expense, status_attr, "REVIEW_REQUIRED")
                    setattr(expense, reasons_attr, rest[3:] if rest.startswith(" - ") else None)
                continue
            if rec.status != "RESOLVED":
                continue
            if rec.exception_type == "BILL MATCH REVIEW REQUIRED" and expense.bill_match_status in (
                "REVIEW_REQUIRED",
                "MULTIPLE_CANDIDATES",
            ):
                expense.bill_match_status = "MATCHED"
                expense.bill_match_reasons = (
                    f"Manually verified by auditor - {expense.bill_match_reasons}"
                    if expense.bill_match_reasons
                    else "Manually verified by auditor."
                )
            elif rec.exception_type == "TRIP MATCH REVIEW REQUIRED" and expense.trip_match_status in (
                "REVIEW_REQUIRED",
                "MULTIPLE_CANDIDATES",
            ):
                expense.trip_match_status = "MATCHED"
                expense.trip_match_reasons = (
                    f"Manually verified by auditor - {expense.trip_match_reasons}"
                    if expense.trip_match_reasons
                    else "Manually verified by auditor."
                )

    expenses = list(expenses_by_id.values())
    verified = sum(1 for e in expenses if e.validation_status in ("PASS", "VERIFIED"))
    exception_count = sum(1 for e in expenses if e.validation_status == "EXCEPTION")
    review_count = sum(1 for e in expenses if e.validation_status == "REVIEW_REQUIRED")

    # A report-level EXCEPTION/REVIEW REQUIRED (e.g. Total Validation Check,
    # which has no expense_id) only still counts if its own ExceptionRecord
    # wasn't itself verified/accepted - checked against the expense_id=None
    # bucket of `records`, not the raw ValidationResult.result, so an
    # accepted report-level exception actually clears here too.
    report_level_exception = any(
        r.severity == EXCEPTION and r.status == "OPEN" for r in records_by_expense.get(None, [])
    )
    report_level_review = any(
        r.severity == REVIEW_REQUIRED and r.status == "OPEN" for r in records_by_expense.get(None, [])
    )

    if exception_count > 0 or report_level_exception:
        overall = EXCEPTION
    elif review_count > 0 or report_level_review:
        overall = REVIEW_REQUIRED
    else:
        overall = ACCOUNT_VERIFIED

    report.verified_expense_count = verified
    report.exception_count = exception_count
    report.review_required_count = review_count
    report.overall_status = overall
    db.commit()
