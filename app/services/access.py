"""The AccessResolver (§A14.3, G1.2) — the ONE seam where retrieval filters
and listing checks are built from authenticated identity.

Fail-closed carries over from §A7 unchanged: a vector with no read_access
key matches nothing; get_retriever refuses a missing filter; the legacy
chat keeps the static ORG_ONLY_FILTER. Token grammar is STRICT from day
one (DECISION #9 verified 2026-11-12: rag-v2 holds zero bare-UUID vectors):
"org", "group:<group_id>", "user:<user_id>" — nothing else is emitted or
honored.

EVERYTHING is recomputed from the database on every call — never cached on
the session row, never snapshotted at login — so a grant, revoke, disable
or archive applies to the user's very next request (proven by
tests/security/test_access_resolver.py).
"""
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models.document import Document
from app.models.group import Group, GroupMembership
from app.models.user import User


class AccessResolver(Protocol):
    """Backend-agnostic seam: the same rules run against Pinecone now and
    pgvector later (§A14.3). Implementations receive the caller's session."""

    def audiences_for(self, user: User) -> list[str]:
        """["org"] + ["group:<id>" for each ACTIVE membership] + ["user:<self>"]
        — rebuilt from the DB on every call."""

    def filter_for(self, user: User) -> dict:
        """{"read_access": {"$in": audiences_for(user)}} — the SAME single
        $in condition as today (Pinecone array-any-match semantics)."""

    def can_read(self, user: User, doc: Document) -> bool:
        """Listing/detail checks in Postgres terms (scope, group_id,
        owner_id) — mirrors what the vector filter would match, so listings
        and retrieval can never disagree."""

    def is_editor(self, user: User, group_id: UUID) -> bool:
        """True iff the user's membership in THIS group has role='editor'
        (C1 — editor-ness is per-group, off the membership row)."""


class DbAccessResolver:
    """The Pinecone-era implementation. Stateless per call; holds only the
    caller's DB session, so stale-filter windows are impossible."""

    def __init__(self, db: DBSession):
        self.db = db

    def audiences_for(self, user: User) -> list[str]:
        group_ids = self.db.execute(
            select(GroupMembership.group_id)
            .join(Group, Group.id == GroupMembership.group_id)
            .where(
                GroupMembership.user_id == user.id,
                Group.is_active.is_(True),  # archived groups are in NO audience
            )
            .order_by(GroupMembership.added_at, GroupMembership.group_id)
        ).scalars().all()
        return ["org"] + [f"group:{gid}" for gid in group_ids] + [f"user:{user.id}"]

    def filter_for(self, user: User) -> dict:
        return {"read_access": {"$in": self.audiences_for(user)}}

    def can_read(self, user: User, doc: Document) -> bool:
        if doc.scope == "org":
            return True
        if doc.scope == "private":
            return doc.owner_id == user.id
        if doc.scope == "group":
            if doc.group_id is None:
                return False  # malformed row — fail closed
            group = self.db.get(Group, doc.group_id)
            if group is None or not group.is_active:
                return False  # dangling/archived — fail closed
            membership = self.db.execute(
                select(GroupMembership.user_id)
                .where(
                    GroupMembership.group_id == doc.group_id,
                    GroupMembership.user_id == user.id,
                )
                .limit(1)
            ).scalar_one_or_none()
            return membership is not None
        return False  # unknown scope — fail closed

    def is_editor(self, user: User, group_id: UUID) -> bool:
        membership = self.db.execute(
            select(GroupMembership.role)
            .where(
                GroupMembership.group_id == group_id,
                GroupMembership.user_id == user.id,
                GroupMembership.role == "editor",
            )
            .limit(1)
        ).scalar_one_or_none()
        return membership is not None
