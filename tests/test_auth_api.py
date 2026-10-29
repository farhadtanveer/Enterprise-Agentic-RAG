"""B2: the auth API matrix — login/logout/me/password (§A8, §A11 tests
4/6/8/9/11/13 + B2 requirements 1–5, 7).

Run against real Postgres through the conftest rollback-per-test fixtures;
TestClient speaks plain http, so COOKIE_SECURE is forced false by conftest
and cookie-flag VALUES are asserted from raw Set-Cookie headers.
"""
from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import auth as auth_api
from app.api.deps import get_current_user
from app.core.db import get_db
from app.main import app as fastapi_app
from app.models.audit_event import AuditEvent
from app.models.session import Session as SessionRow
from app.services import passwords, sessions, users

# A real (TEST-NET-3) IP: TestClient's default peer "testclient" is not a
# valid inet value, and throttle/audit/session rows bind real IPs.
PEER = ("203.0.113.5", 50000)
PASSWORD = "a valid longer passphrase"


@pytest.fixture(autouse=True)
def _fresh_login_limiter(monkeypatch):
    """Module-level limiter state must not leak between tests."""
    from app.services.ratelimit import SlidingWindowLimiter

    monkeypatch.setattr(auth_api, "login_limiter", SlidingWindowLimiter(10**9))


@pytest.fixture()
def client(db_session):
    def _override():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override
    yield TestClient(fastapi_app, client=PEER)
    fastapi_app.dependency_overrides.clear()


def make_user(db, username="alice", password=PASSWORD, role="user", **kwargs):
    user = users.create_user(
        db, username, password=password, role=role, must_change_password=kwargs.pop(
            "must_change_password", False
        )
    )
    db.commit()
    return user


def login(client, username="alice", password=PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def set_cookies_of(response):
    return response.headers.get_list("set-cookie")


def events_of(db, event_type):
    return db.execute(
        select(AuditEvent).where(AuditEvent.event_type == event_type)
    ).scalars().all()


# --- login ------------------------------------------------------------------


def test_login_success_sets_cookies_body_and_no_store(client, db_session):
    make_user(db_session)
    r = login(client)
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["username"] == "alice"
    assert body["user"]["role"] == "user"
    assert body["csrf_token"]

    sid = [c for c in set_cookies_of(r) if c.startswith("sid=")][0]
    csrf = [c for c in set_cookies_of(r) if c.startswith("csrf=")][0]
    assert "HttpOnly" in sid  # B2 requirement 5
    assert "SameSite=lax" in sid
    assert "Path=/" in sid
    assert "Max-Age=2592000" in sid  # absolute cap 30 d (D7)
    assert "Secure" not in sid  # COOKIE_SECURE=false in tests (conftest)
    assert "HttpOnly" not in csrf  # JS must read it (double-submit)
    assert "SameSite=lax" in csrf
    assert r.headers["cache-control"] == "no-store"  # B2 requirement 4


def test_login_normalizes_username_case(client, db_session):
    make_user(db_session)
    assert login(client, "ALICE").status_code == 200
    assert login(client, "  alice ").status_code == 200


def test_secure_flag_follows_cookie_secure_setting(client, db_session, monkeypatch):
    monkeypatch.setattr(auth_api.settings, "cookie_secure", True)
    make_user(db_session)
    r = login(client)
    sid = [c for c in set_cookies_of(r) if c.startswith("sid=")][0]
    assert "Secure" in sid  # flag present when the setting says so


def test_unknown_user_generic_401_and_dummy_verify_invoked(client, db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(passwords, "verify_dummy", lambda pw: calls.append(pw) or False)
    r = login(client, "ghost-user", "whatever password 42")
    assert r.status_code == 401
    assert r.json()["detail"] == passwords.GENERIC_LOGIN_FAILED
    assert calls == ["whatever password 42"]  # B2 requirement 1: timing equalized


def test_wrong_password_generic_401_without_dummy_verify(client, db_session, monkeypatch):
    make_user(db_session)
    calls = []
    monkeypatch.setattr(passwords, "verify_dummy", lambda pw: calls.append(pw) or False)
    r = login(client, password="not the right password")
    assert r.status_code == 401
    assert r.json()["detail"] == passwords.GENERIC_LOGIN_FAILED  # same text as unknown
    assert calls == []  # real verify path — dummy is only for missing users


def test_failed_login_known_user_audits_actor_only(client, db_session):
    user = make_user(db_session, "audit-me")
    login(client, "audit-me", "wrong password 123")
    events = events_of(db_session, "login_failed")
    assert len(events) == 1
    ev = events[0]
    assert ev.actor_user_id == user.id  # §A11.13: actor id, never the username
    assert ev.outcome == "denied"
    assert str(ev.ip) == PEER[0]
    dump = f"{ev.event_type}|{ev.route}|{ev.object_type}|{ev.object_id}|{str(ev.details)}"
    assert "audit-me" not in dump  # the attempted username appears nowhere


def test_failed_login_unknown_user_audits_nothing_at_all(client, db_session):
    """§A11.13 / B2 requirement 6: no identifier for unknown accounts —
    not even the typed string."""
    login(client, "secret-unknown-user", "whatever password 42")
    events = events_of(db_session, "login_failed")
    assert len(events) == 1
    ev = events[0]
    assert ev.actor_user_id is None
    assert ev.details == {}
    dump = f"{ev.event_type}|{ev.route}|{ev.object_type}|{ev.object_id}|{str(ev.details)}|{str(ev.ip)}"
    assert "secret-unknown-user" not in dump


def test_successful_login_audits_and_resets_throttle(client, db_session):
    user = make_user(db_session)
    login(client, password="wrong password 123")
    r = login(client)  # success resets the counter (§A5)
    assert r.status_code == 200
    login_types = [e.event_type for e in events_of(db_session, "login")]
    assert login_types == ["login"]
    assert login(client, password="wrong password 123").status_code == 401  # counter is clean


# --- lockout (§A11 test 8) ---------------------------------------------------


def test_lockout_429_with_retry_after_then_unlock_recovers(client, db_session):
    make_user(db_session, "locky", "the right password 42")
    make_user(db_session, "other", "another passphrase xx")
    for _ in range(5):
        assert login(client, "locky", "wrong password 123").status_code == 401
    # 6th attempt — even with the CORRECT password — is locked out
    r = login(client, "locky", "the right password 42")
    assert r.status_code == 429
    assert 890 <= int(r.headers["Retry-After"]) <= 900  # ~15 min (§A5)
    assert [e.event_type for e in events_of(db_session, "login_throttled")] == ["login_throttled"]
    # same IP + different username is NOT throttled (no shared-IP freeze)
    assert login(client, "other", "another passphrase xx").status_code == 200
    # operator recovery: unlock CLI core clears it
    users.unlock_username(db_session, "locky")
    db_session.commit()
    assert login(client, "locky", "the right password 42").status_code == 200


def test_per_ip_login_rate_limit_429(client, db_session, monkeypatch):
    from app.services.ratelimit import SlidingWindowLimiter

    monkeypatch.setattr(auth_api, "login_limiter", SlidingWindowLimiter(3))
    make_user(db_session)
    for _ in range(3):
        assert login(client, password="wrong password 123").status_code == 401
    r = login(client)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "60"


# --- CSRF + logout (§A11 test 11) ---------------------------------------------


def test_csrf_rejection_and_server_side_logout(client, db_session):
    make_user(db_session)
    login(client)
    sid_token = client.cookies.get("sid")

    r = client.post("/api/auth/logout")  # no header
    assert r.status_code == 403
    assert r.json() == {"code": "csrf_failed"}
    r = client.post("/api/auth/logout", headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403

    csrf = client.cookies.get("csrf")
    r = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"

    # replay the OLD cookie: server-side revocation must 401 it (not just a
    # cleared cookie jar)
    client.cookies.set("sid", sid_token)
    assert client.get("/api/me").status_code == 401


def test_csrf_applies_to_password_endpoint_too(client, db_session):
    make_user(db_session)
    login(client)
    r = client.post("/api/auth/password", json={"old_password": PASSWORD, "new_password": "x"})
    assert r.status_code == 403
    assert r.json() == {"code": "csrf_failed"}


def test_get_requests_do_not_need_csrf(client, db_session):
    make_user(db_session)
    login(client)
    assert client.get("/api/me").status_code == 200  # GET is not mutating


# --- /api/me + session lifecycle ----------------------------------------------


def test_me_payload_and_no_store(client, db_session):
    make_user(db_session, display_name=None)
    login(client)
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "alice"
    assert r.json()["user"]["must_change_password"] is False
    assert r.headers["cache-control"] == "no-store"


def test_disabled_user_dies_everywhere_immediately(client, db_session):
    user = make_user(db_session, "disabled")
    assert login(client, "disabled").status_code == 200
    user.is_active = False
    db_session.commit()
    # live cookie → 401 on the very next request (§A5)
    assert client.get("/api/me").status_code == 401
    # correct password login → same generic 401, no disabled-account hint
    r = login(client, "disabled")
    assert r.status_code == 401
    assert r.json()["detail"] == passwords.GENERIC_LOGIN_FAILED


def test_sliding_renewal_happens_on_authenticated_requests(client, db_session):
    make_user(db_session)
    login(client)
    row = db_session.execute(select(SessionRow)).scalar_one()
    row.idle_expires_at = sessions.utcnow() + timedelta(hours=168) - timedelta(minutes=20)
    db_session.commit()

    assert client.get("/api/me").status_code == 200
    db_session.expire_all()
    row = db_session.get(SessionRow, row.id)
    # renewed to a full idle window from ~now (throttled-write path, ≥10 min)
    assert row.idle_expires_at > sessions.utcnow() + timedelta(hours=167)


def test_unauthenticated_requests_401_on_every_session_endpoint(client, db_session):
    assert client.request("GET", "/api/me").status_code == 401
    assert client.request("POST", "/api/auth/logout", json={}).status_code == 401
    assert client.request("POST", "/api/auth/password", json={}).status_code == 401
    assert client.get("/api/health").status_code == 200  # still open (§A11.4)
    assert login(client, "nobody", "whatever password 42").status_code == 401  # login open


# --- forced password change (§A5 gate; B2 requirements 6/8) --------------------


def _probe_client(db_session):
    """A throwaway app with a non-exempt session-authed route: B2 ships only
    the three exempt endpoints, but the GATE lives in the shared dependency
    and must be proven against a non-exempt path (§A5)."""
    from app.api.deps import GateDenied, gate_denied_handler

    probe = FastAPI()
    probe.add_exception_handler(GateDenied, gate_denied_handler)

    @probe.get("/api/anything")
    def anything(user=Depends(get_current_user)):
        return {"ok": True}

    def _override():
        yield db_session

    probe.dependency_overrides[get_db] = _override
    return TestClient(probe, client=PEER)


def test_forced_change_gate_and_full_flow(client, db_session):
    make_user(db_session, "temp", "temp-passphrase-1", must_change_password=True)
    r = login(client, "temp", "temp-passphrase-1")
    assert r.status_code == 200
    assert r.json()["user"]["must_change_password"] is True

    # exempt endpoints still work
    assert client.get("/api/me").status_code == 200

    # the probe client shares the session cookies (fresh jar otherwise)
    probe = _probe_client(db_session)
    for name in ("sid", "csrf"):
        probe.cookies.set(name, client.cookies.get(name))
    pr = probe.get("/api/anything")
    assert pr.status_code == 403
    assert pr.json() == {"code": "password_change_required"}

    # the change itself is exempt and needs CSRF
    csrf = client.cookies.get("csrf")
    r = client.post(
        "/api/auth/password",
        json={"old_password": "temp-passphrase-1", "new_password": "brand new passphrase 2"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["user"]["must_change_password"] is False
    assert r.headers["cache-control"] == "no-store"

    # gate lifts immediately
    assert probe.get("/api/anything").status_code == 200


def _switch_sid(client, token):
    """Swap the sid cookie. Delete first: a leftover host-only cookie from
    the login response would otherwise ride along in the header, and which
    duplicate value Starlette's cookie dict keeps is ordering-dependent."""
    client.cookies.delete("sid")
    client.cookies.set("sid", token)


def test_password_change_revokes_other_sessions_keeps_current(client, db_session):
    user = make_user(db_session, "multi")
    login(client, "multi")  # session 1 in the jar
    sid1 = client.cookies.get("sid")
    csrf1 = client.cookies.get("csrf")
    token2 = sessions.create_session(db_session, user.id)
    db_session.commit()
    _switch_sid(client, token2)
    assert client.get("/api/me").status_code == 200  # session 2 live

    _switch_sid(client, sid1)
    r = client.post(
        "/api/auth/password",
        json={"old_password": PASSWORD, "new_password": "next passphrase here"},
        headers={"X-CSRF-Token": csrf1},
    )
    assert r.status_code == 200

    _switch_sid(client, token2)
    assert client.get("/api/me").status_code == 401  # other session revoked (B2 req 2)
    _switch_sid(client, sid1)
    assert client.get("/api/me").status_code == 200  # current session survives (§A5)


def test_password_change_validation(client, db_session):
    make_user(db_session, "picky")
    login(client, "picky")
    csrf = client.cookies.get("csrf")
    headers = {"X-CSRF-Token": csrf}

    def change(new_password, old_password=PASSWORD):
        return client.post(
            "/api/auth/password",
            json={"old_password": old_password, "new_password": new_password},
            headers=headers,
        )

    r = change("acceptable passphrase 7", old_password="wrong old password!")
    assert r.status_code == 400
    r = change("picky")  # username as password (B2 requirement 3)
    assert r.status_code == 422
    assert "username" in r.json()["detail"]
    r = change("short11char")  # 11 chars <12
    assert r.status_code == 422
    r = change("x" * 129)  # >128
    assert r.status_code == 422
    r = change("acceptable passphrase 7")
    assert r.status_code == 200


# --- legacy surface untouched (B2 requirement 7 / D8) --------------------------


def test_legacy_endpoints_still_registered(client):
    # both legacy routes answer (422 = body validation, NOT 404/405): the
    # static-key contract is unchanged; behavior covered by existing modules
    # (tests/test_chat_auth.py, tests/test_routes_chat.py, test_admin_key.py)
    assert client.post("/api/chat", json={"question": ""}).status_code == 422
    assert client.post("/api/ingest").status_code == 422
