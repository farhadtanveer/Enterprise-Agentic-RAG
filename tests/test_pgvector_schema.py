"""P2: the document_chunks schema (docs/pgvector-design.md §3).

Migration 0006 lands the vector store's chunks INSIDE Postgres — the
columns the task mandates (document_id FK ON DELETE CASCADE, chunk_index,
content, page nullable, embedding vector(N), embedding_model,
embedding_version, generated tsvector, created_at) with PK
(document_id, chunk_index). The Pinecone backend stays the default until
P7; this table is written by NO production code until P3 — these tests pin
the SHAPE so the store can be built on it.

Probes that only read the catalog run in always-rolled-back transactions;
behavioral tests (defaults, cascade) use the production-semantics
db_session fixture. Tests are RED before migration 0006 exists (the table
is absent) — the RED run is recorded in docs/evidence/pgvector-p2-evidence.md.
"""
import pytest
from sqlalchemy import text

TABLE = "document_chunks"


def _columns(db_session):
    return {
        row[0]: row
        for row in db_session.execute(
            text(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
                " WHERE table_schema='public' AND table_name=:t"
            ),
            {"t": TABLE},
        ).all()
    }


# --- catalog probes (rolled back; no data written) --------------------------


def test_table_exists_with_the_exact_task_columns(db_session):
    cols = _columns(db_session)
    assert set(cols) == {
        "document_id",
        "chunk_index",
        "content",
        "page",
        "embedding",
        "embedding_model",
        "embedding_version",
        "generated",
        "created_at",
    }, f"document_chunks columns drifted: {sorted(cols)}"


def test_nullability_matches_the_task(db_session):
    cols = _columns(db_session)
    assert cols["content"][2] == "NO"
    assert cols["embedding"][2] == "NO"
    assert cols["embedding_model"][2] == "NO"
    assert cols["page"][2] == "YES"  # PDFs have pages; txt/md/docx do not
    assert cols["embedding_version"][2] == "NO"
    assert cols["created_at"][2] == "NO"


def test_pk_is_document_id_chunk_index(db_session):
    rows = db_session.execute(
        text(
            "SELECT a.attname FROM pg_index i"
            " JOIN pg_class c ON c.oid = i.indrelid"
            " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)"
            " WHERE c.relname = :t AND i.indisprimary"
        ),
        {"t": TABLE},
    ).scalars().all()
    assert sorted(rows) == ["chunk_index", "document_id"]


def test_embedding_column_is_vector_of_1536(db_session):
    row = db_session.execute(
        text(
            "SELECT t.typname, a.atttypmod FROM pg_attribute a"
            " JOIN pg_class c ON c.oid = a.attrelid"
            " JOIN pg_type t ON t.oid = a.atttypid"
            " WHERE c.relname = :t AND a.attname = 'embedding'"
        ),
        {"t": TABLE},
    ).one_or_none()
    assert row is not None, "no embedding column"
    typname, atttypmod = tuple(row)
    assert typname == "vector"
    # pgvector stores the locked dimension in atttypmod (design §3:
    # dimension-LOCKED per column — a model change needs the §7 procedure)
    assert atttypmod == 1536


def test_generated_is_a_stored_tsvector_of_simple_config(db_session):
    row = db_session.execute(
        text(
            "SELECT a.attgenerated, pg_get_expr(ad.adbin, ad.adrelid)"
            " FROM pg_attribute a"
            " JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum"
            " JOIN pg_class c ON c.oid = a.attrelid"
            " JOIN pg_type t ON t.oid = a.atttypid"
            " WHERE c.relname = :t AND a.attname = 'generated'"
        ),
        {"t": TABLE},
    ).one_or_none()
    assert row is not None, "generated is not a generated column"
    attgenerated, expr = tuple(row)
    assert attgenerated == "s"  # STORED, never recomputed on read
    assert "to_tsvector" in expr
    assert "'simple'" in expr


def test_fk_cascades_from_documents(db_session):
    row = db_session.execute(
        text(
            "SELECT confdeltype FROM pg_constraint con"
            " JOIN pg_class c ON c.oid = con.conrelid"
            " WHERE c.relname = :t AND con.contype = 'f'"
            " AND con.confrelid = 'documents'::regclass"
        ),
        {"t": TABLE},
    ).scalar_one_or_none()
    assert row == "c"  # ON DELETE CASCADE — the §A9.1 one-transaction delete


# --- behavior (production-semantics session; real commits) ------------------


def _Chunk(**kwargs):
    from app.models.document_chunk import DocumentChunk

    return DocumentChunk(**kwargs)


def _make_document(db_session):
    from uuid import uuid4

    from app.models.document import Document

    doc = Document(
        scope="org",
        display_name="p2-probe.md",
        storage_path=f"uploads/{uuid4()}/{uuid4()}.md",
        checksum=uuid4().hex,
        size_bytes=10,
        status="indexed",
    )
    db_session.add(doc)
    db_session.commit()
    return doc


def _embedding_1536(value="0.1") -> str:
    """A syntactically valid vector(1536) literal — the column is
    dimension-LOCKED (pgvector rejects anything else; pinned explicitly by
    test_embedding_dimension_is_locked below)."""
    return "[" + ",".join([value] * 1536) + "]"


def _make_chunk(db_session, doc, index=0, content="vpn reset steps", page=None,
                embedding=None, **overrides):
    embedding = embedding if embedding is not None else _embedding_1536()
    from app.models.document_chunk import DocumentChunk

    chunk = DocumentChunk(
        document_id=doc.id,
        chunk_index=index,
        content=content,
        page=page,
        embedding=embedding,
        embedding_model="text-embedding-3-small",
        **overrides,
    )
    db_session.add(chunk)
    db_session.commit()
    return chunk


def test_defaults_embedding_version_1_and_created_at_now(db_session):
    doc = _make_document(db_session)
    chunk = _make_chunk(db_session, doc)
    assert chunk.embedding_version == 1
    assert chunk.created_at is not None


def test_generated_matches_to_tsvector_simple(db_session):
    doc = _make_document(db_session)
    chunk = _make_chunk(db_session, doc, content="Reset the VPN tunnel first")
    expr = db_session.execute(
        text("SELECT to_tsvector('simple', 'Reset the VPN tunnel first')")
    ).scalar_one()
    assert chunk.generated == expr


def test_embedding_dimension_is_locked_at_1536(db_session):
    """Design §3: the column is dimension-LOCKED — a different-dimension
    insert (e.g. from a model change) must fail loudly, never silently
    truncate. A model change needs the §7 dual-column procedure."""
    from sqlalchemy.exc import DataError

    doc = _make_document(db_session)
    db_session.add(
        _Chunk(document_id=doc.id, chunk_index=0, content="x",
               embedding="[0.1,0.2,0.3]", embedding_model="text-embedding-3-small")
    )
    with pytest.raises(DataError):
        db_session.commit()


def test_fk_rejects_chunks_for_unknown_documents(db_session):
    from uuid import uuid4

    from sqlalchemy.exc import IntegrityError

    from app.models.document_chunk import DocumentChunk

    db_session.add(
        DocumentChunk(
            document_id=uuid4(),  # no documents row
            chunk_index=0,
            content="x",
            embedding=_embedding_1536(),
            embedding_model="text-embedding-3-small",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_content_is_required_page_is_optional(db_session):
    doc = _make_document(db_session)
    from sqlalchemy.exc import IntegrityError

    from app.models.document_chunk import DocumentChunk

    db_session.add(
        DocumentChunk(
            document_id=doc.id,
            chunk_index=0,
            content=None,
            embedding=_embedding_1536(),
            embedding_model="text-embedding-3-small",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    chunk = _make_chunk(db_session, doc, content="pdf chunk with a page", page=7)
    assert chunk.page == 7
    chunk = _make_chunk(db_session, doc, index=1, content="text chunk", page=None)
    assert chunk.page is None


def test_delete_document_cascades_chunks(db_session):
    doc = _make_document(db_session)
    _make_chunk(db_session, doc, index=0)
    _make_chunk(db_session, doc, index=1, content="second chunk")
    db_session.delete(doc)
    db_session.commit()
    remaining = db_session.execute(
        text("SELECT count(*) FROM document_chunks WHERE document_id = :d"),
        {"d": doc.id},
    ).scalar_one()
    assert remaining == 0
