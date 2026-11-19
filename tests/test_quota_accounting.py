"""A5.2 (docs/tasks/g2.md): quota accounting.

Failed and deleted documents must not keep consuming quota; stale pending
rows are swept. The quota SUM must count exactly the documents that hold
the user's storage: ``pending`` (in-flight — the race safety of A2 DEPENDS
on pending counting) and ``indexed`` — never ``failed`` and never deleted
(the row is gone). The admin near-quota enforcement view uses the SAME
accounting (a user pushed over the line only by failed rows is NOT
near-quota). Stale pending rows (a process died between TX1 and TX2) are
swept to 'failed' by the operator sweep; fresh pending rows are never
touched, and the staleness threshold is a REAL setting (read by production
code, its effect proven here).

Route-level accounting tests are offline: load_file/add_documents faked,
files land under tmp_path only.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from langchain_core.documents import Document as LCDoc
from sqlalchemy import select, update

from app.api import auth as auth_api
from app.api import conversations as conv_api
from app.api import documents as doc_api
from app.cli import cli
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app as fastapi_app
from app.models.audit_event import AuditEvent
from app.models.document import Document
from app.services import documents as docs_svc
from app.services import users as users_svc
from app.services.ingestion import ParseFailed
from app.services.ratelimit import SlidingWindowLimiter

PEER = ("203.0.113.15", 50000)
PASSWORD = "a valid longer passphrase"
MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_api, "login_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(conv_api, "chat_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(
        doc_api, "upload_limiter", SlidingWindowLimiter(10**9, window_seconds=3600.0)
    )
    monkeypatch.setattr(
        doc_api, "load_file",
        lambda p: [LCDoc(page_content="x", metadata={"source": str(p)})],
    )
    monkeypatch.setattr(doc_api, "add_documents", lambda chunks, **kw: ["id"])
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


@pytest.fixture()
def cli_runner(joined_session_factory, monkeypatch):
    monkeypatch.setattr("app.cli._session", joined_session_factory)
    return CliRunner()


def make(db, username, *, role="user", **kw):
    u = users_svc.create_user(db, username, password=PASSWORD, role=role, **kw)
    db.commit()
    return u


def login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def upload(client, csrf, *, name="q.txt", content=b"x"):
    return client.post(
        "/api/documents", files={"file": (name, content, "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )


def _row(db, user, *, size=1000, status="pending", age=None):
    """Direct row seed (route-independent); checksum unique per row."""
    doc = Document(
        scope="private", owner_id=user.id, display_name=f"{uuid4().hex}.txt",
        storage_path="", checksum=uuid4().hex, size_bytes=size, status=status,
    )
    db.add(doc)
    db.flush()
    if age is not None:
        db.execute(
            update(Document)
            .where(Document.id == doc.id)
            .values(created_at=datetime.now(timezone.utc) - age)
        )
        db.flush()
    return doc


# --- failed documents must not consume quota (RED first: they did) ------------


def test_failed_documents_do_not_consume_total_quota(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "user_upload_total_mb", 1)
    make(db_session, "acct-user")
    csrf = login(client, "acct-user")
    assert upload(client, csrf, name="a.txt", content=b"a" * int(0.6 * MB)).status_code == 201

    # a parse failure leaves a FAILED row (S2/B4 contract); 0.2MB fits the
    # remaining headroom so the failure happens at parse, not at the gate
    def failing_parse(path):
        raise ParseFailed("timeout")

    import app.api.documents as doc_mod

    monkeypatch.setattr(doc_mod, "load_file", failing_parse)
    r = upload(client, csrf, name="bad.txt", content=b"b" * int(0.2 * MB))
    assert r.status_code == 422, r.text
    monkeypatch.setattr(
        doc_mod, "load_file",
        lambda p: [LCDoc(page_content="x", metadata={})],
    )
    # the failed row's bytes must NOT count: 0.6 indexed + 0.35 new = 0.95MB
    # fits the 1MB quota; if the 0.2MB failed row still counted, 1.15MB
    # breaches and the upload would 409 (the old, wrong accounting)
    r = upload(client, csrf, name="c.txt", content=b"c" * int(0.35 * MB))
    assert r.status_code == 201, f"failed row still consumed quota — {r.status_code} {r.text}"


def test_failed_documents_do_not_consume_doc_count(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "user_upload_max_docs", 2)
    make(db_session, "acct-cnt")
    csrf = login(client, "acct-cnt")
    assert upload(client, csrf, name="1.txt").status_code == 201

    import app.api.documents as doc_mod

    monkeypatch.setattr(
        doc_mod, "load_file", lambda p: (_ for _ in ()).throw(ParseFailed("t"))
    )
    assert upload(client, csrf, name="2.txt").status_code == 422
    monkeypatch.setattr(
        doc_mod, "load_file", lambda p: [LCDoc(page_content="x", metadata={})]
    )
    r = upload(client, csrf, name="3.txt")
    assert r.status_code == 201, f"failed row still consumed the doc count — {r.text}"


# --- deleted documents must not consume quota (the byte axis) ------------------


def test_deleted_documents_free_the_total_quota(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "user_upload_total_mb", 1)
    monkeypatch.setattr(doc_api, "delete_document_vectors", lambda i: None)
    make(db_session, "del-bytes")
    csrf = login(client, "del-bytes")
    first = upload(client, csrf, name="one.txt", content=b"a" * int(0.6 * MB))
    assert first.status_code == 201, first.text
    assert upload(client, csrf, name="two.txt", content=b"b" * int(0.6 * MB)).status_code == 409
    r = client.delete(f"/api/documents/{first.json()['document_id']}",
                      headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    # the deleted row's bytes are freed: the same 0.6MB fits again
    assert upload(client, csrf, name="three.txt", content=b"c" * int(0.6 * MB)).status_code == 201


def test_inflight_pending_rows_still_count_race_safety_depends_on_it(db_session, monkeypatch):
    """The A2 race proof needs in-flight pending rows to count — a too-wide
    exclusion would reopen the oversubscription race. Pin the flip side."""
    monkeypatch.setattr(get_settings(), "user_upload_total_mb", 1)
    u = make(db_session, "pend-counts")
    _row(db_session, u, size=int(0.6 * MB), status="pending")
    db_session.commit()
    with pytest.raises(docs_svc.QuotaExceeded) as ei:
        docs_svc.enforce_private_quota(db_session, u.id, int(0.6 * MB))
    assert ei.value.kind == "total"


# --- the admin near-quota view uses the SAME accounting ------------------------


def test_near_quota_view_ignores_failed_rows(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "user_upload_total_mb", 1)
    make(db_session, "nq-admin", role="admin")
    owner = make(db_session, "nq-owner")
    # only FAILED bytes — 0.9MB of a 1MB quota would be "near" if failed rows counted
    _row(db_session, owner, size=int(0.9 * MB), status="failed")
    db_session.commit()
    csrf = login(client, "nq-admin")
    r = client.get("/api/admin/documents", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    assert r.json()["near_quota_users"] == [], (
        "failed rows pushed the user into the near-quota enforcement view"
    )
    # the flip side: indexed bytes at the same size DO list them
    _row(db_session, owner, size=int(0.9 * MB), status="indexed")
    db_session.commit()
    r = client.get("/api/admin/documents", headers={"X-CSRF-Token": csrf})
    listed = [u["user_id"] for u in r.json()["near_quota_users"]]
    assert listed == [str(owner.id)], r.text


# --- stale pending sweep --------------------------------------------------------


def test_sweep_marks_only_stale_pending_failed(db_session):
    u = make(db_session, "sweep-user")
    old_pending = _row(db_session, u, size=10, status="pending", age=timedelta(hours=2))
    fresh_pending = _row(db_session, u, size=10, status="pending")
    old_indexed = _row(db_session, u, size=10, status="indexed", age=timedelta(hours=2))
    old_failed = _row(db_session, u, size=10, status="failed", age=timedelta(hours=2))
    db_session.commit()

    swept = docs_svc.sweep_stale_pending(db_session)
    db_session.commit()
    assert swept == 1

    db_session.expire_all()
    assert db_session.get(Document, old_pending.id).status == "failed"
    assert db_session.get(Document, old_pending.id).error == "stale_pending"
    assert db_session.get(Document, fresh_pending.id).status == "pending"  # in-flight: untouched
    assert db_session.get(Document, old_indexed.id).status == "indexed"
    assert db_session.get(Document, old_failed.id).status == "failed"


def test_sweep_frees_quota_and_is_audited(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "user_upload_total_mb", 1)
    u = make(db_session, "sweep-quota")
    _row(db_session, u, size=int(0.6 * MB), status="pending", age=timedelta(hours=2))
    db_session.commit()
    with pytest.raises(docs_svc.QuotaExceeded):
        docs_svc.enforce_private_quota(db_session, u.id, int(0.6 * MB))
    db_session.rollback()

    assert docs_svc.sweep_stale_pending(db_session) == 1
    db_session.commit()
    docs_svc.enforce_private_quota(db_session, u.id, int(0.6 * MB))  # freed — no raise
    events = db_session.execute(
        select(AuditEvent).where(AuditEvent.event_type == "stale_pending_swept")
    ).scalars().all()
    assert len(events) == 1
    assert events[0].actor_user_id is None  # CLI sweep: no actor


def test_sweep_threshold_is_a_real_setting(db_session, monkeypatch):
    """No decorative settings: production code must READ
    user_upload_pending_stale_minutes — 0 minutes sweeps even a fresh row."""
    monkeypatch.setattr(get_settings(), "user_upload_pending_stale_minutes", 0)
    u = make(db_session, "sweep-zero")
    fresh = _row(db_session, u, size=10, status="pending")
    db_session.commit()
    assert docs_svc.sweep_stale_pending(db_session) == 1
    db_session.expire_all()
    assert db_session.get(Document, fresh.id).status == "failed"


def test_uploads_sweep_stale_cli(db_session, cli_runner):
    u = make(db_session, "sweep-cli")
    old = _row(db_session, u, size=10, status="pending", age=timedelta(hours=2))
    fresh = _row(db_session, u, size=10, status="pending")
    db_session.commit()

    result = cli_runner.invoke(cli, ["uploads-sweep-stale"])
    assert result.exit_code == 0, result.output
    assert "1" in result.output
    db_session.expire_all()
    assert db_session.get(Document, old.id).status == "failed"
    assert db_session.get(Document, fresh.id).status == "pending"
