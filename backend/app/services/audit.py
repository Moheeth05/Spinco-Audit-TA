"""Phase 10 - the single writer for AuditLog rows. Never logs passwords,
password hashes, session tokens, or CSRF tokens - every call site below
passes only business-meaningful details (old/new role, filenames, report
IDs, etc.). Focuses on meaningful state changes, never routine GET
requests, per the Phase 10 spec.

Deliberately does NOT commit (Phase 1 hardening - see
docs/CENTRALIZED_ARCHITECTURE.md §16). It used to call db.commit() itself,
which meant every call site actually made two separate transactions: one
for the business change, one for this audit row - so a crash between them
could leave a business action committed with no audit record, or (in
theory) the reverse. Callers now call this BEFORE their own single
db.commit(), so the business change and its audit row always succeed or
fail together, atomically, as one transaction. See each call site for the
same "log_action(...) then commit()" ordering.
"""

from sqlalchemy.orm import Session

from app.models.logs import AuditLog


def log_action(
    db: Session,
    *,
    user_id: int | None,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    details: str | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
        )
    )
    db.flush()


# Action constants - exact strings, used consistently by every call site and
# by tests, so audit events are never accidentally mis-typed differently in
# different places.
AUTH_LOGIN_SUCCESS = "AUTH_LOGIN_SUCCESS"
AUTH_LOGIN_FAILURE = "AUTH_LOGIN_FAILURE"
AUTH_LOGOUT = "AUTH_LOGOUT"

USER_CREATED = "USER_CREATED"
USER_UPDATED = "USER_UPDATED"
USER_ROLE_CHANGED = "USER_ROLE_CHANGED"
USER_ACTIVATED = "USER_ACTIVATED"
USER_DEACTIVATED = "USER_DEACTIVATED"
USER_PASSWORD_CHANGED = "USER_PASSWORD_CHANGED"
ADMIN_PASSWORD_RESET = "ADMIN_PASSWORD_RESET"

DOCUMENT_UPLOADED = "DOCUMENT_UPLOADED"
DOCUMENT_REPROCESSED = "DOCUMENT_REPROCESSED"
DOCUMENT_DELETED = "DOCUMENT_DELETED"
DOCUMENT_ARCHIVED = "DOCUMENT_ARCHIVED"
DOCUMENT_RESTORED = "DOCUMENT_RESTORED"
STORAGE_RECONCILED = "STORAGE_RECONCILED"

REPORT_VALIDATED = "REPORT_VALIDATED"
REPORT_REVALIDATED = "REPORT_REVALIDATED"

BILL_REMATCHED = "BILL_REMATCHED"
TRIP_REMATCHED = "TRIP_REMATCHED"

EXCEPTION_VERIFIED = "EXCEPTION_VERIFIED"
# System events written by a full reprocess (extraction/verification_carryover.py): a manual
# verification followed its claim to the recreated Expense row, or was left where it is.
VERIFICATION_CARRIED_OVER = "VERIFICATION_CARRIED_OVER"
VERIFICATION_NOT_CARRIED_OVER = "VERIFICATION_NOT_CARRIED_OVER"

EXCEL_GENERATED = "EXCEL_GENERATED"
EXCEL_DOWNLOADED = "EXCEL_DOWNLOADED"
