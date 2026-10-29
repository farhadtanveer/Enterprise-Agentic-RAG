"""users, sessions, login_throttle, audit_events (B2)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29

The multi-user auth schema (docs/backend-design.md §A4/§A5). All IDs are
uuidv7() Postgres-side defaults; timestamps timestamptz; inet for IPs; CHECK
constraints where the app makes promises (roles, outcomes, lowercase
usernames, sha256 token hashes).

audit_events lands with B2 (auth lifecycle events + the §A11 privacy tests
need it) even though §A12 files the writer expansion under B5.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("username = lower(username)", name="ck_users_username_lowercase"),
        sa.CheckConstraint("role IN ('admin', 'user')", name="ck_users_role"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name=op.f("users_username_key")),
    )
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_sessions_token_hash_sha256"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name=op.f("sessions_token_hash_key")),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)
    op.create_index(op.f("ix_sessions_expires_at"), "sessions", ["expires_at"], unique=False)
    op.create_table(
        "login_throttle",
        sa.Column("username_hash", sa.Text(), nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=False),
        sa.Column("fail_count", sa.Integer(), nullable=False),
        sa.Column("throttle_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("username_hash", "ip"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=True),
        sa.Column("object_id", postgresql.UUID(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("route", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("tokens_total", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.CheckConstraint("outcome IN ('ok', 'denied', 'error')", name="ck_audit_events_outcome"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_events_created_at"), "audit_events", ["created_at"], unique=False)
    op.create_index(op.f("ix_audit_events_actor_user_id"), "audit_events", ["actor_user_id"], unique=False)
    op.create_index(
        op.f("ix_audit_events_event_type"), "audit_events", ["event_type"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_events_event_type"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_actor_user_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_created_at"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("login_throttle")
    op.drop_index(op.f("ix_sessions_expires_at"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("users")
