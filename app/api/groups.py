"""Group surfaces for regular members (§A14.7, G1.4).

- GET /api/me/groups — the caller's ACTIVE memberships (the only group
  listing a regular member gets).
- GET /api/groups/{group_id}/members — editors of THAT group only.
  Plain member → 403 (they know the group exists); outsider (and an admin
  who is not a member — DECISION #4, no implicit access) → 404,
  indistinguishable from nonexistent (§A8 404-not-403).
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_user
from app.core.db import get_db
from app.models.group import Group
from app.models.user import User
from app.services import groups as groups_svc
from app.services.access import DbAccessResolver

router = APIRouter(prefix="/api", tags=["groups"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _group_or_404(db: DBSession, group_id: UUID) -> Group:
    group = db.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Not found")
    return group


@router.get("/me/groups")
def my_groups(
    response: Response,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    memberships = groups_svc.groups_for_user(db, user.id)
    items = []
    for m in memberships:
        group = db.get(Group, m.group_id)
        items.append({
            "group_id": str(m.group_id),
            "name": group.name,
            "kind": group.kind,
            "role": m.role,
            "added_at": m.added_at.isoformat(),
        })
    _no_store(response)
    return {"items": items, "total": len(items)}


@router.get("/groups/{group_id}/members")
def group_members(
    group_id: UUID,
    response: Response,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    group = _group_or_404(db, group_id)
    resolver = DbAccessResolver(db)
    membership = groups_svc.get_membership(db, group.id, user.id)
    if not resolver.is_editor(user, group.id):
        if membership is None:
            # outsider OR admin-without-membership: no group exists for them
            raise HTTPException(status_code=404, detail="Not found")
        raise HTTPException(status_code=403, detail="Editor role required")
    items = []
    for m in groups_svc.members_of(db, group.id):
        member_user = db.get(User, m.user_id)
        items.append({
            "user_id": str(m.user_id),
            "username": member_user.username if member_user else None,
            "role": m.role,
            "added_at": m.added_at.isoformat(),
        })
    _no_store(response)
    return {"items": items, "total": len(items)}
