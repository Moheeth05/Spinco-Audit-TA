"""Phase 10 - the one centralized permission matrix, and the FastAPI
dependencies every route uses to enforce it. Role checks are never
scattered ad-hoc through individual route bodies - every protected route
depends on `require_permission(...)` (or, for read-only "just be logged
in" routes, `get_current_user` alone), so the entire authorization surface
is auditable from this one file plus each route's own dependency list.

CSRF: folded into `get_current_user` itself rather than a separate
dependency, so every mutating route that already requires a permission
(and therefore depends on get_current_user) gets CSRF protection for free
with no extra boilerplate. GET/HEAD/OPTIONS are exempt (they must never
mutate state, so there is nothing for CSRF to protect there). See
services/auth.py / models/session.py for why a synchronizer token (rather
than relying on SameSite alone) was chosen.
"""

from fastapi import Cookie, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.services.auth import SESSION_COOKIE_NAME, get_valid_session

# ---------- Permission constants ----------

VIEW_DASHBOARD = "VIEW_DASHBOARD"
VIEW_ANALYTICS = "VIEW_ANALYTICS"
VIEW_EXPENSE_REPORTS = "VIEW_EXPENSE_REPORTS"
VIEW_TRIP_REPORTS = "VIEW_TRIP_REPORTS"
VIEW_DOCUMENTS = "VIEW_DOCUMENTS"
UPLOAD_DOCUMENTS = "UPLOAD_DOCUMENTS"
REPROCESS_DOCUMENTS = "REPROCESS_DOCUMENTS"
DELETE_DOCUMENTS = "DELETE_DOCUMENTS"
VALIDATE_REPORTS = "VALIDATE_REPORTS"
REVIEW_EXCEPTIONS = "REVIEW_EXCEPTIONS"
VIEW_EXCEL = "VIEW_EXCEL"
GENERATE_EXCEL = "GENERATE_EXCEL"
DOWNLOAD_EXCEL = "DOWNLOAD_EXCEL"
MANAGE_POLICY = "MANAGE_POLICY"
MANAGE_USERS = "MANAGE_USERS"
MANAGE_ROLES = "MANAGE_ROLES"
VIEW_AUDIT_LOGS = "VIEW_AUDIT_LOGS"
MANUAL_CORRECTION = "MANUAL_CORRECTION"
CHANGE_OWN_PASSWORD = "CHANGE_OWN_PASSWORD"

_READ_ONLY_BASE = {
    VIEW_DASHBOARD,
    VIEW_ANALYTICS,
    VIEW_EXPENSE_REPORTS,
    VIEW_TRIP_REPORTS,
    VIEW_DOCUMENTS,
    # Exceptions have no write/resolve endpoint anywhere in the current
    # application (ExceptionRecord.status is always OPEN, computed
    # entirely by Phase 7) - "reviewing" one is inherently read-only today,
    # so every role that can view anything can view exceptions.
    REVIEW_EXCEPTIONS,
    VIEW_EXCEL,
    DOWNLOAD_EXCEL,
    CHANGE_OWN_PASSWORD,
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "VIEWER": set(_READ_ONLY_BASE),
    "AUDITOR": _READ_ONLY_BASE
    | {
        UPLOAD_DOCUMENTS,
        REPROCESS_DOCUMENTS,
        VALIDATE_REPORTS,
        GENERATE_EXCEL,
        MANUAL_CORRECTION,
    },
    "ADMIN": _READ_ONLY_BASE
    | {
        UPLOAD_DOCUMENTS,
        REPROCESS_DOCUMENTS,
        DELETE_DOCUMENTS,
        VALIDATE_REPORTS,
        GENERATE_EXCEL,
        MANUAL_CORRECTION,
        MANAGE_POLICY,
        MANAGE_USERS,
        MANAGE_ROLES,
        VIEW_AUDIT_LOGS,
    },
}

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> User:
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    session = get_valid_session(db, session_token)
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid.")
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    if request.method not in _SAFE_METHODS:
        csrf_header = request.headers.get("X-CSRF-Token")
        if not csrf_header or csrf_header != session.csrf_token:
            raise HTTPException(status_code=403, detail="CSRF validation failed.")

    return user


def require_permission(permission: str):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if permission not in ROLE_PERMISSIONS.get(user.role, set()):
            raise HTTPException(status_code=403, detail="You do not have permission to perform this action.")
        return user

    return dependency
