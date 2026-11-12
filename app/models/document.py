from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Document(Base):
    """Ingested document metadata (§A4/§A14.4). Three audiences (G1.1):
    org docs and group docs are OWNERLESS (owner_id NULL — D16/C2); private
    docs always carry an owner. ``uploaded_by`` records who PUT the file in
    for every scope — attribution without ownership: deleting that user
    never deletes org/group documents (their uploaded_by goes NULL), and
    deleting a group is RESTRICTed while documents reference it."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("scope IN ('org', 'group', 'private')", name="ck_documents_scope"),
        CheckConstraint(
            "(scope = 'org' AND owner_id IS NULL AND group_id IS NULL)"
            " OR (scope = 'group' AND owner_id IS NULL AND group_id IS NOT NULL)"
            " OR (scope = 'private' AND owner_id IS NOT NULL AND group_id IS NULL)",
            name="ck_documents_scope_owner_group",
        ),
        CheckConstraint("status IN ('pending', 'indexed', 'failed', 'deleting')", name="ck_documents_status"),
        # same file may legitimately exist as org AND private; dedupe is
        # advisory (§A7) — NULL keys are distinct in PG, so org rows never
        # collide; group docs dedupe per group (different groups may hold
        # the same file)
        UniqueConstraint(
            "checksum", "scope", "owner_id", "group_id",
            name="uq_documents_checksum_scope_owner_group",
        ),
        Index("ix_documents_scope", "scope"),
        Index("ix_documents_owner_id", "owner_id"),
        Index("ix_documents_group_id", "group_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, server_default=func.uuidv7())
    owner_id: Mapped[UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE")
    )
    scope: Mapped[str] = mapped_column(String(8))
    group_id: Mapped[UUID | None] = mapped_column(
        Uuid(), ForeignKey("groups.id", ondelete="RESTRICT")
    )
    display_name: Mapped[str] = mapped_column(Text)  # original filename, for citations
    storage_path: Mapped[str] = mapped_column(Text)  # uploads/<doc_id>/<display_name>
    checksum: Mapped[str] = mapped_column(Text)  # sha256 of bytes: dedupe + integrity
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_suffix: Mapped[str | None] = mapped_column(String(16))
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    # WHO put the document in, every scope — attribution without ownership:
    # SET NULL on the uploader's deletion, never a delete of the document (C2)
    uploaded_by: Mapped[UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # failure class only (e.g. "ingest_failed") — never chunk/exception content
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Document {self.scope}/{self.status} {self.display_name}>"
