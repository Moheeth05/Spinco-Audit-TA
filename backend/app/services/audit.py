"""Phase 10 - the single writer for AuditLog rows. Never logs passwords,
password hashes, session tokens, or CSRF tokens - every call site below
passes only business-meaningful details (old/new role, filenames, report
IDs, etc.). Focuses on meaningful state changes, never routine GET
requests, per the Phase 10 spec.
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
    db.commit()


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

REPORT_VALIDATED = "REPORT_VALIDATED"
REPORT_REVALIDATED = "REPORT_REVALIDATED"

BILL_REMATCHED = "BILL_REMATCHED"
TRIP_REMATCHED = "TRIP_REMATCHED"

EXCEPTION_VERIFIED = "EXCEPTION_VERIFIED"

EXCEL_GENERATED = "EXCEL_GENERATED"
EXCEL_DOWNLOADED = "EXCEL_DOWNLOADED"
