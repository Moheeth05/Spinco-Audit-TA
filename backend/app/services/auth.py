"""Phase 10 - password hashing and server-managed session lifecycle.

Password hashing uses Argon2 (via argon2-cffi), never plaintext, never a
fast/reversible scheme. Sessions are opaque random tokens the browser holds
in an HttpOnly cookie; only a SHA-256 hash of the token is ever persisted
(hashing a high-entropy random token with a fast hash is standard practice
and distinct from password hashing - the token itself already has enough
entropy that speed doesn't matter, unlike a human-chosen password), so a
database read alone can never be replayed as a valid session. See
models/session.py for why this was chosen over a self-contained JWT.
"""

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy.orm import Session

from app.models.session import AuthSession
from app.models.user import User

SESSION_COOKIE_NAME = "spinco_session"
SESSION_TTL = timedelta(hours=12)
MIN_PASSWORD_LENGTH = 8

# Argon2 cost parameters are deliberately expensive in production (that is
# the entire point of the algorithm) but that cost is pure overhead in the
# test suite, which hashes/verifies a password on every single test via the
# `client` fixture. Only the test suite overrides these (see conftest.py,
# which sets the env vars before the app - and this module - is imported);
# production always gets argon2-cffi's strong defaults.
_hasher = PasswordHasher(
    time_cost=int(os.environ.get("SPINCO_ARGON2_TIME_COST", "3")),
    memory_cost=int(os.environ.get("SPINCO_ARGON2_MEMORY_COST", "65536")),
    parallelism=int(os.environ.get("SPINCO_ARGON2_PARALLELISM", "4")),
)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:  # noqa: BLE001 - a malformed/legacy hash must never crash login
        return False


def validate_password_policy(password: str) -> str | None:
    """Returns an error message if the password is unacceptable, else None.
    Deliberately permissive beyond a minimum length - see Phase 10 spec's
    own "do not create an unnecessarily restrictive policy" instruction."""
    if not password:
        return "Password is required."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(db: Session, user: User) -> tuple[str, str, AuthSession]:
    """Creates a new session row and returns (raw_token, csrf_token, row).
    The raw_token is returned exactly once here - only its hash is ever
    stored - the caller must set it in the response cookie immediately."""
    raw_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    row = AuthSession(
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        csrf_token=csrf_token,
        created_at=now,
        expires_at=now + SESSION_TTL,
        last_seen_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return raw_token, csrf_token, row


def get_valid_session(db: Session, raw_token: str) -> AuthSession | None:
    if not raw_token:
        return None
    row = db.query(AuthSession).filter(AuthSession.token_hash == _hash_token(raw_token)).first()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        db.delete(row)
        db.commit()
        return None
    row.last_seen_at = now
    db.commit()
    return row


def invalidate_session(db: Session, raw_token: str) -> None:
    if not raw_token:
        return
    db.query(AuthSession).filter(AuthSession.token_hash == _hash_token(raw_token)).delete()
    db.commit()


def invalidate_all_sessions_for_user(db: Session, user_id: int, *, except_token: str | None = None) -> None:
    """Used on password change/deactivation - signs the user out everywhere
    (optionally keeping the session that just performed the change alive)."""
    q = db.query(AuthSession).filter(AuthSession.user_id == user_id)
    if except_token:
        q = q.filter(AuthSession.token_hash != _hash_token(except_token))
    q.delete()
    db.commit()
