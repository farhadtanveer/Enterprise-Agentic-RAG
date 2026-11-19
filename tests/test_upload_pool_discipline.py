"""A5.1 (docs/tasks/g2.md): upload connection discipline.

The upload path must not hold a DB connection or transaction while parsing
or embedding (§A8): TX1 = quota check + insert the pending row + commit;
ingest with NO connection held; TX2 = mark indexed or failed. Proven with
the same pool probe as the chat TX-discipline test
(test_tx_discipline_pool_released_during_graph): ``checkedout()==0`` inside
a slow fake ingest, production-style session lifecycle.

The user (private/group) route already complied — its test pins the
existing behavior. The admin ORG upload held its transaction across
parse/embed (create_document flush + no commit until the end) and was
restructured to the same TX1/ingest/TX2 shape in this step (its test was
RED first and drives the fix).
"""
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document as LCDoc
from sqlalchemy import delete

from app.api import admin as admin_api
from app.api import auth as auth_api
from app.api import documents as doc_api
from app.core.config import get_settings
from app.core.db import get_db, get_engine, get_sessionmaker
from app.main import app as fastapi_app
from app.models.audit_event import AuditEvent
from app.models.document import Document
from app.models.user import User
from app.services import sessions as sessions_svc
from app.services import users as users_svc
from app.services.ratelimit import SlidingWindowLimiter

PEER = ("203.0.113.14", 50000)
PASSWORD = "a valid longer passphrase"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_api, "login_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(
        doc_api, "upload_limiter", SlidingWindowLimiter(10**9, window_seconds=3600.0)
    )
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))


def _production_style_db(test_url):
    """Same lifecycle as the real get_db: fresh short-lived session, caller
    commits, the connection returns to the pool at close (the pool probe
    must read ZERO while the fake ingest runs)."""

    def _override():
        db = get_sessionmaker(test_url)()
        try:
            yield db
        finally:
            db.close()

    return _override


def _setup_user(test_url, username, *, role="user"):
    db = get_sessionmaker(test_url)()
    try:
        user = users_svc.create_user(db, username, password=PASSWORD, role=role)
        db.commit()
        token, session_row = sessions_svc.create_session(db, user.id)
        db.commit()
        csrf = sessions_svc.csrf_token_for(session_row)
        return user, token, csrf
    finally:
        db.close()


def _slow_fake_ingest(test_url, seen):
    def fake_load_file(path):
        seen["checkedout"] = get_engine(test_url).pool.checkedout()
        return [LCDoc(page_content="x", metadata={"source": str(path)})]

    return fake_load_file


def _cleanup(test_url, *, user_id, doc_ids):
    """Committed rows must go (this test bypasses the truncate fixture)."""
    cleanup = get_sessionmaker(test_url)()
    try:
        cleanup.execute(delete(AuditEvent).where(AuditEvent.actor_user_id == user_id))
        for doc_id in doc_ids:
            cleanup.execute(delete(Document).where(Document.id == doc_id))
        cleanup.execute(delete(User).where(User.id == user_id))
        cleanup.commit()
    finally:
        cleanup.close()


def test_private_upload_releases_the_pool_during_ingest(monkeypatch):
    """TX1 commits before parse/embed: the pool probe inside a slow fake
    ingest reads ZERO checked-out connections (the user route already
    complied — this test pins it)."""
    test_url = os.environ["TEST_DATABASE_URL"]
    fastapi_app.dependency_overrides[get_db] = _production_style_db(test_url)
    try:
        user, token, csrf = _setup_user(test_url, f"pool-up-{uuid4().hex[:8]}")
        seen = {}
        monkeypatch.setattr(doc_api, "load_file", _slow_fake_ingest(test_url, seen))
        monkeypatch.setattr(doc_api, "add_documents", lambda chunks, **kw: ["id"])
        client = TestClient(fastapi_app, client=PEER)
        client.cookies.set("sid", token)
        client.cookies.set("csrf", csrf)
        r = client.post(
            "/api/documents",
            files={"file": ("pool.txt", b"upload pool discipline", "text/plain")},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 201, r.text
        assert seen["checkedout"] == 0, "a DB connection was held across parse/embed"
        _cleanup(test_url, user_id=user.id, doc_ids=[r.json()["document_id"]])
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)


def test_admin_org_upload_releases_the_pool_during_ingest(monkeypatch):
    """§A5.1 for the admin ORG upload (POST /api/admin/documents): the
    pending row must COMMIT (TX1) before parse/embed, and the connection
    must be back in the pool while they run (RED against the old
    flush-and-hold flow; drives this step's restructure)."""
    test_url = os.environ["TEST_DATABASE_URL"]
    fastapi_app.dependency_overrides[get_db] = _production_style_db(test_url)
    try:
        admin, token, csrf = _setup_user(
            test_url, f"pool-org-{uuid4().hex[:8]}", role="admin"
        )
        seen = {}
        monkeypatch.setattr(admin_api, "load_file", _slow_fake_ingest(test_url, seen))
        monkeypatch.setattr(admin_api, "add_documents", lambda chunks, **kw: ["id"])
        client = TestClient(fastapi_app, client=PEER)
        client.cookies.set("sid", token)
        client.cookies.set("csrf", csrf)
        r = client.post(
            "/api/admin/documents",
            files={"file": ("pool-org.txt", b"admin org pool discipline", "text/plain")},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 201, r.text
        assert seen["checkedout"] == 0, (
            "the admin org upload holds a DB connection across parse/embed"
        )
        _cleanup(test_url, user_id=admin.id, doc_ids=[r.json()["document_id"]])
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
