from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Document(Base):
    """Ingested document metadata (§A4). Org docs are ownerless BY
    DEFINITION — owner_id is ALWAYS NULL for scope='org' (D16), enforced by
    CHECKs so nothing is ever attributed to or reassigned from an admin."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("scope IN ('org', 'private')", name="ck_documents_scope"),
        CheckConstraint(
            "(scope = 'org' AND owner_id IS NULL) OR (scope = 'private' AND owner_id IS NOT NULL)",
            name="ck_documents_owner_matches_scope",
        ),
        CheckConstraint("status IN ('pending', 'indexed', 'failed', 'deleting')", name="ck_documents_status"),
        # same file may legitimately exist as org AND private; dedupe is
        # advisory (§A7) — org rows all share NULL owner (distinct in PG)
        UniqueConstraint("checksum", "scope", "owner_id", name="uq_documents_checksum_scope_owner"),
        Index("ix_documents_scope", "scope"),
        Index("ix_documents_owner_id", "owner_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, server_default=func.uuidv7())
    owner_id: Mapped[UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE")
    )
    scope: Mapped[str] = mapped_column(String(8))
    display_name: Mapped[str] = mapped_column(Text)  # original filename, for citations
    storage_path: Mapped[str] = mapped_column(Text)  # uploads/<doc_id>/<display_name>
    checksum: Mapped[str] = mapped_column(Text)  # sha256 of bytes: dedupe + integrity
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_suffix: Mapped[str | None] = mapped_column(String(16))
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # failure class only (e.g. "ingest_failed") — never chunk/exception content
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Document {self.scope}/{self.status} {self.display_name}>"
