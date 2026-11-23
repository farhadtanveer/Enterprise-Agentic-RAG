"""P3 contract tests: the VectorStore Protocol on BOTH backends
(docs/pgvector-design.md §2 + §11.1, PART 0 amendments: AccessContext).

The SAME fixtures and assertions run against:
- the Pinecone adapter, backed by an in-memory fake that implements the
  Pinecone semantics the ACL relies on ($in over the read_access ARRAY,
  missing key ⇒ no match, delete-by-doc_id filter) — offline, keyless,
  deterministic;
- PgVectorStore, against REAL Postgres (the pgvector image; migration 0006
  schema, exact search) with a DETERMINISTIC hash embedder — no API key,
  no network.

Both adapters resolve an AccessContext to audiences the SAME way (via the
AccessResolver), so the suite doubles as the first Python-vs-SQL parity
matrix (§11.4): whatever the resolver grants, both engines must return —
and nothing outside it.

Ranking under the hash embedder is deterministic but NOT semantic, so
tests assert on the audience-visible SET (never on semantic order), with
k small enough that the default top_k=4 cannot truncate a scenario.
"""
import hashlib
from uuid import uuid4

import pytest

from app.models.document import Document
from app.models.group import Group, GroupMembership
from app.models.user import User

BACKENDS = ["pinecone", "pgvector"]

ORG_TEXT = "Contract org chunk: the VPN portal requires MFA."
ALICE_TEXT = "Contract private chunk for Alice only: her salary band is 250k."
BOB_TEXT = "Contract private chunk for Bob only: his laptop is a ThinkPad."
G1_TEXT = "Contract G1 chunk: the alpha team uses the build runner."
G2_TEXT = "Contract G2 chunk: the archived project's old notes."


class _HashEmbedder:
    """Deterministic, keyless, 1536-dim (the locked column dimension).
    Same text ⇒ same vector; cosine ranking is deterministic but NOT
    semantic — the suite asserts audience SETS, never semantic order."""

    dimension = 1536

    def _vec(self, text: str) -> list[float]:
        seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
        vals = []
        x = seed or 1
        for _ in range(self.dimension):
            x = (x * 1103515245 + 12345) % (2**31)
            vals.append((x / float(2**30)) - 1.0)
        return vals

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


# --- the Pinecone-leg fake (the semantics the ACL relies on) ----------------


class _FakeIndex:
    """$in over the read_access ARRAY; a vector with NO read_access key
    matches nothing (fail closed); delete filters by doc_id only."""

    def __init__(self):
        self.vectors = {}  # id -> {"text", "metadata"}; insertion-ordered
        self._order = []

    def delete(self, filter=None, namespace=None, **kw):
        doc_id = filter["doc_id"]["$eq"]
        doomed = [
            i for i in self._order
            if str(self.vectors[i]["metadata"].get("doc_id")) == str(doc_id)
        ]
        for i in doomed:
            del self.vectors[i]
            self._order.remove(i)

    def add(self, ids, chunks):
        for i, c in zip(ids, chunks):
            self.vectors[i] = {"text": c.page_content, "metadata": dict(c.metadata)}
            self._order.append(i)


class _FakeStore:
    def __init__(self):
        self.index = _FakeIndex()

    def add_documents(self, chunks, ids=None, **kw):
        ids = list(ids) if ids else [str(uuid4()) for _ in chunks]
        self.index.add(ids, chunks)
        return ids

    def as_retriever(self, search_kwargs):
        idx = self.index
        kw = dict(search_kwargs)

        class _R:
            def invoke(self, _query):
                allowed = set(kw["filter"]["read_access"]["$in"])
                out = []
                for i in idx._order:
                    v = idx.vectors[i]
                    if allowed & set(v["metadata"].get("read_access", [])):
                        out.append(type("D", (), {
                            "page_content": v["text"],
                            "metadata": dict(v["metadata"]),
                        })())
                        if len(out) >= kw.get("k", 4):
                            break
                return out

        return _R()


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


@pytest.fixture
def pinecone_fake(monkeypatch):
    fake = _FakeStore()
    import app.rag.vectorstore as vs

    monkeypatch.setattr(vs, "get_vectorstore", lambda: fake)
    return fake


@pytest.fixture
def store(backend, pinecone_fake, joined_session_factory):
    from app.rag import vector_backend

    if backend == "pinecone":
        return vector_backend.PineconeVectorStoreAdapter(
            session_factory=joined_session_factory
        )
    return vector_backend.PgVectorStore(
        session_factory=joined_session_factory, embedder=_HashEmbedder()
    )


@pytest.fixture
def world(db_session):
    """alice (member of active g1 and ARCHIVED g2), bob (no groups); indexed
    documents: org, alice-private, bob-private, g1-group, g2-group (the
    archived group's doc is retrievable by NOBODY until reactivation —
    H13). The DOCUMENT rows are shared by both legs; chunks are added by
    the store under test."""
    alice = User(username=f"alice-{uuid4().hex[:8]}", password_hash="x", role="user")
    bob = User(username=f"bob-{uuid4().hex[:8]}", password_hash="x", role="user")
    g1 = Group(name=f"g1-{uuid4().hex[:8]}", kind="department", is_active=True)
    g2 = Group(name=f"g2-{uuid4().hex[:8]}", kind="department", is_active=False)
    db_session.add_all([alice, bob, g1, g2])
    db_session.commit()
    db_session.add_all([
        GroupMembership(group_id=g1.id, user_id=alice.id, role="member"),
        GroupMembership(group_id=g2.id, user_id=alice.id, role="member"),
    ])
    docs = {
        "org": Document(scope="org", display_name="org.md",
                        storage_path=f"uploads/{uuid4()}/x.md",
                        checksum=uuid4().hex, size_bytes=10, status="indexed"),
        "alice_priv": Document(scope="private", owner_id=alice.id,
                               display_name="alice.md",
                               storage_path=f"uploads/{uuid4()}/x.md",
                               checksum=uuid4().hex, size_bytes=10, status="indexed"),
        "bob_priv": Document(scope="private", owner_id=bob.id,
                             display_name="bob.md",
                             storage_path=f"uploads/{uuid4()}/x.md",
                             checksum=uuid4().hex, size_bytes=10, status="indexed"),
        "g1_doc": Document(scope="group", group_id=g1.id, display_name="g1.md",
                           storage_path=f"uploads/{uuid4()}/x.md",
                           checksum=uuid4().hex, size_bytes=10, status="indexed"),
        "g2_doc": Document(scope="group", group_id=g2.id, display_name="g2.md",
                           storage_path=f"uploads/{uuid4()}/x.md",
                           checksum=uuid4().hex, size_bytes=10, status="indexed"),
    }
    db_session.add_all(docs.values())
    db_session.commit()
    return {"alice": alice, "bob": bob, "g1": g1, "g2": g2, "docs": docs}


def _seed(store, world):
    """One chunk per document via the store under test."""
    for key, text in [("org", ORG_TEXT), ("alice_priv", ALICE_TEXT),
                      ("bob_priv", BOB_TEXT), ("g1_doc", G1_TEXT),
                      ("g2_doc", G2_TEXT)]:
        doc = world["docs"][key]
        store.add_chunks(
            [type("C", (), {"page_content": text, "metadata": {}})()],
            doc_id=doc.id, scope=doc.scope,
            owner_id=doc.owner_id, source_name=doc.display_name,
            group_id=doc.group_id,
        )


def _ids(store, query, context, k=None):
    kw = {"k": k} if k is not None else {}
    docs = store.search(query, context=context, **kw)
    return {str(d.metadata["doc_id"]) for d in docs}


# --- the contract (both backends must agree) --------------------------------


def test_both_adapters_satisfy_the_protocol(store):
    from app.rag.vector_backend import VectorStore

    assert isinstance(store, VectorStore), type(store)


def test_org_only_context_sees_only_org(store, world):
    from app.services.access import ORG_ONLY

    _seed(store, world)
    assert _ids(store, "contract", ORG_ONLY) == {str(world["docs"]["org"].id)}


def test_user_with_one_active_group_sees_org_own_and_that_group_only(store, world):
    from app.services.access import AccessContext

    _seed(store, world)
    seen = _ids(store, "contract", AccessContext(user=world["alice"].id), k=10)
    assert seen == {
        str(world["docs"]["org"].id),
        str(world["docs"]["alice_priv"].id),
        str(world["docs"]["g1_doc"].id),
    }, "alice must see org + her private + the ACTIVE group; never the archived G2"


def test_user_with_no_groups_sees_org_and_own_only(store, world):
    from app.services.access import AccessContext

    _seed(store, world)
    seen = _ids(store, "contract", AccessContext(user=world["bob"].id), k=10)
    assert seen == {
        str(world["docs"]["org"].id),
        str(world["docs"]["bob_priv"].id),
    }


def test_archived_group_chunk_is_retrievable_by_nobody(store, world):
    """H13 through both engines: the archived G2's doc is in NO audience —
    not even its (former) member's."""
    from app.services.access import AccessContext

    _seed(store, world)
    for uid in (world["alice"].id, world["bob"].id):
        assert str(world["docs"]["g2_doc"].id) not in _ids(
            store, "contract", AccessContext(user=uid), k=10
        )
    from app.services.access import ORG_ONLY

    assert str(world["docs"]["g2_doc"].id) not in _ids(store, "contract", ORG_ONLY)


def test_membership_revoked_applies_to_the_next_search(store, world):
    """The resolver recomputes on every call — and the SQL predicate joins
    live rows. Revoking alice's G1 membership changes the NEXT search on
    BOTH backends (the §11.4 parity property)."""
    from app.services.access import AccessContext
    from sqlalchemy import delete as sa_delete

    _seed(store, world)
    g1_id = world["g1"].id
    assert str(world["docs"]["g1_doc"].id) in _ids(
        store, "contract", AccessContext(user=world["alice"].id), k=10
    )
    db = store.session_factory()  # both adapters carry the same factory
    db.execute(sa_delete(GroupMembership).where(GroupMembership.group_id == g1_id))
    db.commit()
    db.close()
    assert str(world["docs"]["g1_doc"].id) not in _ids(
        store, "contract", AccessContext(user=world["alice"].id), k=10
    )


def test_replace_semantics_fully_replace_the_document(store, world):
    doc = world["docs"]["alice_priv"]
    store.add_chunks(
        [type("C", (), {"page_content": "old version text", "metadata": {}})()],
        doc_id=doc.id, scope="private", owner_id=world["alice"].id,
        source_name="alice.md", group_id=None,
    )
    store.add_chunks(
        [type("C", (), {"page_content": ALICE_TEXT, "metadata": {}})(),
         type("C", (), {"page_content": "second chunk of the new version", "metadata": {}})()],
        doc_id=doc.id, scope="private", owner_id=world["alice"].id,
        source_name="alice.md", group_id=None,
    )
    from app.services.access import AccessContext

    hits = store.search("contract", context=AccessContext(user=world["alice"].id), k=10)
    mine = [h for h in hits if str(h.metadata["doc_id"]) == str(doc.id)]
    assert len(mine) == 2, "replace leaves EXACTLY the new chunks"
    texts = " ".join(h.page_content for h in mine)
    assert "old version text" not in texts
    assert ALICE_TEXT in texts


def test_delete_document_is_idempotent(store, world):
    from app.services.access import AccessContext

    doc = world["docs"]["bob_priv"]
    _seed(store, world)
    store.delete_document(doc.id)
    store.delete_document(doc.id)  # second call: no-op, never raises
    assert str(doc.id) not in _ids(store, "contract", AccessContext(user=world["bob"].id), k=10)
    assert all(
        str(h.metadata["doc_id"]) != str(doc.id)
        for h in store.search("contract", context=AccessContext(user=world["alice"].id), k=10)
    )


def test_missing_context_refuses_to_search(store, world):
    _seed(store, world)
    with pytest.raises(ValueError, match="[Cc]ontext"):
        store.search("contract", context=None)


def test_k_override_truncates(store, world):
    from app.services.access import ORG_ONLY

    store.add_chunks(
        [type("C", (), {"page_content": f"org chunk {i}", "metadata": {}})() for i in range(3)],
        doc_id=world["docs"]["org"].id, scope="org", owner_id=None,
        source_name="org.md", group_id=None,
    )
    assert len(store.search("org", context=ORG_ONLY, k=2)) == 2
    assert len(store.search("org", context=ORG_ONLY)) <= 4  # settings.top_k


def test_copy_chunks_copies_without_reembedding(store, backend, world, db_session):
    if backend != "pgvector":
        pytest.skip("Pinecone has no copy primitive and the cap blocks its reads; "
                    "copy lands on pgvector (P8) — the declared §2.1 gap")
    from app.services.access import AccessContext

    src = world["docs"]["alice_priv"]
    _seed(store, world)
    target = Document(scope="private", owner_id=world["alice"].id,
                      display_name="alice-copy.md",
                      storage_path=f"uploads/{uuid4()}/x.md",
                      checksum=uuid4().hex, size_bytes=10, status="indexed")
    db_session.add(target)  # the caller creates the documents row (Part B does this with the share flow)
    db_session.commit()
    copied = store.copy_chunks(src.id, target.id)
    assert copied >= 1
    assert store.count_chunks(target.id) == store.count_chunks(src.id)
    seen = store.search("contract", context=AccessContext(user=world["alice"].id), k=10)
    texts = " ".join(h.page_content for h in hits_of(seen, target))
    assert ALICE_TEXT in texts, "the copied chunks must be retrievable under the target document"


def hits_of(docs, doc):
    return [d for d in docs if str(d.metadata["doc_id"]) == str(doc.id)]


# --- backend-specific contracts ---------------------------------------------


def test_count_chunks_exact_on_pgvector_declared_gap_on_pinecone(store, backend, world):
    doc = world["docs"]["org"]
    store.add_chunks(
        [type("C", (), {"page_content": f"org chunk {i}", "metadata": {}})() for i in range(3)],
        doc_id=doc.id, scope="org", owner_id=None, source_name="org.md", group_id=None,
    )
    if backend == "pgvector":
        assert store.count_chunks(doc.id) == 3
        assert store.count_chunks(uuid4()) == 0  # unknown id: zero, never an error
    else:
        with pytest.raises(NotImplementedError, match="count"):
            store.count_chunks(doc.id)


def test_pgvector_metadata_carries_page_and_created_at(store, backend, world):
    if backend != "pgvector":
        pytest.skip("page/created_at are stored per chunk on pgvector only; "
                   "Pinecone never stored them (design §2.1 — declared gap)")
    from app.services.access import AccessContext

    doc = world["docs"]["org"]
    store.add_chunks(
        [type("C", (), {"page_content": ORG_TEXT,
                        "metadata": {"page": 3}})()],
        doc_id=doc.id, scope="org", owner_id=None,
        source_name="org.md", group_id=None,
    )
    hit = store.search("contract", context=AccessContext(user=None), k=4)[0]
    assert hit.metadata["doc_id"] == str(doc.id)
    assert hit.metadata["source"] == "org.md"
    assert hit.metadata["page"] == 3
    assert hit.metadata["created_at"] is not None


def test_pgvector_refuses_chunks_for_unknown_documents(store, backend, world):
    if backend != "pgvector":
        pytest.skip("the FK lives in Postgres; the in-memory fake enforces no schema")
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        store.add_chunks(
            [type("C", (), {"page_content": "orphan", "metadata": {}})()],
            doc_id=uuid4(), scope="org", owner_id=None,
            source_name="x.md", group_id=None,
        )


# --- the two new settings' EFFECTS (H7 rule: no decorative settings) --------


def test_vector_store_setting_selects_the_adapter(monkeypatch):
    """DECISION #4: the transitional selector is load-bearing — the factory
    honors it, defaults to pinecone, and fails FAST on an unknown value."""
    from app.core.config import get_settings
    from app.rag import vector_backend

    assert get_settings().vector_store == "pinecone"  # the shipped default
    assert isinstance(
        vector_backend.get_vector_store_backend(), vector_backend.PineconeVectorStoreAdapter
    )
    monkeypatch.setattr(get_settings(), "vector_store", "pgvector")
    assert isinstance(
        vector_backend.get_vector_store_backend(), vector_backend.PgVectorStore
    )
    monkeypatch.setattr(get_settings(), "vector_store", "qdrant")
    with pytest.raises(ValueError, match="vector_store"):
        vector_backend.get_vector_store_backend()


def test_embedding_dimensions_setting_gates_every_vector(monkeypatch):
    """DECISION #7: PgVectorStore refuses a vector whose dimension differs
    from EMBEDDING_DIMENSIONS — loudly, before the DB sees it. A model
    change goes through the §7 dual-column procedure, never a silent
    drift (the DB's own lock is pinned by test_pgvector_schema.py)."""
    from app.core.config import get_settings

    store = None
    from app.rag import vector_backend

    store = vector_backend.PgVectorStore(
        session_factory=lambda: None, embedder=_HashEmbedder()
    )
    good = _HashEmbedder().embed_query("x")  # 1536-dim
    assert len(store._checked_literal(good).split(",")) == 1536
    monkeypatch.setattr(get_settings(), "embedding_dimensions", 4)
    with pytest.raises(ValueError, match="EMBEDDING_DIMENSIONS"):
        store._checked_literal(good)
    assert store._checked_literal([0.1, 0.2, 0.3, 0.4]) == "[0.1,0.2,0.3,0.4]"
