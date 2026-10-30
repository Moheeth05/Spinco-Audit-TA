"""History of manual verifications (models/validation.py's ExceptionVerificationHistory).

Two places retire a live ExceptionVerification state, and both keep it here first, inside the caller's
transaction (neither commits):

  * api/exceptions.py: an explicit re-verification overwrites the live row, whose id is preserved.
  * api/documents.py: deleting a report removes its live verifications. A live verification is keyed by
    (expense_report_id, expense_id, exception_type) and SQLite reuses freed ids, so a live row left behind
    could be applied to a LATER report that happens to receive the same ids. Nothing here relies on id
    uniqueness to prevent that: the live rows are removed, and their last state is kept as history.

History rows are append-only: nothing here updates or deletes one.
"""

from sqlalchemy.orm import Session

from app.models.validation import ExceptionVerification, ExceptionVerificationHistory


def snapshot(verification: ExceptionVerification, superseded_by_id: int | None) -> ExceptionVerificationHistory:
    """The state of `verification` as it is right now, as a (not yet added) history row."""
    return ExceptionVerificationHistory(
        verification_id=verification.id,
        expense_report_id=verification.expense_report_id,
        expense_id=verification.expense_id,
        exception_type=verification.exception_type,
        original_severity=verification.original_severity,
        original_reason=verification.original_reason,
        evidence_signature=verification.evidence_signature,
        verified_by_id=verification.verified_by_id,
        verified_at=verification.verified_at,
        verification_note=verification.verification_note,
        superseded_by_id=superseded_by_id,
    )


def retire_for_report(db: Session, report_id: int, user_id: int | None) -> list[int]:
    """Keep every live verification of this report (expense-level and report-level) as history, then delete
    the live rows. Returns the retired verification ids. Never touches another report's rows."""
    rows = (
        db.query(ExceptionVerification)
        .filter(ExceptionVerification.expense_report_id == report_id)
        .order_by(ExceptionVerification.id)
        .all()
    )
    retired: list[int] = []
    for row in rows:
        db.add(snapshot(row, user_id))
        retired.append(row.id)
        db.delete(row)
    return retired
