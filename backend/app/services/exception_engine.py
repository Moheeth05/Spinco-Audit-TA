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

from collections import defaultdict

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
    "Category Consistency Check": "BILL MATCH REVIEW REQUIRED",
    "Duplicate Bill Check": "BILL MATCH REVIEW REQUIRED",
    "Required Document Check": "BILL MATCH REVIEW REQUIRED",
    "Batta Trip Match Check": "TRIP MATCH REVIEW REQUIRED",
    "Total Validation Check": "REPORT TOTAL REVIEW REQUIRED",
    # A policy exception on an otherwise-successfully-matched bill (Part
    # 15-17 of the post-UAT correction) - kept in its own family, never
    # folded into "BILL MATCH REVIEW REQUIRED", since it is not a matching
    # problem at all: the bill matched fine, the auditor just needs to see
    # that catering was bundled into the fare.
    "Catering Check": "CATERING INCLUDED",
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
    "TRIP MATCH REVIEW REQUIRED": "TRIP",
    "REPORT TOTAL REVIEW REQUIRED": "TOTAL",
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
                records.append(
                    _build_record(
                        report=report,
                        expense=expense,
                        primary=vr,
                        exception_type=exception_type,
                        severity=EXCEPTION,
                        all_rows=[vr],
                    )
                )

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
            records.append(
                _build_record(
                    report=report,
                    expense=expense,
                    primary=group_rows[0],
                    exception_type=family,
                    severity=REVIEW_REQUIRED,
                    all_rows=group_rows,
                )
            )

    # Re-apply any standing manual verifications (see models/validation.py's
    # ExceptionVerification) onto the freshly-rebuilt records - this table
    # was just wiped and recreated above, so without this step a prior
    # "Verify" action would silently vanish on the next (re)evaluation
    # (see the module-level docstring's dedup rationale, and the post-UAT
    # correction spec's explicit "do not delete audit history" requirement).
    # Never applied to severity=EXCEPTION - only a REVIEW REQUIRED condition
    # can ever be verified away.
    verifications = {
        (v.expense_id, v.exception_type): v
        for v in db.query(ExceptionVerification).filter(ExceptionVerification.expense_report_id == report.id).all()
    }
    for rec in records:
        if rec.severity != REVIEW_REQUIRED:
            continue
        v = verifications.get((rec.expense_id, rec.exception_type))
        if v is not None:
            rec.status = "RESOLVED"
            rec.verified_by_id = v.verified_by_id
            rec.verified_at = v.verified_at
            rec.verification_note = v.verification_note

    for rec in records:
        db.add(rec)
    db.commit()

    # An expense's validation_status was already set by validate_expense_report()
    # (see validation/pipeline.py::_aggregate_status) before this function
    # runs - override it here only when every one of that expense's
    # REVIEW_REQUIRED conditions has since been manually verified and it has
    # no unverified EXCEPTION, so "VERIFIED" is visible everywhere the status
    # is read (Expense Summary, dashboard, analytics, Excel) rather than the
    # UI continuing to show REVIEW REQUIRED for something a human already
    # resolved (Part 22). Kept distinct from "PASS" (which means every check
    # cleanly passed on its own) so the fact that this expense DID require
    # human review remains visible, per the "do not delete audit history"
    # requirement - it is a resolved state, not an erased one.
    records_by_expense: dict[int | None, list[ExceptionRecord]] = defaultdict(list)
    for rec in records:
        records_by_expense[rec.expense_id].append(rec)
    for expense_id, expense in expenses_by_id.items():
        if expense.validation_status != "REVIEW_REQUIRED":
            continue
        expense_records = records_by_expense.get(expense_id, [])
        has_unverified_review = any(r.severity == REVIEW_REQUIRED and r.status == "OPEN" for r in expense_records)
        has_exception = any(r.severity == EXCEPTION for r in expense_records)
        if not has_unverified_review and not has_exception:
            expense.validation_status = "VERIFIED"

    expenses = list(expenses_by_id.values())
    verified = sum(1 for e in expenses if e.validation_status in ("PASS", "VERIFIED"))
    exception_count = sum(1 for e in expenses if e.validation_status == "EXCEPTION")
    review_count = sum(1 for e in expenses if e.validation_status == "REVIEW_REQUIRED")

    report_level_rows = by_expense.get(None, [])
    report_level_exception = any(r.result == "EXCEPTION" for r in report_level_rows)
    # A report-level REVIEW REQUIRED (e.g. Total Validation Check, which has
    # no expense_id) only still counts if its own ExceptionRecord wasn't
    # itself verified - same rule as a per-expense one, just checked
    # against the expense_id=None bucket of `records`.
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
