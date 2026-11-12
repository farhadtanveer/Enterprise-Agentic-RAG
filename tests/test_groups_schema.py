"""G1.1 (docs/tasks/g1.md PART 1): the groups schema — written BEFORE the
models/migration, so red-first is the point.

Covers the g1.md minimum test list at the schema level:
- documents CHECK constraints reject EVERY invalid scope/owner/group combo;
- groups.kind CHECK; name unique among ACTIVE groups only (archived frees
  the name — partial unique index);
- group_memberships.role CHECK (C1: member|editor only — 'group_editor' is
  NOT a thing); unique (group_id, user_id);
- users.role CHECK untouched — role='group_editor' is rejected by the DB;
- delete consistency: group delete RESTRICTed while documents reference it
  (memberships CASCADE); user delete cascades memberships + private docs,
  NULLs uploaded_by, and NEVER touches group/org documents (C2).
"""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.document import Document
from app.models.group import Group, GroupMembership
from app.models.user import User
from app.services import users as users_svc

PASSWORD = "a valid longer passphrase"


def _doc(**kw):
    base = dict(
        display_name="note.txt", storage_path="uploads/x/note.txt",
        checksum="c" * 64, size_bytes=10, status="indexed",
    )
    base.update(kw)
    return Document(**base)


def _group(db, name, *, kind="department", is_active=True):
    g = Group(name=name, kind=kind, is_active=is_active)
    db.add(g)
    db.flush()
    return g


def _user(db, name, *, role="user"):
    u = users_svc.create_user(db, name, password=PASSWORD, role=role)
    db.commit()
    return u


# --- documents CHECKs --------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        # the three valid shapes
        ("org", None, None, True),
        ("private", "user", None, True),
        ("group", None, "group", True),
        # every invalid combo must be REJECTED by the schema
        ("org", "user", None, False),       # org docs are ownerless (D16)
        ("org", None, "group", False),      # org docs have no group
        ("org", "user", "group", False),
        ("group", "user", "group", False),  # group docs are ownerless (C2)
        ("group", None, None, False),       # group scope ⇒ group_id NOT NULL
        ("private", None, None, False),     # private ⇒ owner_id NOT NULL
        ("private", "user", "group", False),
    ],
)
def test_document_scope_owner_group_check(db_session, case):
    scope, owner, group_id, valid = case
    user = _user(db_session, "combo-owner") if owner == "user" else None
    group = _group(db_session, "combo-g") if group_id == "group" else None
    row = _doc(
        checksum="c" * 64,  # truncate-per-test: no cross-case collision possible
        scope=scope,
        owner_id=user.id if user else None,
        group_id=group.id if group else None,
    )
    db_session.add(row)
    if valid:
        db_session.flush()  # no raise
        db_session.rollback()
    else:
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


def test_document_scope_check_rejects_unknown_scope(db_session):
    db_session.add(_doc(scope="secret"))  # fits varchar(8); CHECK must reject
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# --- groups + memberships constraints ----------------------------------------------


def test_group_kind_check_rejects_unknown_kind(db_session):
    db_session.add(Group(name="g", kind="squad"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_group_name_unique_among_active_only(db_session):
    _group(db_session, "finance")
    db_session.add(Group(name="finance", kind="department"))  # second ACTIVE
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
    # archived groups do NOT hold the name
    archived = _group(db_session, "ops", is_active=False)
    assert archived.is_active is False
    reborn = _group(db_session, "ops")  # same name, active — allowed
    assert reborn.name == "ops"


def test_membership_role_check_c1_no_group_editor_role(db_session):
    g = _group(db_session, "role-g")
    u = _user(db_session, "role-u")
    db_session.add(GroupMembership(group_id=g.id, user_id=u.id, role="group_editor"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
    db_session.add(GroupMembership(group_id=g.id, user_id=u.id, role="owner"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
    # the two real roles are accepted
    db_session.add(GroupMembership(group_id=g.id, user_id=u.id, role="member"))
    db_session.flush()
    db_session.add(GroupMembership(group_id=g.id, user_id=_user(db_session, "role-u2").id, role="editor"))
    db_session.flush()


def test_membership_unique_per_group_user(db_session):
    g = _group(db_session, "dup-g")
    u = _user(db_session, "dup-u")
    db_session.add(GroupMembership(group_id=g.id, user_id=u.id, role="member"))
    db_session.flush()
    db_session.add(GroupMembership(group_id=g.id, user_id=u.id, role="editor"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_users_role_check_unchanged_rejects_group_editor(db_session):
    """C1: users.role stays IN ('user','admin') FOREVER — the DB itself must
    reject the v1 'group_editor' role."""
    db_session.add(User(
        username="badrole", password_hash="x", role="group_editor",
    ))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# --- delete consistency (C2 + RESTRICT) ---------------------------------------------


def test_group_delete_restricted_while_documents_exist(db_session):
    g = _group(db_session, "restrict-g")
    u = _user(db_session, "restrict-u")
    db_session.add(GroupMembership(group_id=g.id, user_id=u.id, role="member"))
    doc = _doc(scope="group", group_id=g.id, checksum="r" * 64)
    db_session.add(doc)
    db_session.commit()

    db_session.delete(g)
    with pytest.raises(IntegrityError):  # FK ON DELETE RESTRICT
        db_session.commit()
    db_session.rollback()

    # once the document is gone the group deletes and memberships cascade
    db_session.delete(db_session.get(Document, doc.id))
    db_session.commit()
    db_session.delete(db_session.get(Group, g.id))
    db_session.commit()
    assert db_session.execute(
        select(GroupMembership).where(GroupMembership.group_id == g.id)
    ).scalars().first() is None


def test_user_delete_c2_group_and_org_docs_survive_uploaded_by_nulls(db_session):
    g = _group(db_session, "c2-g")
    u = _user(db_session, "c2-u")
    other = _user(db_session, "c2-other")
    db_session.add(GroupMembership(group_id=g.id, user_id=u.id, role="editor"))
    private = _doc(scope="private", owner_id=u.id, checksum="p" * 64)
    group_doc = _doc(scope="group", group_id=g.id, uploaded_by=u.id, checksum="g" * 64)
    org_doc = _doc(scope="org", uploaded_by=u.id, checksum="o" * 64)
    db_session.add_all([private, group_doc, org_doc])
    db_session.commit()

    db_session.delete(db_session.get(User, u.id))
    db_session.commit()

    assert db_session.get(User, u.id) is None
    # membership CASCADEd away
    assert db_session.execute(
        select(GroupMembership).where(GroupMembership.user_id == u.id)
    ).scalars().first() is None

    def _row(doc_id):
        # column-level SELECT — deliberately bypasses the identity map,
        # which still holds rows the DB deleted/updated behind the session
        # (FK CASCADE / SET NULL are DB-side effects)
        return db_session.execute(
            select(Document.uploaded_by, Document.group_id).where(Document.id == doc_id)
        ).first()

    # ONLY the private doc died (owner FK CASCADE, existing 0004 semantics)
    assert _row(private.id) is None
    # group + org docs survive, their uploaded_by attribution goes NULL (C2)
    surviving_group = _row(group_doc.id)
    surviving_org = _row(org_doc.id)
    assert surviving_group is not None and surviving_group.uploaded_by is None
    assert surviving_group.group_id == g.id
    assert surviving_org is not None and surviving_org.uploaded_by is None
    # other users' memberships are untouched
    assert db_session.execute(
        select(GroupMembership).where(GroupMembership.user_id == other.id)
    ).scalars().first() is None
