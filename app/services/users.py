"""User primitives shared by the CLIs (B2) and admin endpoints (B6).

Core functions are deliberately thin: they do the DB work + write the
audit event, but the CALLER owns commit and transaction boundaries (§A8).
All of them raise ValueError with an operator-friendly message on policy
violations; CLI prompts translate that into a re-prompt, endpoints into
422s.
"""
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models.user import User
from app.services import passwords
from app.services.audit_events import record_event
from app.services.login_throttle import clear_for_username
from app.services.sessions import revoke_user_sessions

# Usernames: stored lowercase (schema CHECK), modest charset, 3–64.
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,64}$")


def normalize_username(raw: str) -> str:
    return raw.strip().lower()


def get_by_username(db: DBSession, username: str) -> User | None:
    return db.execute(
        select(User).where(User.username == normalize_username(username))
    ).scalar_one_or_none()


def has_any_admin(db: DBSession) -> bool:
    """Any admin row at all (active or not) — bootstrap's refusal rule."""
    return (
        db.execute(select(func.count()).select_from(User).where(User.role == "admin")).scalar_one()
        > 0
    )


def active_admin_count(db: DBSession) -> int:
    """users WHERE role='admin' AND is_active — the last-admin gauge (§A5)."""
    return db.execute(
        select(func.count())
        .select_from(User)
        .where(User.role == "admin", User.is_active.is_(True))
    ).scalar_one()


def would_leave_zero_admins(db: DBSession, user: User) -> bool:
    """Last-admin guard helper (§A5): True if disabling/demoting/deleting
    this user would leave zero active admins. B6 endpoints map True → 409."""
    if user.role != "admin" or not user.is_active:
        return False
    return active_admin_count(db) <= 1


def create_user(
    db: DBSession,
    username: str,
    *,
    password: str,
    role: str = "user",
    display_name: str | None = None,
    must_change_password: bool = False,
) -> User:
    """Create a user from a plaintext password (hashed here — no plaintext
    is ever stored). Raises ValueError on policy violations."""
    username = normalize_username(username)
    problems = passwords.validate_password(password, username)
    if problems:
        raise ValueError("; ".join(problems))
    user = User(
        username=username,
        password_hash=passwords.hash_password(password),
        role=role,
        display_name=display_name,
        must_change_password=must_change_password,
        password_changed_at=datetime.now(timezone.utc) if not must_change_password else None,
    )
    db.add(user)
    db.flush()
    return user


def bootstrap_first_admin(db: DBSession, username: str, password: str) -> User:
    """First-admin bootstrap (§A5): refuses if ANY admin already exists —
    there is no default password anywhere, so the first admin is born here."""
    if has_any_admin(db):
        raise ValueError("an admin already exists; bootstrap-admin refuses to run")
    user = create_user(db, username, password=password, role="admin")
    record_event(
        db,
        "user_created",
        actor_user_id=user.id,
        object_type="user",
        object_id=user.id,
        outcome="ok",
        details={"role": "admin", "via": "bootstrap-admin"},
    )
    return user


def _apply_password(db: DBSession, user: User, password: str, *, must_change: bool) -> None:
    problems = passwords.validate_password(password, user.username)
    if problems:
        raise ValueError("; ".join(problems))
    user.password_hash = passwords.hash_password(password)
    user.must_change_password = must_change
    user.password_changed_at = datetime.now(timezone.utc)


def change_own_password(
    db: DBSession, user: User, new_password: str, *, keep_session_id=None
) -> None:
    """POST /api/auth/password: re-hash, clear the forced-change flag, revoke
    all OTHER sessions; the current session survives (§A5)."""
    _apply_password(db, user, new_password, must_change=False)
    revoked = revoke_user_sessions(db, user.id, keep_id=keep_session_id)
    record_event(
        db,
        "password_changed",
        actor_user_id=user.id,
        object_type="user",
        object_id=user.id,
        outcome="ok",
        details={"revoked_sessions": revoked},
    )


def admin_reset_password(db: DBSession, user: User, temp_password: str) -> None:
    """Temp password + must_change_password + revoke sessions (§A5).
    Used by the recovery CLI (B2) and the admin endpoint (B6)."""
    _apply_password(db, user, temp_password, must_change=True)
    revoked = revoke_user_sessions(db, user.id)
    record_event(
        db,
        "password_reset_by_admin",
        actor_user_id=None,  # operator action — no acting user row
        object_type="user",
        object_id=user.id,
        outcome="ok",
        details={"target_user_id": str(user.id), "revoked_sessions": revoked, "via": "cli"},
    )


def unlock_username(db: DBSession, username: str) -> int:
    """Recovery CLI: clear (username, IP) throttle records; audit it."""
    cleared = clear_for_username(db, username)
    record_event(
        db,
        "account_unlocked",
        actor_user_id=None,
        object_type="user",
        outcome="ok",
        details={"username_cleared": True, "cleared_records": cleared},
    )
    return cleared
