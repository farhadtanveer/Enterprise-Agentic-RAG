"""Groups and memberships (§A14.4 M1 / G1.1, docs/tasks/g1.md).

C1: editor-ness lives on the MEMBERSHIP (role member|editor), never on
users.role — which stays IN ('user','admin') forever. Archiving
(is_active=False) keeps memberships but removes the group from every
audience: the AccessResolver only counts ACTIVE memberships, so archived
group documents become retrievable by no one until the group is re-activated
or its documents are deleted/re-homed (explicit operator decision).
`name` is unique among ACTIVE groups only (partial unique index) — an
archived group's name can be reused by a new active group.
"""
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (
        CheckConstraint("kind IN ('department', 'project_team')", name="ck_groups_kind"),
        Index(
            "uq_groups_name_active", "name", unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, server_default=func.uuidv7())
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), default="department")
    # nullable free text (an alias/room/mailbox — NOT a user FK, §A14.4);
    # DECISION #10 surface for insufficient-evidence answers lands in G2.
    contact: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Group {self.name!r} active={self.is_active}>"


class GroupMembership(Base):
    __tablename__ = "group_memberships"
    __table_args__ = (
        CheckConstraint("role IN ('member', 'editor')", name="ck_group_memberships_role"),
        UniqueConstraint("group_id", "user_id", name="uq_group_memberships_group_user"),
    )

    group_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16), default="member")
    # attribution without retention: the adder is recorded, but deleting the
    # adder never deletes the membership (SET NULL — same philosophy as
    # documents.uploaded_by, C2)
    added_by: Mapped[UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL")
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<GroupMembership group={self.group_id} user={self.user_id} role={self.role}>"
