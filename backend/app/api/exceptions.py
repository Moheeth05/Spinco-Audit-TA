"""Manual "Verify" workflow for a REVIEW REQUIRED or EXCEPTION condition
(post-UAT correction 2, Parts 20-26; extended to EXCEPTION severity per a
later request) - a real backend state transition, not a frontend-only label
change. See models/validation.py's ExceptionVerification for why this is a
separate, durable table rather than a field written directly onto the
(wholesale-replaced) ExceptionRecord row.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.expense_reports import build_exception_out
from app.database import get_db
from app.models.expense_report import Expense
from app.models.user import User
from app.models.validation import ExceptionRecord, ExceptionVerification, ValidationResult
from app.schemas.exceptions import ExceptionRecordOut, VerifyExceptionIn
from app.services import audit, verification_history
from app.services.excel_generator import generate_workbook
from app.services.exception_engine import evaluate_report, signature_for_record, strip_stale_note
from app.services.permissions import MANUAL_CORRECTION, require_permission

router = APIRouter(prefix="/api/exceptions", tags=["exceptions"])
_verify_permission = Depends(require_permission(MANUAL_CORRECTION))


@router.post("/{exception_id}/verify", response_model=ExceptionRecordOut)
def verify_exception(
    exception_id: int, payload: VerifyExceptionIn, db: Session = Depends(get_db), user: User = _verify_permission
) -> ExceptionRecordOut:
    record = db.get(ExceptionRecord, exception_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Exception not found.")
    if record.severity not in ("REVIEW REQUIRED", "EXCEPTION"):
        raise HTTPException(status_code=400, detail="Only a REVIEW REQUIRED or EXCEPTION condition can be verified.")
    if record.status == "RESOLVED":
        raise HTTPException(status_code=400, detail="This condition has already been verified.")
    # A confirmed EXCEPTION is a real policy violation, not an ambiguous
    # match the system merely couldn't confirm - accepting one (e.g. a
    # legitimate rounding/GST difference between claimed and bill amount)
    # is a real judgement call that overrides an automated finding, so it
    # requires the auditor to actually write down why, unlike an optional
    # note on an ordinary REVIEW REQUIRED confirmation.
    if record.severity == "EXCEPTION" and not (payload.note and payload.note.strip()):
        raise HTTPException(
            status_code=400,
            detail="A note explaining why this exception is acceptable is required to verify a confirmed EXCEPTION.",
        )

    # Upsert: verifying the same (expense, exception_type) again just
    # updates who/when/why, rather than accumulating duplicate history rows.
    existing = (
        db.execute(
            select(ExceptionVerification).where(
                ExceptionVerification.expense_report_id == record.expense_report_id,
                ExceptionVerification.expense_id == record.expense_id,
                ExceptionVerification.exception_type == record.exception_type,
            )
        )
        .scalars()
        .first()
    )
    # What the auditor is approving: the reason as the checks produced it (without the
    # "previous verification no longer applies" note a stale approval adds to the display) and
    # a signature of the typed evidence behind it - evaluate_report() re-applies this
    # approval only while that evidence is unchanged.
    approved_reason = strip_stale_note(record.reason)
    signature = signature_for_record(db, record)
    if existing is not None:
        # The state being replaced is first preserved in the history table, in this same
        # transaction (the note text is deliberately not in the audit event, so this is where
        # it survives). The live row is then updated in place: its id is preserved, and a
        # note is replaced only when a new one is given.
        db.add(verification_history.snapshot(existing, user.id))
        existing.original_severity = record.severity
        existing.original_reason = approved_reason
        existing.evidence_signature = signature
        existing.verified_by_id = user.id
        existing.verified_at = datetime.now(timezone.utc)
        if payload.note and payload.note.strip():
            existing.verification_note = payload.note
    else:
        db.add(
            ExceptionVerification(
                expense_report_id=record.expense_report_id,
                expense_id=record.expense_id,
                exception_type=record.exception_type,
                original_severity=record.severity,
                original_reason=approved_reason,
                evidence_signature=signature,
                verified_by_id=user.id,
                verification_note=payload.note,
            )
        )
    # Audit record written in the SAME transaction as the verification
    # upsert above (Phase 1 hardening - see docs/CENTRALIZED_ARCHITECTURE.md
    # §16) - the durable source-of-truth business action here is the
    # ExceptionVerification row itself, not the wholesale-replace recompute
    # below, so this is where atomicity actually matters most.
    audit.log_action(
        db,
        user_id=user.id,
        action=audit.EXCEPTION_VERIFIED,
        entity_type="ExceptionRecord",
        entity_id=str(exception_id),
        # The free-text note is stored on the verification record (and returned by the exceptions
        # API); it is deliberately NOT copied into the audit details.
        details=f"{record.exception_type} on expense_id={record.expense_id} (report {record.expense_report_id})"
        + ("; note recorded" if payload.note and payload.note.strip() else ""),
    )
    db.commit()

    # Recompute everything the verification affects: the ExceptionRecord's
    # own status, the expense's validation_status, and the report-level
    # counts/overall_status that the dashboard/analytics/Excel all read from
    # (Part 21's 14-point checklist) - evaluate_report() is the single place
    # that already does this wholesale-replace + recompute, so it's reused
    # here rather than duplicating that logic.
    evaluate_report(db, record.expense_report_id)
    try:
        generate_workbook(db)
    except Exception:  # noqa: BLE001 - Excel regeneration must never fail the verification itself
        pass

    updated = (
        db.execute(
            select(ExceptionRecord).where(
                ExceptionRecord.expense_report_id == record.expense_report_id,
                ExceptionRecord.expense_id == record.expense_id,
                ExceptionRecord.exception_type == record.exception_type,
            )
        )
        .scalars()
        .first()
    )
    if updated is None:
        raise HTTPException(status_code=500, detail="Verification saved but the exception could not be re-read.")

    expense = db.get(Expense, updated.expense_id) if updated.expense_id else None
    vr = db.get(ValidationResult, updated.validation_result_id) if updated.validation_result_id else None
    verifier = db.get(User, updated.verified_by_id) if updated.verified_by_id else None
    return build_exception_out(updated, expense, vr, verifier.name if verifier else None)
