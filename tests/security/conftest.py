"""Fixtures for the permanent security-regression suite (tests/security/).

Origin: written by an independent reviewer against backend/multi-user
(review commit a41228c, findings fixed in a7d3201..45e1aac) and promoted
into the permanent suite in F8. House rules: offline (no API keys), no
real Pinecone, no destructive directory operations (single files under
tmp_path only).

Two client flavors:
- ``client``     — joins the caller's rollback-per-test ``db_session``
                   (same pattern as tests/test_conversations_api.py).
- ``prod_client``— production-style get_db (fresh session per request,
                   real commits against TEST_DATABASE_URL) for tests that
                   exercise cross-request concurrency; cleans up its rows.
"""
import pytest
from fastapi.testclient import TestClient

from app.api import auth as auth_api
from app.api import conversations as conv_api
from app.core.db import get_db
from app.main import app as fastapi_app
from app.services import users
from app.services.ratelimit import SlidingWindowLimiter

PEER = ("203.0.113.5", 50000)
PASSWORD = "a valid longer passphrase"


@pytest.fixture(autouse=True)
def _fresh_limiters(monkeypatch):
    monkeypatch.setattr(conv_api, "chat_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(auth_api, "login_limiter", SlidingWindowLimiter(10**9))


@pytest.fixture()
def client(db_session):
    def _override():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override
    yield TestClient(fastapi_app, client=PEER)
    fastapi_app.dependency_overrides.clear()


@pytest.fixture()
def prod_client_factory(monkeypatch):
    """Production-style sessions for concurrency tests.

    Every request gets its own committed-against-Postgres session (same
    lifecycle as the real get_db), so two TestClients in two threads race
    through the real TX1/FOR UPDATE/409 machinery. Rows are cleaned up.
    """
    import os

    test_url = os.environ["TEST_DATABASE_URL"]
    from app.core.db import get_sessionmaker

    def _production_style_db():
        db = get_sessionmaker(test_url)()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = _production_style_db

    track = {"users": [], "extra": []}

    def make_user(db, username):
        user = users.create_user(db, username, password=PASSWORD)
        db.commit()
        track["users"].append(user.id)
        return user

    def client_for(cookie_token=None, csrf=None, *, raise_server_exceptions=True):
        c = TestClient(
            fastapi_app, client=PEER, raise_server_exceptions=raise_server_exceptions
        )
        if cookie_token:
            c.cookies.set("sid", cookie_token)
            c.cookies.set("csrf", csrf)
        return c

    yield {
        "client_for": client_for,
        "make_user": make_user,
        "track": track,
        "test_url": test_url,
    }

    fastapi_app.dependency_overrides.pop(get_db, None)

    # cleanup: the rows these tests committed must go
    from sqlalchemy import delete

    from app.models.audit_event import AuditEvent
    from app.models.conversation import Conversation
    from app.models.login_throttle import LoginThrottle
    from app.models.session import Session as SessionRow
    from app.models.user import User

    db = get_sessionmaker(test_url)()
    try:
        for uid in track["users"]:
            # messages + feedback follow conversations via ON DELETE CASCADE
            db.execute(delete(Conversation).where(Conversation.user_id == uid))
            db.execute(delete(SessionRow).where(SessionRow.user_id == uid))
            db.execute(delete(LoginThrottle).where(LoginThrottle.ip == PEER[0]))
            db.execute(delete(AuditEvent).where(AuditEvent.actor_user_id == uid))
            db.execute(delete(User).where(User.id == uid))
        db.commit()
    finally:
        db.close()
