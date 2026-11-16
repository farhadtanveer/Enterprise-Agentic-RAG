"""A2 (docs/tasks/g2.md): personal upload quotas (§A14.6, DECISION #7).

Enforcement order and codes: per-file cap → 413; per-user total bytes →
409 ``quota_total``; per-user document count → 409 ``quota_docs``; hourly
per-user sliding window → 429 + Retry-After. ADMINS ARE EXEMPT from the
personal quotas (DECISION #7) — the global MAX_UPLOAD_MB still binds
everyone. The race-safety invariant (parallel uploads can never exceed the
total quota) is proven by a real multi-thread race against Postgres with a
widened decision window — same discipline as the H3 last-admin race.

Offline (no parse/Pinecone); files land under tmp_path only.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import auth as auth_api
from app.api import conversations as conv_api
from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app as fastapi_app
from app.models.document import Document
from app.services import users as users_svc
from app.services.ratelimit import SlidingWindowLimiter

PEER = ("203.0.113.11", 50000)
PASSWORD = "a valid longer passphrase"
MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    import app.api.documents as doc_api
    from langchain_core.documents import Document as LCDoc

    monkeypatch.setattr(auth_api, "login_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(conv_api, "chat_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(doc_api, "load_file",
                        lambda p: [LCDoc(page_content="x", metadata={"source": str(p)})])
    monkeypatch.setattr(doc_api, "add_documents", lambda chunks, **kw: ["id"])
    # the hourly limiter is built at import from the setting (same pattern
    # as the chat limiter) — tests replace the instance, never the setting;
    # the import-time wiring is pinned in tests/test_settings_audit.py
    monkeypatch.setattr(
        doc_api, "upload_limiter", SlidingWindowLimiter(10**9, window_seconds=3600.0)
    )
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))


@pytest.fixture()
def client(db_session):
    def _override():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(fastapi_app, client=PEER)
    finally:
        fastapi_app.dependency_overrides.clear()


def make(db, username, *, role="user", **kw):
    u = users_svc.create_user(db, username, password=PASSWORD, role=role, **kw)
    db.commit()
    return u


def login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def upload(client, csrf, *, name="q.txt", content=b"x", group_id=None):
    return client.post(
        "/api/documents", files={"file": (name, content, "text/plain")},
        data={"group_id": group_id} if group_id is not None else None,
        headers={"X-CSRF-Token": csrf},
    )


def test_per_file_cap_413_admin_exempt(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "user_upload_max_mb", 1)
    u = make(db_session, "cap-user")
    admin = make(db_session, "cap-admin", role="admin")
    big = b"a" * (2 * MB)

    csrf = login(client, "cap-user")
    r = upload(client, csrf, name="big.txt", content=big)
    assert r.status_code == 413, r.text
    assert db_session.execute(select(Document)).scalars().all() == []

    csrf = login(client, "cap-admin")
    r = upload(client, csrf, name="big.txt", content=big)  # DECISION #7: exempt
    assert r.status_code == 201, r.text


def test_total_quota_409(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "user_upload_total_mb", 1)
    make(db_session, "tot-user")
    csrf = login(client, "tot-user")

    r = upload(client, csrf, name="a.txt", content=b"a" * int(0.6 * MB))
    assert r.status_code == 201, r.text
    r = upload(client, csrf, name="b.txt", content=b"b" * int(0.6 * MB))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "quota_total"
    # the rejected upload left no row and no file
    rows = db_session.execute(select(Document)).scalars().all()
    assert len(rows) == 1


def test_doc_count_quota_409(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "user_upload_max_docs", 2)
    make(db_session, "cnt-user")
    csrf = login(client, "cnt-user")
    assert upload(client, csrf, name="1.txt").status_code == 201
    assert upload(client, csrf, name="2.txt").status_code == 201
    r = upload(client, csrf, name="3.txt")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "quota_docs"


def test_hourly_rate_429_with_retry_after_admin_exempt(client, db_session, monkeypatch):
    import app.api.documents as doc_api

    monkeypatch.setattr(doc_api, "upload_limiter",
                        SlidingWindowLimiter(2, window_seconds=3600.0))
    make(db_session, "rate-user")
    admin = make(db_session, "rate-admin", role="admin")
    csrf = login(client, "rate-user")
    assert upload(client, csrf).status_code == 201
    assert upload(client, csrf).status_code == 201
    r = upload(client, csrf)
    assert r.status_code == 429, r.text
    assert "retry-after" in {k.lower() for k in r.headers}, r.headers

    csrf = login(client, "rate-admin")
    for _ in range(3):  # admin is exempt from the personal window
        assert upload(client, csrf).status_code == 201


def test_deleting_documents_frees_the_doc_count_quota(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "user_upload_max_docs", 1)
    import app.api.documents as doc_api
    monkeypatch.setattr(doc_api, "delete_document_vectors", lambda i: None)
    u = make(db_session, "free-user")
    csrf = login(client, "free-user")
    r = upload(client, csrf, name="one.txt")
    assert r.status_code == 201, r.text
    doc_id = r.json()["document_id"]
    r = upload(client, csrf, name="two.txt")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "quota_docs"
    r = client.delete(f"/api/documents/{doc_id}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    assert upload(client, csrf, name="three.txt").status_code == 201
