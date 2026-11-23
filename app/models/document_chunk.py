from datetime import datetime
from uuid import UUID

from sqlalchemy import Computed, DateTime, ForeignKey, Integer, Text, Uuid, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from app.core.db import Base


class Vector(UserDefinedType):
    """pgvector's VECTOR(n) (pgvector P2, docs/pgvector-design.md §3).

    Bound values are ``'[f1,f2,...]'`` strings — floats only, no injection
    surface, so pgvector-python is NOT a dependency (design §2.2). The
    dimension mirrors migration 0006's literal (1536, the
    text-embedding-3-small default); the settings-driven validation of that
    dimension lands with the PgVectorStore (P3) — no decorative settings
    before its first reader exists."""

    cache_ok = True

    def __init__(self, dimension: int = 1536):
        self.dimension = dimension

    def get_col_spec(self, **kw) -> str:
        return f"VECTOR({self.dimension})"


class DocumentChunk(Base):
    """One indexed chunk of a document (docs/pgvector-design.md §3).

    PK (document_id, chunk_index): re-ingest upserts over itself; two
    users' same-named files can never collide (the doc-scoping the sha256
    chunk-id grammar provided, now enforced by the key). Deleting the
    document cascades — §A9.1's vectors-first dance collapses to one
    transaction at P7. ``generated`` is a STORED tsvector for the
    hybrid-search design (§6, default OFF); SQLAlchemy excludes it from
    INSERTs (Computed). ``page`` is the PDF page at chunk time (B4
    citations); NULL for txt/md/docx.
    """

    __tablename__ = "document_chunks"

    document_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    embedding = mapped_column(Vector(1536), nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text)
    embedding_version: Mapped[int] = mapped_column(Integer, default=1)
    generated: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DocumentChunk doc={self.document_id} idx={self.chunk_index}>"
