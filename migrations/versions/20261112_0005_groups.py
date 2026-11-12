"""groups + group_memberships; documents gain group scope (G1.1)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-12

docs/tasks/g1.md PART 1 / docs/backend-design.md §A14.4 (M1+M2 folded into
one schema step; share_requests stays out until G2). One concern: the
ACCESS-CONTROL schema.

- groups: name unique among ACTIVE groups only (partial unique index —
  archiving frees the name), kind CHECK department|project_team, nullable
  contact, is_active.
- group_memberships: (group_id, user_id) PK, both FKs CASCADE, role CHECK
  member|editor (C1 — no group_editor user role anywhere), added_by SET NULL
  (attribution without retention), added_at.
- documents: scope CHECK extends to org|group|private; new group_id FK
  ON DELETE RESTRICT (deleting a group with documents must be an explicit
  re-homing decision, never a silent purge); new uploaded_by FK users
  ON DELETE SET NULL; scope/owner/group CHECKs enforce org+group ownerless
  (D16/C2), private always owned. The advisory-dedupe unique extends with
  group_id so different groups may hold the same file. Existing rows are
  backfilled uploaded_by = owner_id (private docs); org rows keep NULL.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "groups",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("contact", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('department', 'project_team')", name="ck_groups_kind"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_groups_name_active", "groups", ["name"], unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "group_memberships",
        sa.Column("group_id", postgresql.UUID(), nullable=False),
        sa.Column("user_id", postgresql.UUID(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("added_by", postgresql.UUID(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('member', 'editor')", name="ck_group_memberships_role"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["added_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("group_id", "user_id"),
        sa.UniqueConstraint("group_id", "user_id", name="uq_group_memberships_group_user"),
    )

    op.add_column("documents", sa.Column("group_id", postgresql.UUID(), nullable=True))
    op.add_column("documents", sa.Column("uploaded_by", postgresql.UUID(), nullable=True))
    # attribution backfill: existing private docs were uploaded by their owner
    op.execute("UPDATE documents SET uploaded_by = owner_id WHERE owner_id IS NOT NULL")
    op.create_foreign_key(
        "fk_documents_group_id_groups", "documents", "groups", ["group_id"], ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_documents_uploaded_by_users", "documents", "users", ["uploaded_by"], ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint("ck_documents_scope", "documents", type_="check")
    op.drop_constraint("ck_documents_owner_matches_scope", "documents", type_="check")
    op.create_check_constraint(
        "ck_documents_scope", "documents",
        sa.text("scope IN ('org', 'group', 'private')"),
    )
    op.create_check_constraint(
        "ck_documents_scope_owner_group", "documents",
        sa.text(
            "(scope = 'org' AND owner_id IS NULL AND group_id IS NULL)"
            " OR (scope = 'group' AND owner_id IS NULL AND group_id IS NOT NULL)"
            " OR (scope = 'private' AND owner_id IS NOT NULL AND group_id IS NULL)"
        ),
    )
    op.drop_constraint("uq_documents_checksum_scope_owner", "documents", type_="unique")
    op.create_unique_constraint(
        "uq_documents_checksum_scope_owner_group", "documents",
        ["checksum", "scope", "owner_id", "group_id"],
    )
    op.create_index("ix_documents_group_id", "documents", ["group_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_documents_group_id", table_name="documents")
    op.drop_constraint("uq_documents_checksum_scope_owner_group", "documents", type_="unique")
    op.drop_constraint("ck_documents_scope_owner_group", "documents", type_="check")
    op.drop_constraint("ck_documents_scope", "documents", type_="check")
    op.drop_constraint("fk_documents_uploaded_by_users", "documents", type_="foreignkey")
    op.drop_constraint("fk_documents_group_id_groups", "documents", type_="foreignkey")
    # restore the B4 shapes: group-scope rows must be deleted first (they
    # cannot be represented under the old CHECKs — the downgrade refuses to
    # silently purge them)
    op.execute("DELETE FROM documents WHERE scope = 'group'")
    op.execute("UPDATE documents SET owner_id = uploaded_by WHERE scope = 'private' AND owner_id IS NULL")
    op.drop_column("documents", "uploaded_by")
    op.drop_column("documents", "group_id")
    op.create_check_constraint(
        "ck_documents_scope", "documents", sa.text("scope IN ('org', 'private')")
    )
    op.create_check_constraint(
        "ck_documents_owner_matches_scope", "documents",
        sa.text(
            "(scope = 'org' AND owner_id IS NULL)"
            " OR (scope = 'private' AND owner_id IS NOT NULL)"
        ),
    )
    op.create_unique_constraint(
        "uq_documents_checksum_scope_owner", "documents",
        ["checksum", "scope", "owner_id"],
    )
    op.drop_table("group_memberships")
    op.drop_index("uq_groups_name_active", table_name="groups")
    op.drop_table("groups")
