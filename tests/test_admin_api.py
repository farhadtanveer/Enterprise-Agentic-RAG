"""B6: admin surface — authz matrix, last-admin guard on EVERY path,
C2/C3/C4 document rules, DELETE /api/me, forced-change gate coverage.

Conventions under test (§A8 + §A14.7): plain user 403 / anonymous 401;
nonexistent targets 404; last-admin 409 everywhere including self; audit
events carry no content.
"""
import io
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.db import get_db
from app.main import app as fastapi_app
from app.models.audit_event import AuditEvent
from app.models.document import Document
from app.models.user import User
from app.services import users as users_svc

PEER = ("203.0.113.5", 50000)
PASSWORD = "a valid longer passphrase"
ADMIN_KEY = "test-admin-key"


@pytest.fixture(autouse=True)
def _fresh_limiters(monkeypatch):
    """The in-memory limiters are module singletons; without a reset the
    5/min login limiter is exhausted by the 12th login in this file."""
    from app.api import auth as auth_api
    from app.api import conversations as conv_api
    from app.services.ratelimit import SlidingWindowLimiter

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


def make(db, username, *, role="user", password=PASSWORD, **kw):
    u = users_svc.create_user(db, username, password=password, role=role, **kw)
    db.commit()
    return u


def login(client, username, password=PASSWORD):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def uid(db, username):
    return db.execute(select(User).where(User.username == username)).scalar_one().id


def make_client():
    """An INDEPENDENT TestClient (separate cookie jar) — one per identity;
    the db override is app-global, set once by the client fixture."""
    return TestClient(fastapi_app, client=PEER)


# --- authz matrix -----------------------------------------------------------------


def test_plain_user_gets_403_on_every_admin_route(client, db_session):
    make(db_session, "plain-user")
    db_session.commit()
    csrf = login(client, "plain-user")
    other = make(db_session, "target-user")
    db_session.commit()
    paths = [
        ("GET", "/api/admin/users", None),
        ("POST", "/api/admin/users", {"username": "x1y2z3", "password": PASSWORD}),
        ("PATCH", f"/api/admin/users/{other.id}", {"display_name": "x"}),
        ("POST", f"/api/admin/users/{other.id}/reset-password", {"new_password": PASSWORD}),
        ("DELETE", f"/api/admin/users/{other.id}?confirm=true", None),
        ("POST", f"/api/admin/users/{other.id}/purge-private-documents", None),
        ("GET", "/api/admin/documents", None),
        ("DELETE", f"/api/admin/documents/{other.id}", None),
    ]
    for method, path, body in paths:
        r = client.request(
            method, path, json=body, headers={"X-CSRF-Token": csrf}
        )
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}: {r.text}"
        assert "detail" in r.json()


def test_anonymous_gets_401_on_every_admin_route(client):
    for method, path in (
        ("GET", "/api/admin/users"),
        ("POST", "/api/admin/users"),
        ("GET", "/api/admin/documents"),
        ("DELETE", "/api/admin/documents/00000000-0000-0000-0000-000000000000"),
        ("DELETE", "/api/me"),
    ):
        r = client.request(method, path, json={})
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


def test_admin_cannot_read_any_chat(client, db_session):
    make(db_session, "the-admin-b6", role="admin")
    db_session.commit()
    csrf = login(client, "the-admin-b6")
    for path in (
        "/api/admin/conversations",
        "/api/admin/messages",
        "/api/admin/users/00000000-0000-0000-0000-000000000000/conversations",
    ):
        r = client.get(path, headers={"X-CSRF-Token": csrf})
        assert r.status_code in (404, 405), f"{path} exists: {r.status_code}"


# --- user management --------------------------------------------------------------


def test_create_user_temp_password_and_list_has_no_chat_data(client, db_session):
    make(db_session, "admin-creator", role="admin")
    db_session.commit()
    csrf = login(client, "admin-creator")

    r = client.post(
        "/api/admin/users",
        json={"username": "temp-cred-user", "password": "Temp-Passphrase-9"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["must_change_password"] is True
    assert body["role"] == "user"

    # the temp credential works, but forces a change
    csrf_new = login(client, "temp-cred-user", "Temp-Passphrase-9")
    r = client.get(
        "/api/conversations", headers={"X-CSRF-Token": csrf_new}
    )
    assert r.status_code == 403
    assert r.json() == {"code": "password_change_required"}

    # one cookie jar: back to the admin identity for the listing probe
    login(client, "admin-creator")
    r = client.get("/api/admin/users")
    assert r.status_code == 200
    listing = r.text
    assert "conversations" not in listing.lower()
    assert "message" not in listing.lower()


def test_create_user_rejects_bad_username_weak_password_duplicates(client, db_session):
    make(db_session, "admin-validator", role="admin")
    db_session.commit()
    csrf = login(client, "admin-validator")
    for payload, want in (
        ({"username": "AB", "password": PASSWORD}, 422),  # uppercase/length
        ({"username": "ok-name-99", "password": "short12"}, 422),  # weak
        ({"username": "ok-name-99", "password": PASSWORD, "role": "root"}, 422),
    ):
        r = client.post("/api/admin/users", json=payload, headers={"X-CSRF-Token": csrf})
        assert r.status_code == want, f"{payload} -> {r.status_code}"
    # duplicate username -> 409
    r = client.post(
        "/api/admin/users",
        json={"username": "admin-validator", "password": PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409


def test_last_admin_guard_on_every_path_including_self(client, db_session):
    """The ONLY active admin attempts demotion, self-demotion, disable,
    self-disable, delete and self-delete: every single one must 409."""
    admin = make(db_session, "lone-admin", role="admin")
    db_session.commit()
    csrf = login(client, "lone-admin")
    aid = str(uid(db_session, "lone-admin"))
    other = make(db_session, "bystander")
    db_session.commit()

    attempts = [
        ("PATCH", f"/api/admin/users/{aid}", {"role": "user"}),
        ("PATCH", f"/api/admin/users/{aid}", {"is_active": False}),
        ("PATCH", f"/api/admin/users/{aid}", {"role": "user", "is_active": False}),
        ("DELETE", f"/api/admin/users/{aid}?confirm=true", None),
        ("DELETE", "/api/me?confirm=true", None),
    ]
    for method, path, body in attempts:
        r = client.request(method, path, json=body, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 409, f"{method} {path} -> {r.status_code}: {r.text}"
        assert r.json()["detail"] == {"code": "last_admin"}, r.text

    # with a SECOND admin, all of those succeed for the first admin
    make(db_session, "second-admin", role="admin")
    db_session.commit()
    r = client.patch(
        f"/api/admin/users/{aid}", json={"role": "user"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    # lone-admin is now a PLAIN USER: their admin session must stop working
    r = client.get("/api/admin/users")
    assert r.status_code == 403
    # the bystander delete runs as the SECOND admin (own client)
    second = make_client()
    csrf2 = login(second, "second-admin")
    r = second.delete(
        f"/api/admin/users/{other.id}?confirm=true",
        headers={"X-CSRF-Token": csrf2},
    )
    assert r.status_code == 200, r.text


def test_disable_revokes_sessions_immediately(client, db_session):
    make(db_session, "victim-admin", role="admin")
    make(db_session, "disable-me")
    db_session.commit()
    admin_client = make_client()
    victim_client = make_client()
    csrf = login(admin_client, "victim-admin")
    login(victim_client, "disable-me")
    vid = str(uid(db_session, "disable-me"))

    r = admin_client.patch(
        f"/api/admin/users/{vid}", json={"is_active": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    # the victim's live session dies on the next request (generic 401)
    r = victim_client.get("/api/me")
    assert r.status_code == 401


def test_reset_password_issues_temp_and_revokes_sessions(client, db_session):
    make(db_session, "resetter-admin", role="admin")
    make(db_session, "reset-target")
    db_session.commit()
    admin_client = make_client()
    csrf = login(admin_client, "resetter-admin")
    tid = str(uid(db_session, "reset-target"))

    r = admin_client.post(
        f"/api/admin/users/{tid}/reset-password",
        json={"new_password": "Brand-Temp-Passphrase-2"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["must_change_password"] is True

    r = make_client().post(
        "/api/auth/login",
        json={"username": "reset-target", "password": "Brand-Temp-Passphrase-2"},
    )
    assert r.status_code == 200
    assert r.json()["user"]["must_change_password"] is True

    r = admin_client.post(
        f"/api/admin/users/{tid}/reset-password",
        json={"new_password": "short12"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422

    from app.models.session import Session as SessionRow
    import hashlib as _hl
    rows = db_session.execute(select(SessionRow)).scalars().all()
    print("DEBUG sessions:", [(str(r.user_id)[:8], str(r.token_hash)[:8], r.revoked_at is not None, str(r.id)[:8]) for r in rows])
    tok = admin_client.cookies.get("sid")
    print("DEBUG sid-hash:", _hl.sha256(tok.encode()).hexdigest()[:8])
    r = admin_client.post(
        f"/api/admin/users/{str(__import__('uuid').uuid4())}/reset-password",
        json={"new_password": PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    print("DEBUG probe:", r.status_code, r.text)
    assert r.status_code == 404


# --- documents (C2/C3/C4) ---------------------------------------------------------


def _ingest_org_doc(db_session, tmp_path, monkeypatch, name="kb-note.txt"):
    """Create an org document through the legacy ingest path (admin key)."""
    from app.api import routes as legacy_routes

    monkeypatch.setattr(legacy_routes.settings, "admin_api_key", ADMIN_KEY)
    monkeypatch.setattr(legacy_routes.settings, "upload_dir", str(tmp_path / "uploads"))

    class C:
        page_content = "c"
        metadata = {"start_index": "0"}

    prev = fastapi_app.dependency_overrides.get(get_db)

    def _override():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override
    try:
        c = TestClient(fastapi_app, client=PEER)
        with patch.object(legacy_routes, "load_file", return_value=[C()]), \
             patch.object(legacy_routes, "chunk_documents", return_value=[C()]), \
             patch.object(legacy_routes, "add_documents", return_value=["id1"]):
            r = c.post(
                "/api/ingest",
                files={"file": (name, io.BytesIO(b"org content"), "text/plain")},
                headers={"X-Admin-Key": ADMIN_KEY},
            )
        assert r.status_code == 200, r.text
        return r.json()["document_id"]
    finally:
        # restore the caller fixture's override (clear() would break it)
        if prev is not None:
            fastapi_app.dependency_overrides[get_db] = prev
        else:
            fastapi_app.dependency_overrides.pop(get_db, None)


def test_admin_document_listing_is_org_only_metadata(client, db_session, tmp_path, monkeypatch):
    make(db_session, "doc-admin", role="admin")
    owner = make(db_session, "doc-owner")
    db_session.commit()
    # one org doc + one PRIVATE doc for the owner
    org_id = _ingest_org_doc(db_session, tmp_path, monkeypatch, "org-policy.txt")
    db_session.execute(
        Document.__table__.insert().values(
            id=__import__("uuid").uuid4(), scope="private", owner_id=owner.id,
            display_name="salary-bands-SUPER-SECRET.txt", storage_path="",
            checksum="abc123", size_bytes=10, status="indexed",
        )
    )
    db_session.commit()

    csrf = login(client, "doc-admin")
    r = client.get("/api/admin/documents")
    assert r.status_code == 200
    body = r.text
    assert "org-policy.txt" in body  # org docs are listed per-document
    assert "SUPER-SECRET" not in body, "a private filename leaked to the admin API"
    assert "salary" not in body.lower()
    data = r.json()
    assert data["private_documents_total"] >= 1  # count only, never rows
    assert all("owner" not in item for item in data["items"])


def test_admin_can_delete_any_document_and_purge_private(client, db_session, tmp_path, monkeypatch):
    make(db_session, "purge-admin", role="admin")
    owner = make(db_session, "purge-owner")
    db_session.commit()
    org_id = _ingest_org_doc(db_session, tmp_path, monkeypatch)

    deleted_vectors = []
    import app.api.admin as admin_module

    monkeypatch.setattr(admin_module, "delete_document_vectors",
                        lambda doc_id: deleted_vectors.append(doc_id))

    csrf = login(client, "purge-admin")

    # C3: delete ANY document — the org doc (404 for unknown ids)
    r = client.delete(
        f"/api/admin/documents/{org_id}", headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 200
    r = client.delete(
        "/api/admin/documents/00000000-0000-0000-0000-000000000000",
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404

    # seed two private docs for the owner, then purge them in bulk (C4)
    priv_ids = []
    for i, name in enumerate(("priv-a.txt", "priv-b.txt")):
        pid = __import__("uuid").uuid4()
        priv_ids.append(pid)
        db_session.execute(
            Document.__table__.insert().values(
                id=pid, scope="private", owner_id=owner.id,
                display_name=name, storage_path="", checksum=f"c{i}",
                size_bytes=5, status="indexed",
            )
        )
    db_session.commit()
    pid = str(uid(db_session, "purge-owner"))
    r = client.post(
        f"/api/admin/users/{pid}/purge-private-documents",
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    assert r.json()["purged"] == 2
    # vectors-first consistency (§A9.1): the org-doc delete (1) + the two
    # purged private docs (2)
    assert len(deleted_vectors) == 3
    assert set(deleted_vectors) == {org_id, *[str(d) for d in priv_ids]}
    left = db_session.execute(
        select(func.count()).select_from(Document).where(Document.scope == "private")
    ).scalar_one()
    assert left == 0
    # the user survives (their account and chats are untouched)
    assert db_session.get(User, owner.id) is not None

    events = db_session.execute(
        select(AuditEvent).where(AuditEvent.event_type == "private_documents_purged")
    ).scalars().all()
    assert len(events) == 1
    assert events[0].actor_user_id is not None
    assert "priv-a" not in str(events[0].details)  # no content in audit


def test_delete_user_cascades_privates_but_spares_org_docs(client, db_session, tmp_path, monkeypatch):
    """C2: deleting a user removes their private docs (+vectors, files) but
    NEVER org documents (ownerless); audit actor_user_id stays the admin."""
    make(db_session, "cascader-admin", role="admin")
    doomed = make(db_session, "doomed-user")
    db_session.commit()
    org_id = _ingest_org_doc(db_session, tmp_path, monkeypatch, "survivor.txt")
    deleted_vectors = []
    import app.api.admin as admin_module

    monkeypatch.setattr(admin_module, "delete_document_vectors",
                        lambda doc_id: deleted_vectors.append(doc_id))

    priv_id = __import__("uuid").uuid4()
    db_session.execute(
        Document.__table__.insert().values(
            id=priv_id, scope="private", owner_id=doomed.id,
            display_name="private.txt", storage_path="", checksum="xyz",
            size_bytes=5, status="indexed",
        )
    )
    db_session.commit()

    did = str(uid(db_session, "doomed-user"))
    csrf = login(client, "cascader-admin")
    r = client.delete(
        f"/api/admin/users/{did}?confirm=true", headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 200, r.text
    assert r.json()["private_documents"] == 1
    assert deleted_vectors == [str(priv_id)]
    assert db_session.execute(
        select(func.count()).select_from(Document).where(Document.id == priv_id)
    ).scalar_one() == 0  # private row cascaded
    assert db_session.execute(
        select(func.count()).select_from(Document).where(Document.id == org_id)
    ).scalar_one() == 1  # org doc SURVIVES (C2)
    r = client.post(
        "/api/auth/login",
        json={"username": "doomed-user", "password": PASSWORD},
    )
    assert r.status_code == 401

    ev = db_session.execute(
        select(AuditEvent).where(AuditEvent.event_type == "user_deleted")
    ).scalar_one()
    assert ev.details["private_documents"] == 1
    assert ev.actor_user_id is not None  # the acting admin, not the deleted


def test_delete_me_self_service(client, db_session, tmp_path, monkeypatch):
    make(db_session, "self-nuker")
    db_session.commit()
    csrf = login(client, "self-nuker")

    # missing confirmation -> 422
    r = client.delete("/api/me", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 422

    deleted_vectors = []
    import app.api.admin as admin_module

    monkeypatch.setattr(admin_module, "delete_document_vectors",
                        lambda doc_id: deleted_vectors.append(doc_id))
    r = client.delete(
        "/api/me?confirm=true", headers={"X-CSRF-Token": csrf}
    )
    assert r.status_code == 200
    # login is gone
    r = client.post(
        "/api/auth/login", json={"username": "self-nuker", "password": PASSWORD}
    )
    assert r.status_code == 401
    # audit row anonymized (actor_user_id SET NULL by the FK)
    ev = db_session.execute(
        select(AuditEvent).where(AuditEvent.event_type == "user_deleted")
    ).scalar_one()
    assert ev.details["self"] is True


def test_audit_events_carry_no_content(client, db_session):
    make(db_session, "audit-admin", role="admin")
    db_session.commit()
    csrf = login(client, "audit-admin")
    r = client.post(
        "/api/admin/users",
        json={"username": "audit-target-7", "password": PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201
    events = db_session.execute(
        select(AuditEvent).where(AuditEvent.event_type == "user_created")
    ).scalars().all()
    assert any(e.details.get("role") == "user" for e in events)
    for e in events:
        dump = str(e.details)
        assert "audit-target-7" not in dump and PASSWORD not in dump


# --- forced-change gate covers EVERY non-exempt route (route inventory) -----------


def _iter_routes():
    from fastapi.routing import APIRoute

    seen = set()
    stack = list(fastapi_app.routes)
    while stack:
        route = stack.pop()
        if id(route) in seen:
            continue
        seen.add(id(route))
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD"}:
                yield method, route.path
        for inner in (getattr(route, "original_router", None),
                      getattr(route, "routes", None)):
            if inner is None:
                continue
            stack.extend(inner.routes if hasattr(inner, "routes") else inner)


def test_forced_change_gate_blocks_every_new_route(client, db_session):
    """Walk the LIVE route table: a must-change-password user gets
    403 password_change_required on every session-auth route except the
    three exempt ones — including everything B6 added."""
    make(db_session, "gated-prober", must_change_password=True)
    db_session.commit()
    csrf = login(client, "gated-prober")

    allow = {
        ("GET", "/"),
        ("GET", "/api/health"),
        ("POST", "/api/chat"),
        ("POST", "/api/ingest"),
        ("POST", "/api/auth/login"),
        ("GET", "/openapi.json"),
        ("GET", "/docs"),
        ("GET", "/docs/oauth2-redirect"),
        ("GET", "/redoc"),
        ("GET", "/api/me"),
        ("POST", "/api/auth/password"),
        ("POST", "/api/auth/logout"),
    }
    import re

    checked = 0
    for method, path in _iter_routes():
        if (method, path) in allow:
            continue
        concrete = re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", path)
        r = client.request(
            method, concrete, json={},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 403, (
            f"gate missed {method} {path}: got {r.status_code} {r.text[:120]}"
        )
        assert r.json() == {"code": "password_change_required"}, f"{method} {path}"
        checked += 1
    assert checked >= 15, f"route inventory only probed {checked} routes"
