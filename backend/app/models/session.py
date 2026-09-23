from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuthSession(Base):
    """A server-managed login session (Phase 10) - the authoritative
    authentication record, not a JWT. The browser only ever holds an opaque
    random token in an HttpOnly cookie; this table stores a SHA-256 hash of
    that token (never the raw token itself, so a database read alone can't
    be used to hijack a session), plus a separate CSRF synchronizer token
    for state-changing requests. Logout / password-change / admin
    deactivation all just delete or expire rows here, which is why this
    architecture (rather than a self-contained JWT) was chosen - trivial,
    immediate, server-side invalidation."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
