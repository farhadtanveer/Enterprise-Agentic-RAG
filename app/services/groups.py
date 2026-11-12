"""Groups service (§A14, G1.3/G1.4) — shared by the operator CLIs and the
admin endpoints. Callers own commit (§A8), like users.py.

Every change writes an audit event carrying ids/roles ONLY — never
content. actor_user_id is the acting admin for API calls, None for the
operator CLI (same convention as the B2 recovery CLIs).
"""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models.group import Group, GroupMembership
from app.models.user import User
from app.services.audit_events import record_event

GROUP_KINDS = ("department", "project_team")
MEMBER_ROLES = ("member", "editor")


def get_active_by_name(db: DBSession, name: str) -> Group | None:
    return db.execute(
        select(Group).where(Group.name == name, Group.is_active.is_(True))
    ).scalar_one_or_none()


def get_group(db: DBSession, group_id: UUID) -> Group | None:
    return db.get(Group, group_id)


def create_group(db: DBSession, name: str, *, kind: str = "department",
                 contact: str | None = None, actor_user_id: UUID | None = None) -> Group:
    name = (name or "").strip()
    if not name:
        raise ValueError("group name must not be empty")
    if kind not in GROUP_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(GROUP_KINDS)}")
    existing = db.execute(
        select(Group.id).where(Group.name == name, Group.is_active.is_(True))
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"an active group named '{name}' already exists")
    group = Group(name=name, kind=kind, contact=contact)
    db.add(group)
    db.flush()
    record_event(
        db, "group_created", actor_user_id=actor_user_id,
        object_type="group", object_id=group.id, outcome="ok",
        details={"kind": kind, "has_contact": contact is not None},
    )
    return group


def update_group(db: DBSession, group: Group, *, kind: str | None = None,
                 contact: str | None = None, is_active: bool | None = None,
                 actor_user_id: UUID | None = None) -> Group:
    """Archive = is_active False (memberships kept; the group leaves every
    audience at the next request — §A14.3). Archived groups free their name."""
    changed: dict = {}
    if kind is not None and kind != group.kind:
        if kind not in GROUP_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(GROUP_KINDS)}")
        group.kind = kind
        changed["kind"] = kind
    if contact is not None and contact != group.contact:
        group.contact = contact
        changed["contact_set"] = True
    if is_active is not None and is_active != group.is_active:
        group.is_active = is_active
        changed["is_active"] = is_active
    if not changed:
        raise ValueError("No changes supplied")
    db.flush()
    record_event(
        db, "group_updated", actor_user_id=actor_user_id,
        object_type="group", object_id=group.id, outcome="ok", details=changed,
    )
    return group


def delete_group(db: DBSession, group: Group, *, actor_user_id: UUID | None = None) -> None:
    """REFUSED while documents reference the group (FK RESTRICT — deleting
    with documents must be an explicit re-homing decision, §A14.4)."""
    from sqlalchemy import func
    from app.models.document import Document

    doc_count = db.execute(
        select(func.count()).select_from(Document).where(Document.group_id == group.id)
    ).scalar_one()
    if doc_count:
        raise ValueError(
            f"group still holds {doc_count} document(s) — delete or re-home them first"
        )
    record_event(
        db, "group_deleted", actor_user_id=actor_user_id,
        object_type="group", object_id=group.id, outcome="ok", details={},
    )
    db.delete(group)
    db.flush()


def get_membership(db: DBSession, group_id: UUID, user_id: UUID) -> GroupMembership | None:
    return db.execute(
        select(GroupMembership)
        .where(GroupMembership.group_id == group_id, GroupMembership.user_id == user_id)
    ).scalar_one_or_none()


def add_member(db: DBSession, group: Group, user: User, *, role: str = "member",
               added_by: UUID | None = None) -> GroupMembership:
    if role not in MEMBER_ROLES:
        raise ValueError(f"role must be one of: {', '.join(MEMBER_ROLES)}")
    if get_membership(db, group.id, user.id) is not None:
        raise ValueError(f"{user.username} is already a member of {group.name}")
    membership = GroupMembership(group_id=group.id, user_id=user.id, role=role, added_by=added_by)
    db.add(membership)
    db.flush()
    record_event(
        db, "membership_added", actor_user_id=added_by,
        object_type="membership", object_id=user.id, outcome="ok",
        details={"group_id": str(group.id), "role": role,
                 "self_join": added_by is not None and added_by == user.id},
    )
    return membership


def remove_member(db: DBSession, group: Group, user: User, *,
                  actor_user_id: UUID | None = None) -> None:
    membership = get_membership(db, group.id, user.id)
    if membership is None:
        raise ValueError(f"{user.username} is not a member of {group.name}")
    db.delete(membership)
    db.flush()
    record_event(
        db, "membership_removed", actor_user_id=actor_user_id,
        object_type="membership", object_id=user.id, outcome="ok",
        details={"group_id": str(group.id)},
    )


def set_member_role(db: DBSession, group: Group, user: User, *, role: str,
                    actor_user_id: UUID | None = None) -> GroupMembership:
    if role not in MEMBER_ROLES:
        raise ValueError(f"role must be one of: {', '.join(MEMBER_ROLES)}")
    membership = get_membership(db, group.id, user.id)
    if membership is None:
        raise ValueError(f"{user.username} is not a member of {group.name}")
    if membership.role == role:
        raise ValueError("No changes supplied")
    membership.role = role
    db.flush()
    record_event(
        db, "membership_role_changed", actor_user_id=actor_user_id,
        object_type="membership", object_id=user.id, outcome="ok",
        details={"group_id": str(group.id), "role": role},
    )
    return membership


def groups_for_user(db: DBSession, user_id: UUID) -> list[GroupMembership]:
    """ACTIVE memberships only — mirrors the resolver's audience rule."""
    return list(db.execute(
        select(GroupMembership)
        .join(Group, Group.id == GroupMembership.group_id)
        .where(GroupMembership.user_id == user_id, Group.is_active.is_(True))
        .order_by(GroupMembership.added_at, GroupMembership.group_id)
    ).scalars().all())


def members_of(db: DBSession, group_id: UUID) -> list[GroupMembership]:
    return list(db.execute(
        select(GroupMembership)
        .where(GroupMembership.group_id == group_id)
        .order_by(GroupMembership.added_at, GroupMembership.user_id)
    ).scalars().all())
