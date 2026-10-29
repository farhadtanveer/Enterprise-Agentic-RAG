"""B2: server-side session service — sha256 storage, expiry, renewal."""
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.session import Session as SessionRow
from app.models.user import User
from app.services import sessions


def _make_user(db, username="sess-user", role="user") -> User:
    user = User(username=username, password_hash="x", role=role)
    db.add(user)
    db.flush()
    return user


def test_create_stores_hash_never_token(db_session):
    user = _make_user(db_session)
    token = sessions.create_session(db_session, user.id, ip="203.0.113.9", user_agent="UA")
    row = db_session.execute(select(SessionRow)).scalar_one()
    assert token != row.token_hash  # the raw token is nowhere in the DB
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert str(token) not in str(row.token_hash)
    assert str(row.ip) == "203.0.113.9"  # psycopg returns ipaddress objects
    assert row.user_agent == "UA"
    assert row.revoked_at is None


def test_new_session_issued_per_login_no_reuse(db_session):
    user = _make_user(db_session)
    t1 = sessions.create_session(db_session, user.id)
    t2 = sessions.create_session(db_session, user.id)
    assert t1 != t2  # every login mints a fresh token
    rows = db_session.execute(select(SessionRow)).scalars().all()
    assert len({r.token_hash for r in rows}) == 2


def test_lookup_roundtrip(db_session):
    user = _make_user(db_session)
    token = sessions.create_session(db_session, user.id)
    found = sessions.lookup_session(db_session, token)
    assert found is not None
    row, found_user = found
    assert found_user.id == user.id


def test_lookup_rejects_unknown_token(db_session):
    _make_user(db_session)
    assert sessions.lookup_session(db_session, "garbage") is None


def test_lookup_rejects_revoked_session(db_session):
    user = _make_user(db_session)
    token = sessions.create_session(db_session, user.id)
    row = db_session.execute(select(SessionRow)).scalar_one()
    sessions.revoke_session(db_session, row)
    db_session.commit()
    assert sessions.lookup_session(db_session, token) is None


def _expired_session(db_session, user_id, **overrides) -> str:
    """Insert a session whose lifetimes already ran out."""
    token = "manual-" + hashlib.sha256(user_id.bytes).hexdigest()[:16]
    now = sessions.utcnow()
    row = SessionRow(
        user_id=user_id,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=overrides.get("expires_at", now - timedelta(seconds=1)),
        idle_expires_at=overrides.get("idle_expires_at", now + timedelta(days=1)),
        created_at=now - timedelta(days=31),
    )
    db_session.add(row)
    db_session.flush()
    return token


def test_lookup_rejects_absolutely_expired(db_session):
    user = _make_user(db_session)
    token = _expired_session(db_session, user.id)  # expires_at in the past
    assert sessions.lookup_session(db_session, token) is None


def test_lookup_rejects_idle_expired(db_session):
    user = _make_user(db_session)
    now = sessions.utcnow()
    token = _expired_session(
        db_session,
        user.id,
        expires_at=now + timedelta(days=10),
        idle_expires_at=now - timedelta(seconds=1),
    )
    assert sessions.lookup_session(db_session, token) is None


def test_sliding_renewal_throttled_write(db_session):
    user = _make_user(db_session)
    token = sessions.create_session(db_session, user.id)
    row = db_session.execute(select(SessionRow)).scalar_one()

    # minutes after login: within the 10-min threshold → no write
    assert sessions.renew_if_due(db_session, row) is False
    before = row.idle_expires_at

    # pretend the last renewal was 20 minutes ago → due
    idle = timedelta(hours=168)
    row.idle_expires_at = sessions.utcnow() + idle - timedelta(minutes=20)
    assert sessions.renew_if_due(db_session, row) is True
    after = row.idle_expires_at
    assert after > before
    # renewed window is a full idle period from ~now
    assert after - sessions.utcnow() > idle - timedelta(minutes=1)
    # cookie value still maps to the same row (hash unchanged)
    assert sessions.lookup_session(db_session, token) is not None


def test_revoke_user_sessions_keeps_current(db_session):
    user = _make_user(db_session)
    tokens = [sessions.create_session(db_session, user.id) for _ in range(3)]
    rows = db_session.execute(select(SessionRow).order_by(SessionRow.created_at)).scalars().all()
    keep = rows[0]
    revoked = sessions.revoke_user_sessions(db_session, user.id, keep_id=keep.id)
    assert revoked == 2
    assert sessions.lookup_session(db_session, tokens[0]) is not None  # kept alive
    assert sessions.lookup_session(db_session, tokens[1]) is None
    assert sessions.lookup_session(db_session, tokens[2]) is None


def test_revoke_user_sessions_scopes_to_user(db_session):
    a = _make_user(db_session, username="sess-a")
    b = _make_user(db_session, username="sess-b")
    sessions.create_session(db_session, a.id)
    b_token = sessions.create_session(db_session, b.id)
    sessions.revoke_user_sessions(db_session, a.id)
    assert sessions.lookup_session(db_session, b_token) is not None  # B untouched


def test_lifetime_settings_shape_the_row(db_session):
    user = _make_user(db_session)
    sessions.create_session(db_session, user.id)
    row = db_session.execute(select(SessionRow)).scalar_one()
    # DB-clock created_at vs Python-computed lifetimes: allow a small skew
    assert abs((row.expires_at - row.created_at).total_seconds() - 30 * 86400) < 5  # absolute (D7)
    assert abs((row.idle_expires_at - row.created_at).total_seconds() - 168 * 3600) < 5  # sliding
    assert row.created_at.tzinfo is not None  # timestamptz end to end
