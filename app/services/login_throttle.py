"""(username, client IP) exponential login throttle (docs/backend-design.md §A5).

Counters are keyed on sha256(pepper + normalized username) + ip — the
plaintext username is never stored, and unknown usernames are throttled
exactly like known ones (the attacker can't tell which is which). 5th
failure starts a 15-minute delay for that pair, doubling per further
failure, capped at 1 hour, reset on success. No hard username-wide lock:
a shared-IP office or an attacker can never freeze a victim's account
everywhere at once (§A5).

Deliberately NOT timing-hard: the counter lookup leaks nothing useful —
it runs identically for known and unknown usernames.

No commits here: callers own the transaction boundary (§A8).
"""
import hashlib
import logging
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DBSession

from app.core.config import get_settings
from app.models.login_throttle import LoginThrottle

THRESHOLD_FAILURES = 5
BASE_DELAY = timedelta(minutes=15)
MAX_DELAY = timedelta(hours=1)

logger = logging.getLogger(__name__)
# Fixed fallback so dev works out of the box; production sets a real pepper.
_DEV_FALLBACK_PEPPER = "dev-only-throttle-pepper-do-not-use-in-production"
_warned = False


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _username_hash(username: str) -> str:
    global _warned
    settings = get_settings()
    pepper = settings.login_throttle_pepper
    if not pepper:
        pepper = _DEV_FALLBACK_PEPPER
        if not _warned:
            logger.warning(
                "LOGIN_THROTTLE_PEPPER is unset — using a fixed dev fallback. "
                "Set a real secret in production, or throttle rows could be "
                "reversed into usernames."
            )
            _warned = True
    normalized = username.strip().lower()
    return hashlib.sha256((pepper + normalized).encode("utf-8")).hexdigest()


def remaining_seconds(
    db: DBSession, username: str, ip: str, *, now: datetime | None = None
) -> int:
    """Seconds the (username, ip) pair is still locked out; 0 when free."""
    row = db.execute(
        select(LoginThrottle).where(LoginThrottle.username_hash == _username_hash(username), LoginThrottle.ip == ip)
    ).scalar_one_or_none()
    if row is None or row.throttle_until is None:
        return 0
    now = now or utcnow()
    if now >= row.throttle_until:
        return 0
    return math.ceil((row.throttle_until - now).total_seconds())


def record_failure(
    db: DBSession, username: str, ip: str, *, now: datetime | None = None
) -> None:
    """Count one failed attempt; from the 5th, arm the exponential delay."""
    now = now or utcnow()
    key = _username_hash(username)
    row = db.execute(
        select(LoginThrottle).where(LoginThrottle.username_hash == key, LoginThrottle.ip == ip)
    ).scalar_one_or_none()
    if row is None:
        db.add(LoginThrottle(username_hash=key, ip=ip, fail_count=1, last_failure_at=now))
        db.flush()
        return
    row.fail_count += 1
    row.last_failure_at = now
    if row.fail_count >= THRESHOLD_FAILURES:
        delay = min(BASE_DELAY * (2 ** (row.fail_count - THRESHOLD_FAILURES)), MAX_DELAY)
        row.throttle_until = now + delay
    db.flush()


def reset(db: DBSession, username: str, ip: str) -> None:
    """Successful login clears the counter for that pair (§A5)."""
    db.execute(
        delete(LoginThrottle).where(
            LoginThrottle.username_hash == _username_hash(username), LoginThrottle.ip == ip
        )
    )
    db.flush()


def clear_for_username(db: DBSession, username: str) -> int:
    """Operator recovery (``python -m app.cli unlock``): drop every counter
    row for this username, across all IPs. Returns rows removed."""
    result = db.execute(
        delete(LoginThrottle).where(LoginThrottle.username_hash == _username_hash(username))
    )
    return result.rowcount
