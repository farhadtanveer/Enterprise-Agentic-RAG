"""document_chunks — the vector store moves INTO Postgres (pgvector P2)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-23

docs/pgvector-design.md §3 + docs/tasks/pgvector-p1.md (P2 of the phased
plan). ADDITIVE: the Pinecone backend stays the default until P7 and no
production code writes this table until P3 — the schema lands first so the
PgVectorStore is built on a reviewed shape (Farhad reviews the schema at
this checkpoint).

- PK (document_id, chunk_index): the deterministic sha256 chunk-id grammar
  (vectorstore.py:140-148) dies — its job (doc-scoped replace + delete)
  becomes this PK + the CASCADE; re-ingest upserts over the same key.
- document_id FK ON DELETE CASCADE: §A9.1's two-system delete dance
  collapses to one transaction at P7 (docs/pgvector-design.md §8.4).
- embedding vector(1536): dimension-LOCKED per column (pgvector stores the
  dimension in atttypmod); the literal matches settings.embedding_dimensions
  (1536, the text-embedding-3-small default, DECISION #7). A model/dimension
  change needs the §7 dual-column procedure, never an in-place rewrite.
  Requires the `vector` extension (docker/initdb/02-pgvector.sql on fresh
  volumes; scripts/sql/bootstrap-existing.sql on existing clusters).
- generated tsvector GENERATED ALWAYS AS to_tsvector('simple', content)
  STORED: the hybrid-search column (§6, default OFF); 'simple' is
  language-agnostic — the 'german' config is the evaluated alternative, and
  swapping the expression later is a table rewrite, decided by measurement.
- page: the PDF page at chunk time (B4 citations live here from day one).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


class VECTOR(UserDefinedType):
    """pgvector's VECTOR(n), literal col_spec — no pgvector-python
    dependency (design §2.2: values bind as '[f1,f2,...]' strings)."""

    def get_col_spec(self, **kw) -> str:
        return "VECTOR(1536)"


def upgrade() -> None:
    op.create_table(
        "document_chunks",
        sa.Column(
            "document_id",
            postgresql.UUID(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("embedding", VECTOR(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("embedding_version", sa.Integer(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column(
            "generated",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', content)", persisted=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("document_id", "chunk_index"),
    )


def downgrade() -> None:
    # chunks are re-derivable from uploads/<doc_id> (design §8.1) — dropping
    # the table is safe at any point before P7 cutover.
    op.drop_table("document_chunks")
