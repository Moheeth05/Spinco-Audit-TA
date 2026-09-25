import os
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.auth import ChangePasswordRequest, LoginRequest, LoginResponse
from app.schemas.user import UserOut
from app.services import audit
from app.services.auth import (
    SESSION_COOKIE_NAME,
    create_session,
    get_valid_session,
    hash_password,
    invalidate_all_sessions_for_user,
    invalidate_session,
    validate_password_policy,
    verify_password,
)
from app.services.permissions import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Secure=false is required for local HTTP development (Vite on
# http://localhost:5200 talking to http://localhost:8001) - set
# SPINCO_COOKIE_SECURE=true for any deployment served over HTTPS.
_COOKIE_SECURE = os.environ.get("SPINCO_COOKIE_SECURE", "false").lower() == "true"


def _set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=_COOKIE_SECURE,
        samesite="lax",
        path="/",
        max_age=12 * 60 * 60,
    )


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    # Deliberately generic failure message/behavior throughout this function -
    # never reveal whether the email exists, only whether the credentials as
    # a whole were accepted.
    generic_error = HTTPException(status_code=401, detail="Invalid email or password.")

    email = (payload.email or "").strip().lower()
    user = db.query(User).filter(User.email == email).first()

    if user is None or not user.password_hash or not verify_password(payload.password, user.password_hash):
        audit.log_action(
            db, user_id=user.id if user else None, action=audit.AUTH_LOGIN_FAILURE, entity_type="User",
            entity_id=str(user.id) if user else None, details=f"Failed login attempt for {email!r}",
        )
        # log_action only flushes (Phase 1 hardening - see
        # docs/CENTRALIZED_ARCHITECTURE.md §16) - this path has no other
        # commit, so it must commit explicitly or the audit row is lost.
        db.commit()
        raise generic_error

    if not user.is_active:
        audit.log_action(
            db, user_id=user.id, action=audit.AUTH_LOGIN_FAILURE, entity_type="User", entity_id=str(user.id),
            details="Login attempt on a deactivated account",
        )
        db.commit()
        raise generic_error

    raw_token, csrf_token, _session = create_session(db, user)
    user.last_login_at = datetime.now(timezone.utc)
    audit.log_action(db, user_id=user.id, action=audit.AUTH_LOGIN_SUCCESS, entity_type="User", entity_id=str(user.id))
    db.commit()
    db.refresh(user)

    _set_session_cookie(response, raw_token)
    return LoginResponse(user=UserOut.model_validate(user), csrf_token=csrf_token)


@router.post("/logout")
def logout(
    response: Response,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    if session_token:
        session = get_valid_session(db, session_token)
        if session:
            audit.log_action(db, user_id=session.user_id, action=audit.AUTH_LOGOUT, entity_type="User", entity_id=str(session.user_id))
            db.commit()  # log_action no longer commits itself - see docs/CENTRALIZED_ARCHITECTURE.md §16
        invalidate_session(db, session_token)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"detail": "Logged out."}


@router.get("/me", response_model=LoginResponse)
def me(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    # Returns the CSRF token alongside the user, not just the user - the
    # frontend's csrf_token is in-memory only and is lost on every full page
    # reload (F5), even though the session cookie itself survives. Without
    # this, a reload would silently break every mutating request behind a
    # generic "CSRF validation failed" until the user logged out and back in.
    session = get_valid_session(db, session_token)
    return LoginResponse(user=UserOut.model_validate(user), csrf_token=session.csrf_token if session else "")


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    if not user.password_hash or not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    if payload.new_password != payload.confirm_new_password:
        raise HTTPException(status_code=400, detail="New password and confirmation do not match.")
    error = validate_password_policy(payload.new_password)
    if error:
        raise HTTPException(status_code=400, detail=error)

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    audit.log_action(db, user_id=user.id, action=audit.USER_PASSWORD_CHANGED, entity_type="User", entity_id=str(user.id))
    db.commit()

    # Sign the user out everywhere else - the session that just performed
    # the change stays alive so the UI doesn't immediately bounce them.
    invalidate_all_sessions_for_user(db, user.id, except_token=session_token)
    return {"detail": "Password changed."}
