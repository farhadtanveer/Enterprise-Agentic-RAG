"""documents table (B4)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29

Document metadata + lifecycle (docs/backend-design.md §A4, D16): org docs
are ownerless BY DEFINITION (owner_id IS NULL, CHECK-enforced); private
docs always carry an owner. The (checksum, scope, owner_id) unique is
per-owner, not global — dedupe stays advisory (§A7).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("owner_id", postgresql.UUID(), nullable=True),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime_suffix", sa.String(length=16), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("scope IN ('org', 'private')", name="ck_documents_scope"),
        sa.CheckConstraint(
            "(scope = 'org' AND owner_id IS NULL) OR (scope = 'private' AND owner_id IS NOT NULL)",
            name="ck_documents_owner_matches_scope",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'indexed', 'failed', 'deleting')", name="ck_documents_status"
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checksum", "scope", "owner_id", name="uq_documents_checksum_scope_owner"),
    )
    op.create_index(op.f("ix_documents_scope"), "documents", ["scope"], unique=False)
    op.create_index(op.f("ix_documents_owner_id"), "documents", ["owner_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_documents_owner_id"), table_name="documents")
    op.drop_index(op.f("ix_documents_scope"), table_name="documents")
    op.drop_table("documents")
