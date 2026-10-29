"""Server-side session lifecycle (docs/backend-design.md §A3/§A5, D7).

Token: 256-bit ``secrets.token_urlsafe(32)`` in an httpOnly cookie; the DB
stores ONLY the SHA-256 hex digest — a leaked sessions table can't be
replayed (§A2). Every login issues a NEW token (no reuse). Lifetimes:
sliding idle (7 d default) + absolute cap (30 d default), both settings.

CSRF tokens are BOUND TO THE SESSION: HMAC-SHA256(token_hash, session id).
The key (token_hash) never leaves the server, so a CSRF value observed on
one session is worthless on another and cannot be forged; rotation comes
free with the session. When COOKIE_SECURE=true the cookies use the
``__Host-`` prefix (Secure + Path=/ + no Domain — browser-enforced).

No commits here: callers own the transaction boundary (§A8).
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DBSession

from app.core.config import get_settings
from app.models.session import Session as SessionRow
from app.models.user import User

SESSION_COOKIE = "sid"
CSRF_COOKIE = "csrf"
# Sliding renewal is a throttled write: only rewrite idle_expires_at when
# more than 10 minutes of idle budget was consumed (§A5).
RENEWAL_THRESHOLD = timedelta(minutes=10)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def session_cookie_name() -> str:
    return "__Host-sid" if get_settings().cookie_secure else SESSION_COOKIE


def csrf_cookie_name() -> str:
    return "__Host-csrf" if get_settings().cookie_secure else CSRF_COOKIE


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token_for(row: SessionRow) -> str:
    """Session-bound CSRF token: HMAC keyed on the server-only token hash."""
    return hmac.new(
        row.token_hash.encode("utf-8"), str(row.id).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def create_session(
    db: DBSession,
    user_id,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, SessionRow]:
    """Issue a fresh token (also on every login — never reuse), store the
    hash; returns (raw token for the cookie, the session row)."""
    settings = get_settings()
    now = utcnow()
    row = SessionRow(
        user_id=user_id,
        token_hash=_hash_token(token := secrets.token_urlsafe(32)),
        expires_at=now + timedelta(days=settings.session_absolute_days),
        idle_expires_at=now + timedelta(hours=settings.session_idle_hours),
        ip=ip,
        user_agent=user_agent,
    )
    db.add(row)
    db.flush()
    return token, row


def lookup_session(db: DBSession, token: str) -> tuple[SessionRow, User] | None:
    """Cookie value → (session row, user) or None.

    None for: unknown token, revoked, past absolute cap, or idle-expired.
    The disabled-user check happens in the auth dependency (user row is
    returned so the caller can apply it).
    """
    row = db.execute(
        select(SessionRow)
        .where(SessionRow.token_hash == _hash_token(token))
        # refresh from the result even if the identity map already holds
        # this row: Core-style UPDATEs (revoke_user_sessions) don't touch
        # cached attributes, and a long-lived session must not see a stale
        # revoked/expiry state
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        return None
    now = utcnow()
    if row.revoked_at is not None or now >= row.expires_at or now >= row.idle_expires_at:
        return None
    user = db.get(User, row.user_id)
    if user is None:  # cascade should make this impossible; be strict anyway
        return None
    return row, user


def revoke_session(db: DBSession, row: SessionRow) -> None:
    row.revoked_at = utcnow()
    db.flush()


def revoke_user_sessions(db: DBSession, user_id, *, keep_id=None) -> int:
    """Revoke all live sessions of a user (password change / disable).
    ``keep_id`` survives — the current session outlives a self-change."""
    result = db.execute(
        update(SessionRow)
        .where(
            SessionRow.user_id == user_id,
            SessionRow.revoked_at.is_(None),
            SessionRow.id != keep_id,
        )
        .values(revoked_at=utcnow())
    )
    return result.rowcount


def renew_if_due(db: DBSession, row: SessionRow) -> bool:
    """Throttled sliding renewal (§A5). True when idle_expires_at moved."""
    settings = get_settings()
    idle = timedelta(hours=settings.session_idle_hours)
    now = utcnow()
    last_write = row.idle_expires_at - idle  # ≈ the previous renewal moment
    if now - last_write >= RENEWAL_THRESHOLD:
        row.idle_expires_at = now + idle
        db.flush()
        return True
    return False
