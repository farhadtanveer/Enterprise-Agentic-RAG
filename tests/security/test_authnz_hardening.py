"""AuthN/Z review (review task 4): forced-change gate vs routes added
later, X-Forwarded-For spoofing, enumeration side channels (byte sizes),
argon2 parameter floor + rehash-on-login, throttled-attempt audit content.

All pins are expected-PASS; they pin behaviors that the coverage map found
untested. One hole is documented in the report but has no test here:
login POST has no CSRF (standard, design is silent).
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import auth as auth_api
from app.core.db import get_db
from app.main import app as fastapi_app
from app.models.audit_event import AuditEvent
from app.models.user import User
from app.services import passwords, users
from app.services.ratelimit import SlidingWindowLimiter

PEER = ("203.0.113.5", 50000)
PASSWORD = "a valid longer passphrase"


def make_user(db, username, password=PASSWORD, **kw):
    return users.create_user(db, username, password=password, **kw)


def login(client, username, password=PASSWORD):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    return r


def logged_in_client(client, db, username):
    make_user(db, username)
    db.commit()
    r = login(client, username)
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


# --- forced-change gate covers routes added after the gate ------------------------


def test_forced_change_gate_blocks_real_conversation_routes(client, db_session):
    """The gate's CHANGE_EXEMPT list was written in B2 before the
    conversation routes existed; nothing in the suite proved the gate
    covers them (the only gate test used a synthetic probe route). Every
    conversation verb must 403 password_change_required for a
    must-change user; only GET /api/me passes."""
    make_user(db_session, "gated-user", must_change_password=True)
    db_session.commit()
    assert login(client, "gated-user").status_code == 200
    csrf = client.cookies.get("csrf")

    assert client.get("/api/me").status_code == 200  # exempt

    r = client.post("/api/conversations", json={"title": "t"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403 and r.json() == {"code": "password_change_required"}

    r = client.get("/api/conversations")
    assert r.status_code == 403 and r.json() == {"code": "password_change_required"}

    other = str(uuid.uuid4())
    r = client.post(
        f"/api/conversations/{other}/messages",
        json={"question": "while gated"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 403 and r.json() == {"code": "password_change_required"}


# --- throttle / rate limiter vs X-Forwarded-For ----------------------------------


def test_x_forwarded_for_cannot_reset_login_throttle(client, db_session):
    """The (username, IP) throttle keys on the SOCKET peer; a spoofed
    X-Forwarded-For must not buy a fresh lockout window. The design (§A3)
    forbids trusting the header from untrusted sources."""
    make_user(db_session, "xff-victim")
    db_session.commit()
    for _ in range(5):
        login(client, "xff-victim", "wrong password attempt")
    # pair is now locked: next attempt (even the RIGHT password) is 429
    spoof = {"X-Forwarded-For": "9.9.9.9", "X-Real-IP": "9.9.9.9"}
    r = client.post(
        "/api/auth/login",
        json={"username": "xff-victim", "password": PASSWORD},
        headers=spoof,
    )
    assert r.status_code == 429, "spoofed forwarded headers bypassed the login throttle"


def test_x_forwarded_for_cannot_reset_per_ip_rate_limit(client, db_session):
    """The SlidingWindowLimiter on POST /api/auth/login keys on the socket
    peer too; rotated X-Forwarded-For headers must not defeat it."""
    from app.services.ratelimit import SlidingWindowLimiter

    make_user(db_session, "xff-spray")
    db_session.commit()

    # a tiny window directly on the module's limiter (bypasses autouse fixture)
    tiny = SlidingWindowLimiter(3)
    auth_api.login_limiter = tiny
    try:
        for i in range(3):
            assert login(client, "xff-spray", f"nope-{i}").status_code == 401
        assert login(client, "xff-spray", "nope-4").status_code == 429
        # a 'new IP' via headers must NOT get a fresh bucket
        r = client.post(
            "/api/auth/login",
            json={"username": "xff-spray", "password": "nope-5"},
            headers={"X-Forwarded-For": "10.0.0.99"},
        )
        assert r.status_code == 429
    finally:
        auth_api.login_limiter = SlidingWindowLimiter(
            auth_api.settings.login_rate_limit_per_minute
        )


# --- enumeration side channels ------------------------------------------------------


def test_login_failure_responses_are_byte_identical(client, db_session):
    """Unknown user vs wrong password vs disabled account: same status, same
    body bytes, same content-length (b2.sh proved this live; pin it in the
    offline suite too)."""
    make_user(db_session, "enum-real")
    disabled = make_user(db_session, "enum-disabled")
    disabled.is_active = False
    db_session.commit()

    r_unknown = login(client, "ghost-user")
    r_wrong = login(client, "enum-real", "definitely-not-it")
    r_disabled = login(client, "enum-disabled", PASSWORD)

    for r in (r_wrong, r_disabled):
        assert r.status_code == r_unknown.status_code == 401
        assert r.content == r_unknown.content
        assert r.headers.get("content-length") == r_unknown.headers.get("content-length")
        assert r.headers.get("content-type") == r_unknown.headers.get("content-type")


# --- argon2 parameter floor + rehash-on-login ---------------------------------------


def test_argon2_parameters_meet_owasp_minimum():
    """Documented floor (review task 4): m ≥ 19 MiB (19456 KiB), t ≥ 2,
    p ≥ 1 — the OWASP Password Storage Cheat Sheet argon2id minimum. The
    app pins m=64 MiB, t=3, p=4 (well above); pin it so a dependency
    default change cannot silently weaken it."""
    probe = passwords._hasher.hash("parameter probe value")
    alg = probe.split("$")[1]
    param_seg = next(s for s in probe.split("$") if s.startswith("m="))
    parts = dict(kv.split("=") for kv in param_seg.split(","))
    m, t, p = int(parts["m"]), int(parts["t"]), int(parts["p"])
    assert alg == "argon2id"
    assert m >= 19456 and t >= 2 and p >= 1, (m, t, p)


def test_rehash_on_login_upgrades_legacy_hash(client, db_session):
    """needs_rehash exists and login() calls it — but nothing proved a
    legacy-parameter hash actually gets upgraded by logging in. Simulate a
    hash made with weaker (older) parameters and confirm the stored hash
    rolls forward and still verifies."""
    from argon2 import PasswordHasher

    make_user(db_session, "rehash-user")
    db_session.commit()
    user = db_session.execute(
        select(User).where(User.username == "rehash-user")
    ).scalar_one()

    legacy = PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1)
    legacy_hash = legacy.hash(PASSWORD)
    user.password_hash = legacy_hash
    db_session.commit()
    assert passwords.needs_rehash(legacy_hash)  # precondition

    r = login(client, "rehash-user")
    assert r.status_code == 200, r.text

    db_session.expire_all()
    upgraded = db_session.execute(
        select(User).where(User.username == "rehash-user")
    ).scalar_one()
    assert upgraded.password_hash != legacy_hash, "login did not upgrade the legacy hash"
    assert not passwords.needs_rehash(upgraded.password_hash)
    assert passwords.verify_password(PASSWORD, upgraded.password_hash)


# --- throttled-attempt audit content -------------------------------------------------


def test_throttled_login_audits_no_identifier(client, db_session):
    """A 429'd login writes login_throttled with the IP only — never the
    attempted username, not even hashed (§A6: unknown usernames leave no
    trace an attacker can mine for account existence)."""
    make_user(db_session, "throttle-audit")
    db_session.commit()
    for _ in range(5):
        login(client, "throttle-audit", "wrong-password-1")
    assert login(client, "throttle-audit", "wrong-password-2").status_code == 429

    events = db_session.execute(
        select(AuditEvent).where(AuditEvent.event_type == "login_throttled")
    ).scalars().all()
    assert events, "expected a login_throttled event"
    for ev in events:
        dump = "|".join(
            str(getattr(ev, c))
            for c in ("event_type", "object_type", "outcome", "route", "ip")
        ) + f"|{str(ev.details)}"
        assert "throttle-audit" not in dump
        assert ev.actor_user_id is None
