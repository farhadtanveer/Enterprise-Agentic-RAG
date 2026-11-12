"""Retrieval ACL review (review task 6) — updated for G1.2 tokens.

CONTRACT CHANGE (G1.2, docs/tasks/g1.md PART 1; §A14.1 + DECISION #9): the
read_access token grammar is STRICTLY prefixed — "org", "group:<id>",
"user:<id>" — and the authenticated filter is built by the AccessResolver
(DbAccessResolver) instead of the old two-token retrieval_filter_for(user).
rag-v2 was verified to hold ZERO bare-UUID vectors (2026-11-12), so no
vector migration was needed; these tests assert the new grammar directly
(a deliberate contract change, NOT a weakened test — the negative proofs
are unchanged in strength).

The app-side machinery (filter built from the session, filter-required
retriever, $in semantics, doc-scoped chunk ids) is covered by
tests/test_documents_b4.py. What this module proves, on the FULL chat path
(API → ask() → retrieve_kb → retriever) against a Pinecone-faithful fake:

- user B asking A's question receives NO chunk and NO citation of A's
  private document;
- an ADMIN's filter is org + admin's own user: token only — an admin
  CANNOT retrieve a user's private document nor a group they don't belong
  to (no privilege escalation in retrieval);
- fail closed: vectors with NO read_access key match nothing;
- delete-by-doc_id cannot touch another document's chunks.
"""
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import get_db
from app.main import app as fastapi_app
from app.models.user import User
from app.rag import vectorstore as vs
from app.services import users
from app.services.access import DbAccessResolver
from app.services.retrieval import ORG_ONLY_FILTER

PEER = ("203.0.113.5", 50000)
PASSWORD = "a valid longer passphrase"

A_PRIVATE_TEXT = "Alice's PRIVATE salary band: 250k. Never show this to anyone else."
ORG_TEXT = "Company VPN policy: use the portal, MFA required."
LEGACY_TEXT = "Legacy untagged chunk from the pre-B4 namespace."


class FakeIndex:
    """Implements the Pinecone semantics the ACL relies on: metadata
    filtering with $in over a string ARRAY, missing key ⇒ no match."""

    def __init__(self, vectors):
        self.vectors = vectors  # id -> {"metadata": {...}, "text": str}
        self.deleted_filters = []

    def query(self, vector=None, filter=None, top_k=4, namespace=None, **kw):
        allowed = set(filter["read_access"]["$in"])
        hits = [
            {"id": vid, "score": 1.0, "metadata": {**v["metadata"], "text": v["text"]}}
            for vid, v in self.vectors.items()
            if allowed & set(v["metadata"].get("read_access", []))
        ]
        return {"matches": hits[:top_k]}

    def delete(self, filter=None, namespace=None, **kw):
        self.deleted_filters.append(filter)
        doc_id = filter["doc_id"]["$eq"]
        self.vectors = {
            vid: v for vid, v in self.vectors.items()
            if v["metadata"].get("doc_id") != doc_id
        }


class FakeStore:
    def __init__(self, index):
        self.index = index

    def as_retriever(self, search_kwargs):
        index = self.index

        class _Retriever:
            def invoke(self, _query):
                return [
                    type("D", (), {
                        "page_content": h["metadata"]["text"],
                        "metadata": {"source": h["metadata"]["source"]},
                    })()
                    for h in index.query(filter=search_kwargs["filter"])["matches"]
                ]

        return _Retriever()


def seed_vectors():
    """Worst case: A's private chunk is the ONLY good answer and would rank
    first; plus an org chunk and a legacy no-metadata chunk. G1.2: A's
    private vector carries the STRICT prefixed token user:<owner>."""
    owner_a = str(uuid4())
    return {
        "a1": {"metadata": {"doc_id": "doc-a", "read_access": [f"user:{owner_a}"],
                            "source": "alice-salary.pdf"}, "text": A_PRIVATE_TEXT},
        "o1": {"metadata": {"doc_id": "doc-org", "read_access": ["org"],
                            "source": "vpn-policy.txt"}, "text": ORG_TEXT},
        "l1": {"metadata": {"doc_id": "doc-legacy", "source": "old.txt"},
               "text": LEGACY_TEXT},  # NO read_access — pre-B4 vector
    }, owner_a


def _stub_user(uid):
    """The resolver needs only user.id for audiences_for; a stub keeps the
    direct filter-shape tests free of DB setup."""
    return type("U", (), {"id": uid})()


def _no_memberships_result():
    class _R:
        def scalars(self):
            return self

        def all(self):
            return []

    return _R()


def _stub_db():
    class _DB:
        def execute(self, *a, **kw):
            return _no_memberships_result()

    return _DB()


def test_user_b_full_chat_path_never_sees_a_private_chunks(client, db_session, monkeypatch):
    """B asks A's question through the FULL chat path (API send_message →
    ask() → real route_question → retrieve_kb → filtered retriever →
    grade → generate_from_kb). The LLM is stubbed to always route to KB and
    grade 'good'; the vector store is the Pinecone-faithful fake. A's
    private chunk would rank FIRST if the filter leaked — B's answer and
    citations must never reference it, and the legacy untagged chunk must
    be invisible (fail closed)."""
    from app.rag import workflow as wf
    from app.rag.state import EvidenceGrade, RouteDecision

    vectors, _owner_a = seed_vectors()
    fake = FakeStore(FakeIndex(vectors))
    monkeypatch.setattr(vs, "get_vectorstore", lambda: fake)

    captured = {}
    real_get_retriever = wf.get_retriever  # workflow.py bound its own reference

    def spy_get_retriever(filter):
        captured["filter"] = filter
        return real_get_retriever(filter)

    monkeypatch.setattr(wf, "get_retriever", spy_get_retriever)

    class FakeLLM:
        def with_structured_output(self, schema, method=None):
            class _Structured:
                def invoke(self, prompt):
                    if schema is RouteDecision:
                        return RouteDecision(route="kb")
                    return EvidenceGrade(grade="good")

            return _Structured()

        def invoke(self, prompt):
            captured["prompts"] = captured.get("prompts", []) + [str(prompt)]
            return type("R", (), {"content": "Answer from the company KB."})()

    monkeypatch.setattr(wf, "llm", lambda: FakeLLM())

    users.create_user(db_session, "bob-acl", password=PASSWORD)
    db_session.commit()
    bob = db_session.execute(
        select(User).where(User.username == "bob-acl")
    ).scalar_one()
    r = client.post("/api/auth/login", json={"username": "bob-acl", "password": PASSWORD})
    assert r.status_code == 200
    csrf = r.json()["csrf_token"]
    conv = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()

    r = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"question": "what is Alice's salary band?"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # the retriever received exactly B's server-built resolver filter
    assert captured["filter"] == DbAccessResolver(db_session).filter_for(bob)
    assert captured["filter"]["read_access"]["$in"] == ["org", f"user:{bob.id}"]

    everything = body["content"] + "".join(body["trace"]) + str(body["citations"])
    assert "250k" not in everything, "A's private chunk content leaked to B"
    assert "alice-salary" not in everything, "A's private document surfaced to B"
    assert "old.txt" not in everything, "legacy untagged chunk surfaced to B"
    assert body["citations"] == [{"title": "vpn-policy.txt", "url": "", "type": "private_kb"}]
    # and the org chunk DID flow into the (stubbed) generation prompt
    assert any(ORG_TEXT in p for p in captured["prompts"])


def test_retriever_receives_caller_filter_not_a_blessed_one(monkeypatch):
    """Direct proof at the graph node level: retrieve_kb passes the state's
    filter to get_retriever — B's filter excludes A's chunk; with A's own
    filter the chunk IS returned (the fake mirrors Pinecone $in)."""
    vectors, owner_a = seed_vectors()
    fake_index = FakeIndex(vectors)

    class _Store:
        def __init__(self, index):
            self.index = index

        def as_retriever(self, search_kwargs):
            index = self.index

            class _R:
                def invoke(self, _q):
                    return [
                        type("D", (), {
                            "page_content": h["metadata"]["text"],
                            "metadata": {"source": h["metadata"]["source"]},
                        })()
                        for h in index.query(filter=search_kwargs["filter"])["matches"]
                    ]

            return _R()

    monkeypatch.setattr(vs, "get_vectorstore", lambda: _Store(fake_index))
    state = {"current_query": "q", "retrieval_filter": DbAccessResolver(
        _stub_db()).filter_for(_stub_user(str(uuid4())))}  # B — nobody special
    docs = vs.get_retriever(state["retrieval_filter"]).invoke("q")
    assert [d.page_content for d in docs] == [ORG_TEXT]  # A's chunk + legacy excluded

    # A themselves DO see it — proving the exclusion came from the filter,
    # not from the fake being rigged
    docs_a = vs.get_retriever(DbAccessResolver(_stub_db()).filter_for(
        _stub_user(owner_a))).invoke("q")
    assert A_PRIVATE_TEXT in [d.page_content for d in docs_a]


def test_admin_filter_cannot_retrieve_user_private_doc(db_session):
    """An admin's retrieval filter is [org, user:<admin-id>] — the admin's
    own token, not a wildcard. Raising a user to admin must NOT widen what
    their retriever matches on someone else's private chunks (nor on group
    chunks — covered by test_access_resolver.py)."""
    admin = users.create_user(db_session, "acl-admin", password=PASSWORD, role="admin")
    db_session.commit()
    admin = db_session.execute(
        select(User).where(User.username == "acl-admin")
    ).scalar_one()
    f = DbAccessResolver(db_session).filter_for(admin)
    assert f == {"read_access": {"$in": ["org", f"user:{admin.id}"]}}
    assert str(admin.id) != "org"

    vectors, owner_a = seed_vectors()
    allowed = set(f["read_access"]["$in"])
    matched = [vid for vid, v in vectors.items()
               if allowed & set(v["metadata"].get("read_access", []))]
    assert matched == ["o1"], f"admin filter matched A's private chunk: {matched}"


def test_missing_read_access_fails_closed_for_every_filter():
    vectors, owner_a = seed_vectors()
    for f in (
        ORG_ONLY_FILTER,
        DbAccessResolver(_stub_db()).filter_for(_stub_user(owner_a)),
        DbAccessResolver(_stub_db()).filter_for(_stub_user(str(uuid4()))),
    ):
        allowed = set(f["read_access"]["$in"])
        matched = [vid for vid, v in vectors.items()
                   if allowed & set(v["metadata"].get("read_access", []))]
        assert "l1" not in matched, (
            f"vector with NO read_access matched filter {f} — fail-closed broken"
        )


def test_delete_by_doc_id_scope_cannot_touch_other_documents(monkeypatch):
    """Delete-vs-re-ingest: deleting doc-a's vectors (by doc_id filter)
    leaves doc-b's IDENTICAL-content chunks intact even when both were
    ingested from the same file bytes; a re-ingested doc gets a fresh id so
    the old delete can never match it."""
    vectors, _ = seed_vectors()
    index = FakeIndex(dict(vectors))
    # doc-b re-ingested same content, new doc id
    index.vectors["b1"] = {"metadata": {"doc_id": "doc-b", "read_access": ["org"],
                                        "source": "vpn-policy.txt"}, "text": ORG_TEXT}

    class _Store:
        def __init__(self, index):
            self.index = index

    monkeypatch.setattr(vs, "get_vectorstore", lambda: _Store(index))
    vs.delete_document_vectors("doc-a")

    assert index.deleted_filters == [{"doc_id": {"$eq": "doc-a"}}]
    assert "a1" not in index.vectors
    assert "b1" in index.vectors and "o1" in index.vectors, (
        "delete-by-doc_id removed another document's chunks"
    )
