"""A2 race proof (docs/tasks/g2.md): parallel uploads can never exceed the
per-user total quota — the enforcement serializes on the caller's locked
user row (SELECT ... FOR UPDATE) and re-reads committed state inside the
lock.

The race runs for real: production-style per-request DB sessions, four
threads, four 400 KB uploads against a 1 MB total quota, five rounds. The
decision window is widened TEST-SIDE (a monkeypatched wrapper that checks,
sleeps, and re-checks) so the window overlap is deterministic — exactly the
H3 discipline. With the lock, the second request blocks inside the wrapper,
then re-reads the first request's COMMITTED rows and answers 409. Without
the lock both requests compute against the same snapshot and the sum
oversubscribes (the mutant state this test exists to kill).
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models.document import Document
from app.services import documents as docs_service
from app.services import users as users_svc

PASSWORD = "a valid longer passphrase"
MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _fresh_upload_limiter(monkeypatch):
    import app.api.documents as doc_api
    from app.services.ratelimit import SlidingWindowLimiter

    monkeypatch.setattr(doc_api, "upload_limiter", SlidingWindowLimiter(10**9))


def test_parallel_uploads_never_exceed_the_total_quota(prod_client_factory, monkeypatch):
    from app.api import auth as auth_api
    from app.api import conversations as conv_api
    from langchain_core.documents import Document as LCDoc
    from app.services.ratelimit import SlidingWindowLimiter

    monkeypatch.setattr(auth_api, "login_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(conv_api, "chat_limiter", SlidingWindowLimiter(10**9))

    # offline: parse + vector writes faked (the race exercises the QUOTA
    # machinery, not Pinecone)
    import app.api.documents as doc_api

    monkeypatch.setattr(
        doc_api, "load_file",
        lambda p: [LCDoc(page_content="x", metadata={"source": str(p)})],
    )
    monkeypatch.setattr(doc_api, "add_documents", lambda chunks, **kw: ["id"])

    s = get_settings()
    monkeypatch.setattr(s, "user_upload_total_mb", 1)

    real_enforce = docs_service.enforce_private_quota

    def widened(db, user_id, incoming_size):
        real_enforce(db, user_id, 0)  # take the user-row lock, no-op check
        time.sleep(0.05)  # widen: both requests decide "simultaneously"
        real_enforce(db, user_id, incoming_size)

    monkeypatch.setattr(docs_service, "enforce_private_quota", widened)

    factory = prod_client_factory
    db = get_sessionmaker(factory["test_url"])()
    user = users_svc.create_user(db, "race-quota-user", password=PASSWORD)
    db.commit()
    db.close()
    factory["track"]["users"].append(user.id)

    setup = factory["client_for"]()
    r = setup.post("/api/auth/login",
                   json={"username": "race-quota-user", "password": PASSWORD})
    assert r.status_code == 200, r.text
    sid, csrf = r.cookies.get("sid"), r.cookies.get("csrf")

    content = b"a" * (400 * 1024)
    barrier = threading.Barrier(4)

    def fire(i):
        c = factory["client_for"](sid, csrf)
        barrier.wait(timeout=10)
        return c.post(
            "/api/documents",
            files={"file": (f"r{i}.txt", content, "text/plain")},
            headers={"X-CSRF-Token": csrf},
        )

    for round_no in range(5):
        with ThreadPoolExecutor(max_workers=4) as pool:
            rs = list(pool.map(fire, range(4)))
        codes = [r.status_code for r in rs]
        ok = [r for r in rs if r.status_code == 201]
        assert len(ok) >= 1, (round_no, codes, [r.text for r in rs])
        assert codes.count(409) == 4 - len(ok), (round_no, codes)

        dbe = get_sessionmaker(factory["test_url"])()
        try:
            total, count = dbe.execute(
                select(func.coalesce(func.sum(Document.size_bytes), 0), func.count())
                .select_from(Document)
                .where(Document.scope == "private", Document.owner_id == user.id)
            ).one()
            assert int(total) <= MB, (round_no, total, codes)
            assert count == len(ok), (round_no, count, len(ok))
        finally:
            dbe.close()
        # reset for the next round: remove exactly this round's doc rows
        dbe = get_sessionmaker(factory["test_url"])()
        try:
            for d in dbe.execute(
                select(Document).where(Document.owner_id == user.id)
            ).scalars().all():
                dbe.delete(d)
            dbe.commit()
        finally:
            dbe.close()
