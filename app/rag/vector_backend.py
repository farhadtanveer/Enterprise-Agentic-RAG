"""The VectorStore Protocol and its two adapters (pgvector P3,
docs/pgvector-design.md §2 + PART 0 amendments).

The access seam is an AccessContext (DECISION #3) — a user id or ORG_ONLY;
never a token list, never a Pinecone $in dict. Each adapter renders its own
representation: the Pinecone adapter builds its prefixed tokens via the
AccessResolver, PgVectorStore derives audiences in SQL (§3's derived-at-
query predicate — the same one RLS will reuse at P4).

`vector_store` (settings, DECISION #4) selects the adapter via
get_vector_store_backend(): "pinecone" is the DEFAULT until the golden-
question gate passes (P7); the Pinecone adapter, the setting and the $in
dict die at P10 — a migration device with a mandated removal commit.

NO CALLER IS WIRED in P3: the compatibility entry points in
app/rag/vectorstore.py stay Pinecone-direct; P5 performs the seam switch
with its declared pin changes. This module is exercised by the contract
suite (tests/test_vectorstore_contract.py) on BOTH backends — the Pinecone
leg offline via the in-memory fake, the pgvector leg against real Postgres
with a deterministic embedder.

Connection discipline (§A8): every method opens its OWN short session —
checked out for the duration of the work, released before returning; the
graph run holds no DB resource across LLM calls (P5 wires the search the
same way). From P4 the same sessions SET LOCAL app.user_id for RLS.
"""
import logging
from typing import Any, Protocol, runtime_checkable

from langchain_core.documents import Document
from sqlalchemy import text
from sqlalchemy.orm import Session as DBSession

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.services.access import AccessContext, DbAccessResolver

logger = logging.getLogger(__name__)


@runtime_checkable
class VectorStore(Protocol):
    """The backend-agnostic contract (§2.1). chunk-identity is
    (document_id, chunk_index); add_chunks carries REPLACE semantics; the
    Document metadata contract is doc_id/scope/source always — page and
    created_at wherever the backend stores them (pgvector: always;
    Pinecone: never did — the declared gap)."""

    def add_chunks(self, chunks: list[Document], *, doc_id, scope: str,
                   owner_id, source_name: str, group_id) -> int:
        """Stamp ACL metadata (today vectorstore.py:151-199), embed, persist.
        Replace semantics INCLUDED: chunks of doc_id are fully replaced."""

    def delete_document(self, doc_id) -> None:
        """Idempotent delete of every chunk of doc_id (today :202-211)."""

    def search(self, query_text: str, *, context: AccessContext,
               k: int | None = None) -> list[Document]:
        """Embed query_text, k-NN restricted to the context's audience
        (§A7: refuses a missing context; ORG_ONLY sees ONLY org rows)."""

    def copy_chunks(self, source_doc_id, target_doc_id) -> int:
        """Chunk rows copied WITHOUT re-embedding (Part B share-copy, §10)."""

    def replace_document(self, doc_id, chunks: list[Document], *, scope: str,
                         owner_id, source_name: str, group_id) -> int:
        """New content replaces old (C6); on pgvector inside ONE
        transaction — the C6 call-order test becomes a transaction-
        boundary test (§10)."""

    def count_chunks(self, doc_id) -> int:
        """Exact per-document probe (the A5.4 lesson: never stats counters)."""


class _UserRef:
    """audiences_for(user) reads only ``user.id``; the AccessContext carries
    the id — this is the adapter-side bridge, NOT a new identity type."""

    __slots__ = ("id",)

    def __init__(self, user_id):
        self.id = user_id


class PineconeVectorStoreAdapter:
    """Wraps the existing Pinecone module functions verbatim (P3–P7; dies
    at P10). Needs a session_factory ONLY to resolve a user context into
    tokens via the resolver (ORG_ONLY needs none)."""

    def __init__(self, session_factory=None):
        self.session_factory = session_factory or get_sessionmaker

    def _pinecone_filter(self, context: AccessContext) -> dict:
        if context is None:
            raise ValueError(
                "search requires an AccessContext (§A7 — unfiltered "
                "retrieval is not a code path this application has)"
            )
        if context.user is None:  # ORG_ONLY — D8, no DB needed
            from app.services.retrieval import ORG_ONLY_FILTER

            return ORG_ONLY_FILTER
        session = self.session_factory()
        try:
            audiences = DbAccessResolver(session).audiences_for(_UserRef(context.user))
        finally:
            session.close()
        return {"read_access": {"$in": audiences}}

    def add_chunks(self, chunks, *, doc_id, scope, owner_id, source_name,
                   group_id) -> int:
        import app.rag.vectorstore as vs

        vs.add_documents(
            list(chunks), doc_id=str(doc_id), scope=scope,
            owner_id=str(owner_id) if owner_id else None,
            source_name=source_name,
            group_id=str(group_id) if group_id else None,
        )
        return len(chunks)

    def delete_document(self, doc_id) -> None:
        import app.rag.vectorstore as vs

        vs.delete_document_vectors(str(doc_id))

    def search(self, query_text, *, context, k=None):
        import app.rag.vectorstore as vs

        docs = vs.get_retriever(self._pinecone_filter(context)).invoke(query_text)
        return docs[:k] if k is not None else docs

    def copy_chunks(self, source_doc_id, target_doc_id) -> int:
        # No copy primitive in Pinecone: the chunk ids do not encode doc_id,
        # so the source chunks cannot be enumerated without reads (the cap
        # blocks reads until 2026-12-01). Copy lands on pgvector at P8.
        raise NotImplementedError(
            "copy_chunks is a pgvector capability (P8); the Pinecone era "
            "has no chunk-enumeration primitive (design §2.1/§10)"
        )

    def replace_document(self, doc_id, chunks, *, scope, owner_id,
                         source_name, group_id) -> int:
        # The Pinecone-era order: delete-then-add across two external calls
        # (the window C6's ordering existed to bound). On pgvector this is
        # one transaction (see PgVectorStore).
        self.delete_document(doc_id)
        return self.add_chunks(chunks, doc_id=doc_id, scope=scope,
                               owner_id=owner_id, source_name=source_name,
                               group_id=group_id)

    def count_chunks(self, doc_id) -> int:
        # Pinecone cannot count-by-doc_id without reads (ids do not encode
        # doc_id). Declared gap; the pgvector backend implements it.
        raise NotImplementedError(
            "count_chunks is a pgvector capability; Pinecone cannot count "
            "by doc_id without reads (the A5.4 cap)"
        )


class PgVectorStore:
    """Postgres + pgvector (migration 0006): exact KNN with the derived
    access predicate (§3/§4 — HNSW only after the measured decision rule).
    Vectors bind as '[f1,f2,...]' strings cast ::vector — floats only, no
    injection surface, NO pgvector-python dependency (§2.2). The embedder
    is injectable (the contract suite uses a deterministic fake); the
    default resolves lazily to the OpenAI-compatible embeddings (§7) at
    first use — importing this module never needs an API key."""

    def __init__(self, session_factory=None, embedder=None):
        self.session_factory = session_factory or get_sessionmaker
        self._embedder = embedder

    def _embed(self):
        if self._embedder is None:
            from app.rag.vectorstore import get_embeddings

            return get_embeddings()
        return self._embedder

    def _checked_literal(self, vec) -> str:
        dim = get_settings().embedding_dimensions
        if len(vec) != dim:
            raise ValueError(
                f"embedding has {len(vec)} dimensions, "
                f"EMBEDDING_DIMENSIONS is {dim} — a model change needs the "
                "§7 dual-column procedure, never a silent dimension drift"
            )
        return "[" + ",".join(str(float(v)) for v in vec) + "]"

    @staticmethod
    def _documents(rows) -> list[Document]:
        return [
            Document(page_content=r.content, metadata={
                "doc_id": str(r.document_id),
                "chunk_index": r.chunk_index,
                "scope": r.scope,
                "source": r.display_name,  # citation titles (workflow.py:188-193)
                "page": r.page,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
            for r in rows
        ]

    _SEARCH_SQL = text(
        "SELECT c.document_id, c.chunk_index, c.content, c.page, c.created_at,"
        "       d.scope, d.display_name"
        " FROM document_chunks c JOIN documents d ON d.id = c.document_id"
        " WHERE d.status = 'indexed'"
        "   AND (d.scope = 'org'"
        "        OR (d.scope = 'private' AND d.owner_id = :uid)"
        "        OR (d.scope = 'group' AND :uid IS NOT NULL AND EXISTS ("
        "             SELECT 1 FROM group_memberships m"
        "             JOIN groups g ON g.id = m.group_id"
        "             WHERE m.group_id = d.group_id AND m.user_id = :uid"
        "               AND g.is_active)))"
        " ORDER BY c.embedding <=> CAST(:qvec AS vector)"
        " LIMIT :k"
    )

    def add_chunks(self, chunks, *, doc_id, scope, owner_id, source_name,
                   group_id) -> int:
        chunks = list(chunks)
        vectors = self._embed().embed_documents([c.page_content for c in chunks])
        model = get_settings().embedding_model
        literals = [self._checked_literal(v) for v in vectors]
        db: DBSession = self.session_factory()
        try:
            # replace semantics INCLUDED (§2.1) — delete-then-insert inside
            # ONE transaction: a crash leaves old or new, never neither.
            db.execute(text("DELETE FROM document_chunks WHERE document_id = :d"),
                       {"d": doc_id})
            for idx, (chunk, literal) in enumerate(zip(chunks, literals)):
                db.execute(text(
                    "INSERT INTO document_chunks"
                    " (document_id, chunk_index, content, page, embedding, embedding_model)"
                    " VALUES (:doc, :idx, :content, :page, CAST(:qv AS vector), :model)"
                ), {
                    "doc": doc_id, "idx": idx, "content": chunk.page_content,
                    "page": chunk.metadata.get("page"),
                    "qv": literal, "model": model,
                })
            db.commit()
            return len(chunks)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def delete_document(self, doc_id) -> None:
        db: DBSession = self.session_factory()
        try:
            db.execute(text("DELETE FROM document_chunks WHERE document_id = :d"),
                       {"d": doc_id})
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def search(self, query_text, *, context, k=None):
        if context is None:
            raise ValueError(
                "search requires an AccessContext (§A7 — unfiltered "
                "retrieval is not a code path this application has); "
                "ORG_ONLY is the explicit org-restricted context"
            )
        qvec = self._embed().embed_query(query_text)
        literal = self._checked_literal(qvec)
        k = k if k is not None else get_settings().top_k
        db: DBSession = self.session_factory()
        try:
            rows = db.execute(self._SEARCH_SQL, {
                "uid": context.user, "qvec": literal, "k": k,
            }).all()
            return self._documents(rows)
        finally:
            db.close()

    def copy_chunks(self, source_doc_id, target_doc_id) -> int:
        db: DBSession = self.session_factory()
        try:
            result = db.execute(text(
                "INSERT INTO document_chunks"
                " (document_id, chunk_index, content, page, embedding,"
                "  embedding_model, embedding_version, created_at)"
                " SELECT :target, chunk_index, content, page, embedding,"
                "        embedding_model, embedding_version, now()"
                " FROM document_chunks WHERE document_id = :source"
            ), {"target": target_doc_id, "source": source_doc_id})
            db.commit()
            return int(result.rowcount or 0)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def replace_document(self, doc_id, chunks, *, scope, owner_id,
                         source_name, group_id) -> int:
        # C6's guarantee on ONE transaction: a crash leaves the old version
        # or the new one, never neither and never both (§10 — the call-order
        # pin becomes a transaction-boundary pin).
        return self.add_chunks(chunks, doc_id=doc_id, scope=scope,
                               owner_id=owner_id, source_name=source_name,
                               group_id=group_id)

    def count_chunks(self, doc_id) -> int:
        db: DBSession = self.session_factory()
        try:
            return int(db.execute(
                text("SELECT count(*) FROM document_chunks WHERE document_id = :d"),
                {"d": doc_id},
            ).scalar_one())
        finally:
            db.close()


def get_vector_store_backend(session_factory=None) -> VectorStore:
    """The factory (§2.2): ``vector_store`` selects the adapter. Unknown
    values fail FAST here, never half-configured at search time."""
    backend = get_settings().vector_store
    if backend == "pinecone":
        return PineconeVectorStoreAdapter(session_factory=session_factory)
    if backend == "pgvector":
        return PgVectorStore(session_factory=session_factory)
    raise ValueError(
        f"unknown vector_store {backend!r} — expected 'pinecone' or 'pgvector'"
    )
