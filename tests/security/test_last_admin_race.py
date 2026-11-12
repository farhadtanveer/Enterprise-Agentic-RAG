"""H3 (docs/tasks/g1.md PART 0): the last-admin guard under CONCURRENCY.

Two admins demote or disable EACH OTHER at the same moment, 20 times.
Invariant (§A5): zero active admins must never be the result — at least
one of the two racing requests must lose with 409 last_admin, and at
least one active admin must remain afterwards.

The race window is (active_admin_count … commit) inside PATCH
/api/admin/users/{id}. To make it deterministic instead of luck-based,
the test widens the window with a SEMANTICS-FREE delay inside
active_admin_count: both requests compute their count before either
commits, every attempt. Without row locking both see 2 active admins and
both proceed → 0 active admins (the bug this test exists to kill). With
SELECT … FOR UPDATE the second guard blocks until the first transaction
commits, re-reads the post-update state, and answers 409.

Unlike the rest of the admin suite, this test must NOT override get_db
with the shared fixture session — a race needs each concurrent request
to own its DB session/transaction. The override here yields a FRESH
session per request on the test engine (truncate-per-test still applies;
commits are real).
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db as app_get_db
from app.main import app as fastapi_app
from app.models.user import User
from app.services import users as users_svc

PEER = ("203.0.113.9", 50000)
PASSWORD = "a valid longer passphrase"


@pytest.fixture(autouse=True)
def _fresh_limiters(monkeypatch):
    """In-memory limiters are module singletons — 40 logins in this file
    would otherwise exhaust the 5/min login limiter (same fix as the B6
    admin tests)."""
    from app.api import auth as auth_api
    from app.api import conversations as conv_api
    from app.services.ratelimit import SlidingWindowLimiter

    monkeypatch.setattr(auth_api, "login_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(conv_api, "chat_limiter", SlidingWindowLimiter(10**9))


def _make(db, username):
    u = users_svc.create_user(db, username, password=PASSWORD, role="admin")
    db.commit()
    return u


def _login(client, username):
    r = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def _patch_other(client, csrf, target_id, payload, barrier):
    barrier.wait(timeout=10)  # both requests enter their guard together
    return client.patch(
        f"/api/admin/users/{target_id}",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )


@pytest.mark.parametrize("attempt", range(20))
def test_racing_admins_never_leave_zero_active(db_session, pg_engine, monkeypatch, attempt):
    real_count = users_svc.active_admin_count

    def widened_count(db):
        n = real_count(db)  # unchanged semantics — widens the window only
        time.sleep(0.05)
        return n

    monkeypatch.setattr(users_svc, "active_admin_count", widened_count)

    def _fresh_session_per_request():
        db = Session(bind=pg_engine, expire_on_commit=False)
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    fastapi_app.dependency_overrides[app_get_db] = _fresh_session_per_request
    try:
        name_a, name_b = f"race{attempt}a", f"race{attempt}b"
        _make(db_session, name_a)
        _make(db_session, name_b)
        uid_a = db_session.execute(
            select(User.id).where(User.username == name_a)
        ).scalar_one()
        uid_b = db_session.execute(
            select(User.id).where(User.username == name_b)
        ).scalar_one()

        client_a = TestClient(fastapi_app, client=PEER)
        client_b = TestClient(fastapi_app, client=PEER)
        csrf_a = _login(client_a, name_a)
        csrf_b = _login(client_b, name_b)

        # alternate the attack: demote (role) vs disable (is_active)
        payload_a = {"role": "user"} if attempt % 2 == 0 else {"is_active": False}
        payload_b = {"role": "user"} if attempt % 2 == 0 else {"is_active": False}

        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(
                _patch_other, client_a, csrf_a, str(uid_b), payload_a, barrier
            )
            f2 = pool.submit(
                _patch_other, client_b, csrf_b, str(uid_a), payload_b, barrier
            )
            r1, r2 = f1.result(timeout=30), f2.result(timeout=30)

        codes = [r1.status_code, r2.status_code]
        assert codes.count(409) >= 1, (
            f"attempt {attempt}: neither racing request was refused — "
            f"codes={codes} bodies=({r1.text!r}, {r2.text!r})"
        )
        # exactly ONE winner: the loser must be the 409, not a crash
        assert 200 in codes, f"attempt {attempt}: no request succeeded: {codes}"
        remaining = db_session.execute(
            select(User.id).where(User.role == "admin", User.is_active.is_(True))
        ).scalars().all()
        assert len(remaining) >= 1, (
            f"attempt {attempt}: ZERO active admins survived — "
            f"codes={codes} remaining={remaining}"
        )
    finally:
        fastapi_app.dependency_overrides.clear()
