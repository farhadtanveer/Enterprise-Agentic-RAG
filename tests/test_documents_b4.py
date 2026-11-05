"""B4: documents flow — ACL filter (§A7/§A11.5), doc_id-scoped chunk ids
(M2 follow-up), legacy ingest routing (D8), MAX_UPLOAD_MB (413), and
Postgres↔Pinecone delete consistency (§A9.1).

Pinecone is faked everywhere (offline suite); the live semantic check is
gated in tests/test_live_pinecone.py.
"""
import hashlib
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session as _ObserverSession

from app.api import auth as auth_api
from app.api import conversations as conv_api
from app.api import routes as legacy_routes
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app as fastapi_app
from app.models.audit_event import AuditEvent
from app.models.document import Document
from app.services import conversations as svc_conv
from app.services import documents as docs_service
from app.services import users
from app.services.retrieval import ORG_ONLY_FILTER, retrieval_filter_for
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


def make_user(db, username, password=PASSWORD):
    user = users.create_user(db, username, password=password)
    db.commit()
    return user


def login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200
    return r.json()["csrf_token"]


# --- ACL filters (§A7, review of §A11.5) ---------------------------------------


def test_filter_builders():
    class U:
        id = "01900000-0000-7000-8000-000000000001"

    assert retrieval_filter_for(U()) == {
        "read_access": {"$in": ["org", "01900000-0000-7000-8000-000000000001"]}
    }
    assert ORG_ONLY_FILTER == {"read_access": {"$in": ["org"]}}


def test_authenticated_chat_sends_user_specific_filter(client, db_session, monkeypatch):
    user = make_user(db_session, "acl-user")
    csrf = login(client, "acl-user")
    conv = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()
    captured = {}

    def fake_ask(question, retrieval_filter=None):
        captured["filter"] = retrieval_filter
        return {
            "answer": "ok", "source_used": "direct", "trace": [],
            "citations": [], "current_query": question, "token_usage": {},
        }

    monkeypatch.setattr(conv_api, "ask", fake_ask)
    r = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"question": "what is in the KB?"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert captured["filter"] == {"read_access": {"$in": ["org", str(user.id)]}}


def test_legacy_chat_sends_org_only_filter(client, db_session, monkeypatch):
    """D8: a static key proves no identity — legacy retrieval never sees
    private chunks (the filter can't match them)."""
    make_user(db_session, "legacy-acl")
    captured = {}

    def fake_ask(question, retrieval_filter=None):
        captured["filter"] = retrieval_filter
        return {"answer": "ok", "source_used": "direct", "trace": [], "citations": [], "current_query": question}

    monkeypatch.setattr(legacy_routes, "ask", fake_ask)
    r = client.post("/api/chat", json={"question": "any question at all"})
    assert r.status_code == 200
    assert captured["filter"] == ORG_ONLY_FILTER


def test_get_retriever_is_filter_required(monkeypatch):
    """§A7: no code path retrieves unfiltered — None must fail loudly."""
    from app.rag import vectorstore as vs

    with pytest.raises(ValueError, match="ACL filter"):
        vs.get_retriever(None)
    with pytest.raises(ValueError, match="ACL filter"):
        vs.get_retriever({})

    captured = {}

    class _FakeStore:
        def as_retriever(self, search_kwargs):
            captured["kwargs"] = search_kwargs
            return "retriever"

    monkeypatch.setattr(vs, "get_vectorstore", lambda: _FakeStore())
    retriever = vs.get_retriever({"read_access": {"$in": ["org"]}})
    assert retriever == "retriever"
    assert captured["kwargs"]["filter"] == {"read_access": {"$in": ["org"]}}


# --- chunk ids + metadata scheme (item 16, §A7) ---------------------------------


class _FakeIndex:
    def __init__(self):
        self.delete_filters = []

    def delete(self, filter, namespace):
        self.delete_filters.append((filter, namespace))


class _FakeStore:
    def __init__(self):
        self.index = _FakeIndex()
        self.added = []

    def add_documents(self, chunks, ids):
        self.added.append((list(chunks), list(ids)))
        return ids


def test_chunk_ids_are_doc_scoped():
    from app.rag.vectorstore import _chunk_id

    class C:
        def __init__(self, content, start="0"):
            self.page_content = content
            self.metadata = {"start_index": start}

    a = _chunk_id("doc-1", C("identical text"))
    b = _chunk_id("doc-2", C("identical text"))
    assert a != b  # same filename/text in two docs can never collide (M2)
    assert a == _chunk_id("doc-1", C("identical text"))  # deterministic upsert


def test_add_documents_attaches_acl_metadata_and_replaces_by_doc_id():
    from app.rag import vectorstore as vs

    store = _FakeStore()
    monkey_ws = patch.object(vs, "get_vectorstore", return_value=store)
    with monkey_ws:
        class C:
            def __init__(self, content):
                self.page_content = content
                self.metadata = {"start_index": "0", "source": "whatever"}

        vs.add_documents(
            [C("chunk one"), C("chunk two")],
            doc_id="11111111-1111-4111-8111-111111111111",
            scope="private", owner_id="22222222-2222-4222-8222-222222222222",
            source_name="roadmap.md",
        )
        vs.add_documents(
            [C("org chunk")],
            doc_id="33333333-3333-4333-8333-333333333333",
            scope="org", owner_id=None, source_name="policy.pdf",
        )

    # replace semantics target the doc_id, never a source string (M2)
    assert store.index.delete_filters[0][0] == {"doc_id": {"$eq": "11111111-1111-4111-8111-111111111111"}}
    assert store.index.delete_filters[1][0] == {"doc_id": {"$eq": "33333333-3333-4333-8333-333333333333"}}
    private_chunks, private_ids = store.added[0]
    for c in private_chunks:
        assert c.metadata["doc_id"] == "11111111-1111-4111-8111-111111111111"
        assert c.metadata["read_access"] == ["22222222-2222-4222-8222-222222222222"]
        assert c.metadata["source"] == "roadmap.md"
    org_chunks, org_ids = store.added[1]
    assert org_chunks[0].metadata["read_access"] == ["org"]
    assert org_chunks[0].metadata["owner_id"] is None  # D16: org docs ownerless
    # ids derive from doc_id + start + content
    expected = hashlib.sha256(
        "11111111-1111-4111-8111-111111111111\x000\x00chunk one".encode()
    ).hexdigest()
    assert private_ids[0] == expected


def test_fake_acl_store_semantics_org_vs_private():
    """§A11.5 semantics with a fake store: the $in filter decides per-vector
    via read_access membership — A never receives B's private chunk."""
    vectors = {
        "org-1": ["org"],
        "a-private": ["01900000-0000-7000-8000-00000000000a"],
        "b-private": ["01900000-0000-7000-8000-00000000000b"],
    }

    def query(filter):
        allowed = set(filter["read_access"]["$in"])
        return sorted(k for k, access in vectors.items() if allowed & set(access))

    assert query(retrieval_filter_for(type("U", (), {"id": "01900000-0000-7000-8000-00000000000a"})())) == [
        "a-private", "org-1"
    ]
    assert query(ORG_ONLY_FILTER) == ["org-1"]


# --- legacy ingest through the documents flow (item 15 + D8) ---------------------


POLICY_CONTENT = "VPN reset steps:\n1. portal\n"


def _ingest(client, tmp_path, name="policy.txt", content=POLICY_CONTENT):
    p = tmp_path / name
    p.write_text(content)
    with p.open("rb") as fh:
        return client.post(
            "/api/ingest",
            files={"file": (name, fh, "text/plain")},
            headers={"X-Admin-Key": "test-admin-key"},
        )


def test_legacy_ingest_creates_org_document(client, db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(legacy_routes.settings, "admin_api_key", "test-admin-key")
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(legacy_routes.settings, "upload_dir", str(uploads))

    class C:
        def __init__(self, content):
            self.page_content = content
            self.metadata = {"start_index": "0"}

    store = _FakeStore()
    with patch.object(legacy_routes, "add_documents", return_value=["id1"]):
        with patch.object(legacy_routes, "chunk_documents", return_value=[C("c1"), C("c2")]):
            with patch.object(legacy_routes, "load_file", return_value=[object()]):
                r = _ingest(client, tmp_path)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file"] == "policy.txt" and body["chunks"] == 2

    doc = db_session.execute(
        select(Document).where(Document.id == body["document_id"])
    ).scalar_one()
    assert doc.scope == "org" and doc.owner_id is None  # D16: ownerless
    assert doc.status == "indexed" and doc.chunk_count == 2
    assert Path(doc.storage_path).parent == uploads / str(doc.id)  # uploads/<doc_id>/
    assert doc.checksum == hashlib.sha256(POLICY_CONTENT.encode("utf-8")).hexdigest()
    ev = db_session.execute(
        select(AuditEvent).where(AuditEvent.event_type == "document_uploaded")
    ).scalar_one()
    assert ev.outcome == "ok" and ev.details["scope"] == "org"


def test_legacy_ingest_rejects_oversize_early_and_mid_stream(client, db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(legacy_routes.settings, "admin_api_key", "test-admin-key")
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(legacy_routes.settings, "upload_dir", str(uploads))
    monkeypatch.setattr(legacy_routes.settings, "max_upload_mb", 1)

    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (1024 * 1024 + 1))
    with big.open("rb") as fh:
        r = client.post(
            "/api/ingest",
            files={"file": ("big.txt", fh, "text/plain")},
            headers={"X-Admin-Key": "test-admin-key"},
        )
    assert r.status_code == 413
    # no document row, no leftover partial file
    assert db_session.execute(select(Document)).scalars().all() == []
    assert not any(uploads.iterdir()) if uploads.exists() else True


# --- deletion consistency (§A9.1, item 19) ---------------------------------------


def test_complete_delete_removes_row_and_files(db_session, tmp_path, monkeypatch):
    """TX2: after the vectors are gone, the row and the stored file go too."""
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    doc = docs_service.create_document(
        db_session, scope="org", owner_id=None, display_name="gone.txt",
        storage_path=str(tmp_path / "uploads" / "doc-1" / "gone.txt"),
        checksum="c" * 64, size_bytes=10, mime_suffix=".txt",
    )
    db_session.commit()
    doc_dir = tmp_path / "uploads" / "doc-1"
    doc_dir.mkdir(parents=True)
    (doc_dir / "gone.txt").write_text("content")

    docs_service.begin_delete(db_session, doc)
    db_session.commit()
    assert doc.status == "deleting"  # TX1: queryable, hidden from listings

    docs_service.complete_delete(db_session, doc.id)
    db_session.commit()
    assert docs_service.get_document(db_session, doc.id) is None
    assert not doc_dir.exists()  # uploads/<doc_id>/ removed with the row
    ev = db_session.execute(
        select(AuditEvent).where(AuditEvent.event_type == "document_deleted")
    ).scalar_one()
    assert ev.outcome == "ok"


def test_complete_delete_refuses_paths_outside_upload_dir(db_session, tmp_path, monkeypatch, caplog):
    """Defense-in-depth (2026-11-05 incident): a malformed storage_path whose
    parent is NOT the configured uploads/<doc_id>/ tree must never be
    rmtree'd — the row still goes, the foreign directory stays."""
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    foreign = tmp_path / "elsewhere" / "doc-x"
    foreign.mkdir(parents=True)
    (foreign / "f.txt").write_text("keep me")
    doc = docs_service.create_document(
        db_session, scope="org", owner_id=None, display_name="f.txt",
        storage_path=str(foreign / "f.txt"),
        checksum="d" * 64, size_bytes=6, mime_suffix=".txt",
    )
    db_session.commit()

    docs_service.complete_delete(db_session, doc.id)
    db_session.commit()
    assert docs_service.get_document(db_session, doc.id) is None  # row gone
    assert (foreign / "f.txt").exists()  # foreign file untouched
    assert "refusing to rmtree" in caplog.text


def test_sweep_vectors_before_rows_and_survives_outage(db_session, tmp_path, monkeypatch):
    """§A9.1 ordering + restartability: the vector delete runs while the row
    is still resumable; an outage leaves 'deleting'; the retry completes.
    TX1's commit is durable in production, so after the failure the row is
    observed through a SEPARATE session (the rollback-per-test fixture keeps
    everything inside one outer transaction — a rollback here would undo the
    row itself, a state production can never reach). The document gets a
    REAL storage path under the configured upload dir: complete_delete
    removes the file's directory, and anything else must never be deleted
    (see test_complete_delete_refuses_paths_outside_upload_dir)."""
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    storage_dir = tmp_path / "uploads" / "sweep-doc"
    storage_dir.mkdir(parents=True)
    storage_file = storage_dir / "outage.txt"
    storage_file.write_text("content")
    doc = docs_service.create_document(
        db_session, scope="org", owner_id=None, display_name="outage.txt",
        storage_path=str(storage_file), checksum="e" * 64, size_bytes=1, mime_suffix=".txt",
    )
    docs_service.begin_delete(db_session, doc)
    db_session.commit()

    observed = []
    attempts = []

    def first_call_fails(doc_id):
        attempts.append(doc_id)
        observed.append(docs_service.get_document(db_session, doc_id))
        if len(attempts) == 1:
            raise ConnectionError("pinecone unreachable")

    with pytest.raises(ConnectionError):
        docs_service.retry_stuck_deletes(db_session, first_call_fails)
    # the row is still 'deleting' — resumable, never half-deleted
    observer = _ObserverSession(bind=db_session.get_bind())
    try:
        stuck = docs_service.get_document(observer, doc.id)
        assert stuck is not None and stuck.status == "deleting"
    finally:
        observer.close()
    # and when the delete ran, the row was still there (vectors-first order)
    assert observed[0] is not None and observed[0].status == "deleting"

    completed = docs_service.retry_stuck_deletes(db_session, first_call_fails)
    db_session.commit()
    assert completed == 1 and len(attempts) == 2
    assert docs_service.get_document(db_session, doc.id) is None  # row gone AFTER vectors
    assert not storage_dir.exists()  # the stored file went with the row
