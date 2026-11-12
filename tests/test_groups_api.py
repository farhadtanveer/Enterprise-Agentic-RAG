"""G1.4 (docs/tasks/g1.md PART 1): admin group endpoints + member routes.

Conventions under test (§A8 + §A14.7): anonymous 401 / plain user 403 on
admin routes; EVERYTHING outside a user's audience is 404, never 403;
group delete is RESTRICTed while documents exist (409); archive =
is_active false; every change audited without content; admin SELF-JOIN is
audited and visible to that group's editors (DECISION #4); editors read
GET /api/groups/{id}/members for their OWN groups; members see only
GET /api/me/groups; admins have NO implicit access (an admin who is not a
member gets 404 on the members route like any outsider).

Written BEFORE the routes exist (red first — every probe lands on 404
"route missing" or the authz layer).
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import auth as auth_api
from app.api import conversations as conv_api
from app.api.deps import get_current_user
from app.core.db import get_db
from app.main import app as fastapi_app
from app.models.audit_event import AuditEvent
from app.models.document import Document
from app.models.group import Group
from app.models.user import User
from app.services import groups as groups_svc
from app.services import users as users_svc
from app.services.ratelimit import SlidingWindowLimiter

PEER = ("203.0.113.5", 50000)
PASSWORD = "a valid longer passphrase"


@pytest.fixture(autouse=True)
def _fresh_limiters(monkeypatch):
    monkeypatch.setattr(auth_api, "login_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(conv_api, "chat_limiter", SlidingWindowLimiter(10**9))


@pytest.fixture()
def client(db_session):
    def _override():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(fastapi_app, client=PEER)
    finally:
        fastapi_app.dependency_overrides.clear()


def make(db, username, *, role="user", **kw):
    u = users_svc.create_user(db, username, password=PASSWORD, role=role, **kw)
    db.commit()
    return u


def login(client, username, password=PASSWORD):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def uid(db, username):
    return str(db.execute(select(User.id).where(User.username == username)).scalar_one())


def gid(db, name):
    return str(db.execute(select(Group.id).where(Group.name == name)).scalar_one())


def _events(db, event_type):
    return db.execute(
        select(AuditEvent).where(AuditEvent.event_type == event_type)
    ).scalars().all()


# --- authz matrix (g1.md minimum) -----------------------------------------------


def test_admin_group_routes_anon_401_user_403(client, db_session):
    """g1.md minimum: group admin routes — anonymous 401, user 403."""
    make(db_session, "plain-g1")
    db_session.commit()
    probes = [
        ("GET", "/api/admin/groups", None),
        ("POST", "/api/admin/groups", {"name": "x"}),
        ("PATCH", "/api/admin/groups/00000000-0000-0000-0000-000000000000", {}),
        ("DELETE", "/api/admin/groups/00000000-0000-0000-0000-000000000000?confirm=true", None),
        ("POST", "/api/admin/groups/00000000-0000-0000-0000-000000000000/members", {}),
        ("PATCH", "/api/admin/groups/00000000-0000-0000-0000-000000000000/members/"
                  "00000000-0000-0000-0000-000000000000", {}),
        ("DELETE", "/api/admin/groups/00000000-0000-0000-0000-000000000000/members/"
                   "00000000-0000-0000-0000-000000000000", None),
    ]
    for method, path, body in probes:
        assert client.request(method, path, json=body).status_code == 401, f"anon {method} {path}"
    csrf = login(client, "plain-g1")
    for method, path, body in probes:
        r = client.request(method, path, json=body, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 403, f"plain user {method} {path}: {r.status_code} {r.text[:80]}"


# --- group CRUD -------------------------------------------------------------------


def test_group_create_patch_archive_delete(client, db_session):
    csrf = login(client, *(make(db_session, "crud-admin", role="admin").username,))
    r = client.post("/api/admin/groups", json={"name": "Finance", "contact": "fin-alias"},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Finance" and body["is_active"] is True
    g = gid(db_session, "Finance")

    # duplicate ACTIVE name → 409; bad kind → 422
    r = client.post("/api/admin/groups", json={"name": "Finance"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409
    r = client.post("/api/admin/groups", json={"name": "Other", "kind": "squad"},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 422

    # archive (is_active false) — memberships kept
    r = client.patch(f"/api/admin/groups/{g}", json={"is_active": False},
                     headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert _events(db_session, "group_updated")

    # archived freed the name → re-creatable
    r = client.post("/api/admin/groups", json={"name": "Finance"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201

    # delete: no documents → 200; unknown → 404
    r = client.delete(f"/api/admin/groups/{g}?confirm=true", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    r = client.delete(f"/api/admin/groups/{g}?confirm=true", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 404
    assert _events(db_session, "group_deleted")


def test_group_delete_confirm_required_and_restrict_with_documents(client, db_session):
    csrf = login(client, make(db_session, "del-admin", role="admin").username)
    r = client.post("/api/admin/groups", json={"name": "doc-g"}, headers={"X-CSRF-Token": csrf})
    g = gid(db_session, "doc-g")
    db_session.add(Document(scope="group", group_id=g, display_name="d.txt",
                            storage_path="uploads/x/d.txt", checksum="x" * 64,
                            size_bytes=1, status="indexed"))
    db_session.commit()

    r = client.delete(f"/api/admin/groups/{g}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 422  # confirm gate
    r = client.delete(f"/api/admin/groups/{g}?confirm=true", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409  # RESTRICTed while documents exist
    db_session.expire_all()
    assert db_session.execute(
        select(func.count()).select_from(Group).where(Group.id == g)
    ).scalar_one() == 1


def test_group_listing_includes_member_count(client, db_session):
    csrf = login(client, make(db_session, "list-admin", role="admin").username)
    r = client.post("/api/admin/groups", json={"name": "list-g"}, headers={"X-CSRF-Token": csrf})
    g = gid(db_session, "list-g")
    u = make(db_session, "list-member")
    client.post(f"/api/admin/groups/{g}/members", json={"username": "list-member"},
                headers={"X-CSRF-Token": csrf})
    r = client.get("/api/admin/groups", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    row = next(x for x in r.json()["items"] if x["id"] == g)
    assert row["member_count"] == 1


# --- membership management ----------------------------------------------------------


def test_membership_add_duplicate_and_unknowns(client, db_session):
    csrf = login(client, make(db_session, "mem-admin", role="admin").username)
    make(db_session, "mem-user")
    r = client.post("/api/admin/groups", json={"name": "mem-g"}, headers={"X-CSRF-Token": csrf})
    g = gid(db_session, "mem-g")

    r = client.post(f"/api/admin/groups/{g}/members",
                    json={"username": "mem-user", "role": "editor"},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "editor"

    r = client.post(f"/api/admin/groups/{g}/members", json={"username": "mem-user"},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409
    r = client.post(f"/api/admin/groups/{g}/members", json={"username": "ghost"},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 404
    r = client.post(f"/api/admin/groups/{g}/members",
                    json={"user_id": uid(db_session, "mem-user")},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409  # already a member (user_id path works)
    r = client.post(f"/api/admin/groups/{g}/members", json={},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 422  # neither username nor user_id
    assert _events(db_session, "membership_added")


def test_membership_role_change_and_remove(client, db_session):
    csrf = login(client, make(db_session, "role-admin", role="admin").username)
    make(db_session, "role-user")
    client.post("/api/admin/groups", json={"name": "role-g"}, headers={"X-CSRF-Token": csrf})
    g = gid(db_session, "role-g")
    client.post(f"/api/admin/groups/{g}/members", json={"username": "role-user"},
                headers={"X-CSRF-Token": csrf})
    u = uid(db_session, "role-user")

    r = client.patch(f"/api/admin/groups/{g}/members/{u}", json={"role": "editor"},
                     headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["role"] == "editor"
    r = client.patch(f"/api/admin/groups/{g}/members/{u}", json={"role": "editor"},
                     headers={"X-CSRF-Token": csrf})
    assert r.status_code == 422  # no change
    r = client.patch(f"/api/admin/groups/{g}/members/{u}", json={"role": "owner"},
                     headers={"X-CSRF-Token": csrf})
    assert r.status_code == 422
    r = client.delete(f"/api/admin/groups/{g}/members/{u}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    r = client.delete(f"/api/admin/groups/{g}/members/{u}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 404
    assert _events(db_session, "membership_role_changed")
    assert _events(db_session, "membership_removed")


def test_admin_self_join_audited_and_visible_to_editors(client, db_session):
    """DECISION #4: an admin adding THEMSELVES is audited (actor==target)
    and the join shows up in the group's membership list AS SEEN BY AN
    EDITOR of that group."""
    editor = make(db_session, "sj-editor")
    admin = make(db_session, "sj-admin", role="admin")
    acsrf = login(client, "sj-admin")
    r = client.post("/api/admin/groups", json={"name": "sj-g"}, headers={"X-CSRF-Token": acsrf})
    g = gid(db_session, "sj-g")

    r = client.post(f"/api/admin/groups/{g}/members", json={"username": "sj-admin"},
                    headers={"X-CSRF-Token": acsrf})
    assert r.status_code == 201, r.text

    events = _events(db_session, "membership_added")
    assert any(e.actor_user_id == admin.id and str(e.object_id) == str(admin.id)
               for e in events), "self-join not audited with actor==target"

    # an EDITOR of the group sees the admin in the list (user-facing route;
    # separate client so the admin jar survives)
    client.post(f"/api/admin/groups/{g}/members", json={"username": "sj-editor", "role": "editor"},
                headers={"X-CSRF-Token": acsrf})
    e = TestClient(fastapi_app, client=PEER)
    ecsrf = login(e, "sj-editor")
    client.post(f"/api/admin/groups/{g}/members", json={"username": "sj-editor", "role": "editor"},
                headers={"X-CSRF-Token": acsrf})
    r = e.get(f"/api/groups/{g}/members")
    assert r.status_code == 200, r.text
    usernames = {m["username"] for m in r.json()["items"]}
    assert {"sj-editor", "sj-admin"} <= usernames
    roles = {m["username"]: m["role"] for m in r.json()["items"]}
    assert roles["sj-admin"] == "member"


# --- user-facing routes ---------------------------------------------------------------


def test_me_groups_lists_active_memberships_only(client, db_session):
    make(db_session, "me-u")
    make(db_session, "me-admin", role="admin")
    acsrf = login(client, "me-admin")
    for name in ("me-g1", "me-g2", "me-g3"):
        client.post("/api/admin/groups", json={"name": name}, headers={"X-CSRF-Token": acsrf})
    g1, g2, g3 = (gid(db_session, n) for n in ("me-g1", "me-g2", "me-g3"))
    client.post(f"/api/admin/groups/{g1}/members", json={"username": "me-u"},
                headers={"X-CSRF-Token": acsrf})
    client.post(f"/api/admin/groups/{g2}/members", json={"username": "me-u", "role": "editor"},
                headers={"X-CSRF-Token": acsrf})
    client.post(f"/api/admin/groups/{g3}/members", json={"username": "me-u"},
                headers={"X-CSRF-Token": acsrf})
    # archive g3 — it must leave /api/me/groups (resolver mirror)
    client.patch(f"/api/admin/groups/{g3}", json={"is_active": False},
                 headers={"X-CSRF-Token": acsrf})

    # the MEMBER probes /api/me/groups with their OWN client (separate jar)
    me = TestClient(fastapi_app, client=PEER)
    csrf = login(me, "me-u")
    r = me.get("/api/me/groups", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    ids = {m["group_id"] for m in r.json()["items"]}
    assert ids == {g1, g2}, "archived group must not appear in /api/me/groups"
    roles = {m["group_id"]: m["role"] for m in r.json()["items"]}
    assert roles[g2] == "editor"

    # a user with no memberships sees an empty list
    make(db_session, "me-u2")
    c2 = TestClient(fastapi_app, client=PEER)
    csrf2 = login(c2, "me-u2")
    r = c2.get("/api/me/groups", headers={"X-CSRF-Token": csrf2})
    assert r.status_code == 200 and r.json()["items"] == []


def test_members_route_404_outsider_403_member_editor_ok(client, db_session):
    make(db_session, "m-editor")
    make(db_session, "m-member")
    make(db_session, "m-outsider")
    admin = make(db_session, "m-admin", role="admin")
    acsrf = login(client, "m-admin")
    client.post("/api/admin/groups", json={"name": "m-g"}, headers={"X-CSRF-Token": acsrf})
    g = gid(db_session, "m-g")
    client.post(f"/api/admin/groups/{g}/members",
                json={"username": "m-editor", "role": "editor"}, headers={"X-CSRF-Token": acsrf})
    client.post(f"/api/admin/groups/{g}/members", json={"username": "m-member"},
                headers={"X-CSRF-Token": acsrf})

    # editor of THAT group → 200
    e = TestClient(fastapi_app, client=PEER)
    ecsrf = login(e, "m-editor")
    r = e.get(f"/api/groups/{g}/members", headers={"X-CSRF-Token": ecsrf})
    assert r.status_code == 200

    # plain member → 403 (they know the group exists)
    mm = TestClient(fastapi_app, client=PEER)
    mcsrf = login(mm, "m-member")
    r = mm.get(f"/api/groups/{g}/members", headers={"X-CSRF-Token": mcsrf})
    assert r.status_code == 403

    # outsider → 404 (indistinguishable from nonexistent)
    o = TestClient(fastapi_app, client=PEER)
    ocsrf = login(o, "m-outsider")
    r = o.get(f"/api/groups/{g}/members", headers={"X-CSRF-Token": ocsrf})
    assert r.status_code == 404

    # ADMIN who is not a member → 404 (no implicit access, DECISION #4)
    r = client.get(f"/api/groups/{g}/members", headers={"X-CSRF-Token": acsrf})
    assert r.status_code == 404

    # unknown group id → 404 for everyone
    r = e.get("/api/groups/00000000-0000-0000-0000-000000000000/members",
              headers={"X-CSRF-Token": ecsrf})
    assert r.status_code == 404


# --- C2 + audit hygiene at the endpoint level ------------------------------------------


def test_user_delete_leaves_group_documents_intact_uploaded_by_nulls(client, db_session, monkeypatch):
    """C2 at the API level: DELETE /api/admin/users/{id} removes ONLY the
    user's private docs; their uploaded group docs survive with uploaded_by
    NULL; the group itself is untouched. Vector deletion is faked (no keys
    offline) — the same pattern as the B6 admin tests."""
    deleted_vectors = []
    import app.api.admin as admin_module

    monkeypatch.setattr(admin_module, "delete_document_vectors",
                        lambda doc_id: deleted_vectors.append(doc_id))
    acsrf = login(client, make(db_session, "c2-admin", role="admin").username)
    victim = make(db_session, "c2-victim")
    client.post("/api/admin/groups", json={"name": "c2-g"}, headers={"X-CSRF-Token": acsrf})
    g = gid(db_session, "c2-g")
    gdoc = Document(scope="group", group_id=g, uploaded_by=victim.id,
                    display_name="team.txt", storage_path="uploads/x/t.txt",
                    checksum="t" * 64, size_bytes=1, status="indexed")
    orgdoc = Document(scope="org", uploaded_by=victim.id, display_name="org.txt",
                      storage_path="uploads/x/o.txt", checksum="o" * 64,
                      size_bytes=1, status="indexed")
    privdoc = Document(scope="private", owner_id=victim.id, display_name="mine.txt",
                       storage_path="uploads/x/m.txt", checksum="m" * 64,
                       size_bytes=1, status="indexed")
    db_session.add_all([gdoc, orgdoc, privdoc])
    db_session.commit()
    # capture ids BEFORE any deletion: after expire_all(), even `.id` on an
    # expired instance whose row the endpoint deleted raises
    # ObjectDeletedError (same identity-map rule as tests/test_groups_schema.py)
    priv_id, gdoc_id, org_id = privdoc.id, gdoc.id, orgdoc.id

    r = client.delete(f"/api/admin/users/{victim.id}?confirm=true", headers={"X-CSRF-Token": acsrf})
    assert r.status_code == 200, r.text
    assert deleted_vectors == [str(priv_id)]  # §A9.1: ONLY the private doc's vectors

    db_session.expire_all()

    def _row(doc_id):
        # column-level SELECT — bypasses the identity map, which still holds
        # rows the endpoint deleted/updated behind the session
        return db_session.execute(
            select(Document.uploaded_by, Document.group_id, Document.owner_id)
            .where(Document.id == doc_id)
        ).first()

    assert _row(priv_id) is None  # private died with owner
    live_g = _row(gdoc_id)
    live_o = _row(org_id)
    assert live_g is not None and live_g.uploaded_by is None
    assert str(live_g.group_id) == g
    assert live_o is not None and live_o.uploaded_by is None
    assert db_session.get(Group, g) is not None


def test_membership_audit_rows_carry_no_content(client, db_session):
    acsrf = login(client, make(db_session, "audit-admin", role="admin").username)
    make(db_session, "audit-user")
    client.post("/api/admin/groups", json={"name": "audit-g"}, headers={"X-CSRF-Token": acsrf})
    g = gid(db_session, "audit-g")
    client.post(f"/api/admin/groups/{g}/members", json={"username": "audit-user", "role": "editor"},
                headers={"X-CSRF-Token": acsrf})
    for e in _events(db_session, "membership_added"):
        blob = str(e.details) + str(e.event_type)
        assert "audit-user" not in blob  # usernames of members are ids in details
        assert e.details.get("role") == "editor"
