"""Orchestrates policy validation for one Expense Report: loads the report's
expenses/bills, looks up each expense's policy, runs every deterministic
check, and persists the results. Replaces all validation_results/exceptions
rows for the report wholesale on every (re)validation run - the same
"replace, never accumulate duplicates" pattern used by extraction and bill
matching in earlier phases.

This module never computes a report-level ACCOUNT VERIFIED/EXCEPTION
verdict - that is Phase 7's job. It only produces the per-check evidence
Phase 7 will later consume (spec section 26).
"""

from collections import Counter

from sqlalchemy.orm import Session

from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.logs import ProcessingLog
from app.models.trip import TripReport
from app.models.validation import ValidationResult
from app.services.category_map import normalize_category
from app.services.validation import checks
from app.services.validation.policy_lookup import find_policy, is_document_required

STAGE = "VALIDATION"


def _persist_check(db: Session, *, report: ExpenseReport, expense: Expense | None, policy, result: checks.CheckResult) -> None:
    db.add(
        ValidationResult(
            expense_report_id=report.id,
            expense_id=expense.id if expense else None,
            check_type=result.check_type,
            details=result.details,
            result=result.result,
            remarks=result.remarks,
            exception_type=result.exception_type,
            claimed_amount=result.claimed_amount,
            policy_limit=result.policy_limit,
            bill_amount=result.bill_amount,
            expense_date=result.expense_date,
            bill_date=result.bill_date,
            policy_category=policy.category if policy else None,
            policy_version=policy.policy_version if policy else None,
            source_document_id=f"expense:{report.id}",
            source_page=result.source_page,
        )
    )
    # ExceptionRecord rows are derived from these ValidationResult rows by
    # services/exception_engine.py (Phase 7), not written here - see that
    # module's docstring for why it's the sole writer.


def _aggregate_status(results: list[checks.CheckResult]) -> str:
    statuses = {r.result for r in results}
    if checks.EXCEPTION in statuses:
        return "EXCEPTION"
    if checks.REVIEW_REQUIRED in statuses:
        return "REVIEW_REQUIRED"
    return "PASS"


def validate_expense_report(db: Session, expense_report_id: int) -> None:
    report = db.get(ExpenseReport, expense_report_id)
    if report is None:
        raise ValueError(f"No expense report with id {expense_report_id}")

    db.add(
        ProcessingLog(
            expense_report_id=report.id,
            filename=report.original_filename or report.filename,
            stage=STAGE,
            status="PROCESSING",
            message="Policy validation started.",
        )
    )
    db.commit()

    # Replace wholesale - revalidation must never accumulate duplicate rows.
    # (ExceptionRecord rows are owned and replaced by exception_engine.py, not here.)
    db.query(ValidationResult).filter(ValidationResult.expense_report_id == report.id).delete()
    db.commit()

    expenses = db.query(Expense).filter(Expense.expense_report_id == report.id).order_by(Expense.s_no).all()
    bills_by_id = {b.id: b for b in db.query(Bill).filter(Bill.expense_report_id == report.id).all()}
    trip_ids_needed = {e.trip_report_id for e in expenses if e.trip_report_id is not None}
    trips_by_id = (
        {t.id: t for t in db.query(TripReport).filter(TripReport.id.in_(trip_ids_needed)).all()}
        if trip_ids_needed
        else {}
    )

    # A bill referenced by more than one expense's bill_id is a duplicate match,
    # regardless of what Bill.matched_expense_id happens to record (that field
    # only remembers the most recent match, not every expense that claimed it).
    bill_usage = Counter(e.bill_id for e in expenses if e.bill_id is not None)
    # Deterministic "original" claim per re-used bill (lowest s_no wins) - an
    # auditor reading a DUPLICATE BILL exception needs to know which claim is
    # considered original, not just that a duplicate exists (Part 13-14).
    # Only the later claim(s) are flagged; the original itself passes this
    # check like any other uniquely-matched bill.
    bill_original_expense: dict[int, Expense] = {}
    for expense in sorted(expenses, key=lambda e: (e.s_no if e.s_no is not None else 0, e.id)):
        if expense.bill_id is not None and expense.bill_id not in bill_original_expense:
            bill_original_expense[expense.bill_id] = expense

    try:
        for expense in expenses:
            policy = find_policy(db, expense.category)
            # `bill` (confirmed) still gates duplicate-bill detection exactly
            # as before - that check is specifically about two CONFIRMED
            # claims pointing at the same bill, and must not fire on two
            # expenses that merely share the same low-confidence candidate.
            bill = bills_by_id.get(expense.bill_id) if expense.bill_id else None
            # `candidate_bill` (best plausible candidate, confirmed or not)
            # is what amount/date/category evaluation now runs against - see
            # bill_matcher.py's module docstring and checks.py's
            # check_bill_amount docstring for why this must not wait for
            # bill_match_status to reach MATCHED.
            candidate_bill = bills_by_id.get(expense.candidate_bill_id) if expense.candidate_bill_id else None
            document_required = is_document_required(policy) if policy else None

            expense.policy_basis = policy.reimbursement_basis if policy else None
            expense.policy_limit = float(policy.limit_amount) if policy and policy.limit_amount is not None else None

            results = [
                checks.check_policy_limit(
                    claimed_amount=expense.claimed_amount,
                    category_label=expense.category or "(unknown)",
                    policy=policy,
                ),
                checks.check_bill_amount(
                    claimed_amount=expense.claimed_amount,
                    bill=candidate_bill,
                    bill_match_reasons=expense.bill_match_reasons,
                    document_required=document_required,
                ),
                checks.check_bill_date(
                    expense_date=expense.expense_date,
                    bill=candidate_bill,
                    bill_match_reasons=expense.bill_match_reasons,
                    document_required=document_required,
                ),
                checks.check_required_document(
                    bill_match_status=expense.bill_match_status,
                    policy=policy,
                    document_required=document_required,
                    bill_match_reasons=expense.bill_match_reasons,
                ),
                checks.check_category_consistency(
                    category=expense.category,
                    bill=candidate_bill,
                    document_required=document_required,
                ),
                checks.check_duplicate_bill(
                    bill_match_status=expense.bill_match_status,
                    bill=bill,
                    is_duplicate=(
                        bool(expense.bill_id)
                        and bill_usage.get(expense.bill_id, 0) > 1
                        and bill_original_expense.get(expense.bill_id) is not expense
                    ),
                    original_expense=bill_original_expense.get(expense.bill_id) if expense.bill_id else None,
                    document_required=document_required,
                ),
                # Catering must be judged against the same candidate evidence
                # amount/date/category use, not gated separately behind full
                # confirmation - otherwise a claim could reach PASS via
                # genuinely-matching candidate evidence while its bundled
                # catering charge never gets reviewed, simply because the
                # combined ranking score didn't cross the auto-confirm bar.
                checks.check_catering(category=expense.category, bill=candidate_bill, claimed_amount=expense.claimed_amount),
                checks.check_batta_trip_match(
                    category=expense.category,
                    expense_date=expense.expense_date,
                    trip_match_status=expense.trip_match_status,
                    matched_trip=trips_by_id.get(expense.trip_report_id) if expense.trip_report_id else None,
                    employee_mismatch=expense.trip_employee_mismatch,
                ),
            ]

            for result in results:
                _persist_check(db, report=report, expense=expense, policy=policy, result=result)

            expense.validation_status = _aggregate_status(results)

        extracted_sum = sum((float(e.claimed_amount) for e in expenses if e.claimed_amount is not None), 0.0)
        total_result = checks.check_total(extracted_sum=extracted_sum, reported_total=report.total_claimed)
        _persist_check(db, report=report, expense=None, policy=None, result=total_result)

        db.commit()

        db.add(
            ProcessingLog(
                expense_report_id=report.id,
                filename=report.original_filename or report.filename,
                stage=STAGE,
                status="PROCESSED",
                message=f"Policy validation completed for {len(expenses)} expense(s).",
            )
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001 - one bad document must never break a batch
        db.rollback()
        db.add(
            ProcessingLog(
                expense_report_id=report.id,
                filename=report.original_filename or report.filename,
                stage=STAGE,
                status="FAILED",
                message=f"{type(exc).__name__}: {exc}",
            )
        )
        db.commit()
        raise
