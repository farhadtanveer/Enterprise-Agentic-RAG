"""A1 (docs/tasks/g2.md PART A): the user-facing documents surface
(§A14.7 minus admin — org upload stays on /api/admin/documents).

Conventions under test:
- session + CSRF everywhere; anonymous 401;
- listings/detail gate on AccessResolver.can_read — OUTSIDE THE AUDIENCE
  IS 404, never 403 (existence not leaked);
- management rights: private → owner, group → editor of THAT group,
  org → admin only (C3: admin write is delete-any / org-update only);
- PATCH NEVER changes scope or group (DECISION #3 — no moves in v1):
  scope/group keys in the payload are a 422;
- DELETE follows §A9.1 (vectors first, row in 'deleting' at vector-delete
  time) and C3 (admin deletes ANY document, audited, no content access);
- audits carry ids and counters only — NEVER the display name (a filename
  like "salary bands.pdf" is itself a leak, DECISION #1).

Offline: parse + vector writes are faked; every stored file lands under a
per-test tmp_path upload_dir (never the settings default).
"""
import pytest
from fastapi.testclient import TestClient
from pathlib import Path
from sqlalchemy import select

from app.api import auth as auth_api
from app.api import conversations as conv_api
from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app as fastapi_app
from app.models.document import Document
from app.models.group import Group
from app.models.user import User
from app.services import groups as groups_svc
from app.services import users as users_svc
from app.services.ratelimit import SlidingWindowLimiter

PEER = ("203.0.113.7", 50000)
PASSWORD = "a valid longer passphrase"


@pytest.fixture(autouse=True)
def _fresh_limiters(monkeypatch):
    monkeypatch.setattr(auth_api, "login_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(conv_api, "chat_limiter", SlidingWindowLimiter(10**9))


@pytest.fixture(autouse=True)
def _tmp_upload_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    return tmp_path / "uploads"


@pytest.fixture(autouse=True)
def _offline_ingest(monkeypatch):
    """No real parsing / Pinecone anywhere in this file."""
    import app.api.documents as doc_api
    from langchain_core.documents import Document as LCDoc

    seen = {"added": []}

    def fake_load(path):
        return [LCDoc(page_content="fake parsed text", metadata={"source": str(path)})]

    def fake_add(chunks, **kwargs):
        seen["added"].append(kwargs)
        return ["vecid"]

    monkeypatch.setattr(doc_api, "load_file", fake_load)
    monkeypatch.setattr(doc_api, "add_documents", fake_add)
    yield seen


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


def login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def gid(db, name):
    return str(db.execute(select(Group.id).where(Group.name == name)).scalar_one())


def uid(db, username):
    return str(db.execute(select(User.id).where(User.username == username)).scalar_one())


def upload(client, csrf, *, name="note.txt", content=b"hello document body", group_id=None):
    return client.post(
        "/api/documents", files={"file": (name, content, "text/plain")},
        data={"group_id": group_id} if group_id is not None else None,
        headers={"X-CSRF-Token": csrf},
    )


def _doc_row(db, doc_id):
    return db.execute(select(Document).where(Document.id == doc_id)).scalar_one_or_none()


# --- the route inventory pins these in tests/security/test_route_inventory.py --


def test_anonymous_gets_401_everywhere(client):
    assert client.get("/api/documents").status_code == 401
    assert client.post("/api/documents").status_code == 401
    assert client.get("/api/documents/00000000-0000-0000-0000-000000000000").status_code == 401
    assert client.patch(
        "/api/documents/00000000-0000-0000-0000-000000000000", json={}
    ).status_code == 401
    assert client.delete(
        "/api/documents/00000000-0000-0000-0000-000000000000"
    ).status_code == 401


def test_private_upload_by_any_user(client, db_session, _offline_ingest):
    u = make(db_session, "docup-user")
    csrf = login(client, "docup-user")
    r = upload(client, csrf, name="salary bands.pdf.txt", content=b"private notes")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["scope"] == "private"
    doc = _doc_row(db_session, body["document_id"])
    assert doc is not None
    assert str(doc.owner_id) == str(u.id)  # private IS owned (C2)
    assert doc.scope == "private"
    assert str(doc.uploaded_by) == str(u.id)  # attribution without question
    # F1: the STORED name is server-generated, never the client filename
    stored = Path(doc.storage_path)
    assert stored.name == f"{doc.id}.txt"
    assert stored.exists()
    # the vector token is the strict user:<id> form
    add = _offline_ingest["added"][-1]
    assert add["scope"] == "private" and add["owner_id"] == str(u.id)


def test_group_upload_editor_only(client, db_session):
    editor = make(db_session, "gup-editor")
    member = make(db_session, "gup-member")
    outsider = make(db_session, "gup-out")
    admin = make(db_session, "gup-admin", role="admin")
    g = groups_svc.create_group(db_session, "gup-g")
    groups_svc.add_member(db_session, g, editor, role="editor")
    groups_svc.add_member(db_session, g, member, role="member")
    db_session.commit()
    group_id = str(g.id)

    csrf_e = login(client, "gup-editor")
    r = upload(client, csrf_e, name="team-note.txt", group_id=group_id)
    assert r.status_code == 201, r.text
    doc = _doc_row(db_session, r.json()["document_id"])
    assert doc.scope == "group"
    assert doc.owner_id is None  # C2: group documents are OWNERLESS
    assert str(doc.group_id) == group_id
    assert str(doc.uploaded_by) == str(editor.id)
    # owner token never on group vectors; strict group: token instead
    # (checked via the fake's kwargs in the resolver tests; here the row shape)

    for username, expected in (("gup-member", 403), ("gup-out", 404),
                               ("gup-admin", 404)):  # admin has no implicit access
        csrf = login(client, username)
        r = upload(client, csrf, name="x.txt", group_id=group_id)
        assert r.status_code == expected, (username, r.status_code, r.text)

    csrf = login(client, "gup-editor")
    r = upload(client, csrf, name="x.txt", group_id="00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404  # unknown group — existence not leaked


def test_listing_shows_only_your_audience(client, db_session):
    u = make(db_session, "list-user")
    other = make(db_session, "list-other")
    g1 = groups_svc.create_group(db_session, "list-g1")
    g2 = groups_svc.create_group(db_session, "list-g2")
    groups_svc.add_member(db_session, g1, u, role="member")
    db_session.commit()

    org = Document(scope="org", display_name="org.txt", storage_path="s/o",
                   checksum="a" * 64, size_bytes=1, status="indexed")
    mine = Document(scope="private", owner_id=u.id, display_name="mine.txt",
                    storage_path="s/m", checksum="b" * 64, size_bytes=1, status="indexed")
    theirs = Document(scope="private", owner_id=other.id, display_name="theirs.txt",
                      storage_path="s/t", checksum="c" * 64, size_bytes=1, status="indexed")
    g1doc = Document(scope="group", group_id=g1.id, display_name="g1doc.txt",
                     storage_path="s/1", checksum="d" * 64, size_bytes=1, status="indexed")
    g2doc = Document(scope="group", group_id=g2.id, display_name="g2doc.txt",
                     storage_path="s/2", checksum="e" * 64, size_bytes=1, status="indexed")
    db_session.add_all([org, mine, theirs, g1doc, g2doc])
    db_session.commit()
    theirs_id, g2doc_id = str(theirs.id), str(g2doc.id)

    csrf = login(client, "list-user")
    r = client.get("/api/documents")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    names = {i["display_name"] for i in items}
    assert {"org.txt", "mine.txt", "g1doc.txt"} <= names
    assert "theirs.txt" not in names
    assert "g2doc.txt" not in names
    # every item carries meta only — never content
    for i in items:
        assert "content" not in i and "storage_path" not in i

    # detail: inside the audience 200, outside 404
    mine_id = str(mine.id)
    assert client.get(f"/api/documents/{mine_id}").status_code == 200
    assert client.get(f"/api/documents/{theirs_id}").status_code == 404
    assert client.get(f"/api/documents/{g2doc_id}").status_code == 404
    assert client.get("/api/documents/00000000-0000-0000-0000-000000000000").status_code == 404


def test_archived_group_document_vanishes_from_listing_and_detail(client, db_session):
    u = make(db_session, "arch-user")
    g = groups_svc.create_group(db_session, "arch-g")
    groups_svc.add_member(db_session, g, u, role="member")
    doc = Document(scope="group", group_id=g.id, display_name="arch.txt",
                   storage_path="s/a", checksum="f" * 64, size_bytes=1, status="indexed")
    db_session.add(doc)
    db_session.commit()
    doc_id = str(doc.id)
    groups_svc.update_group(db_session, g, is_active=False)  # archive
    db_session.commit()

    csrf = login(client, "arch-user")
    assert client.get("/api/documents").json()["items"] == []
    assert client.get(f"/api/documents/{doc_id}").status_code == 404


def test_patch_display_name_and_no_moves(client, db_session):
    owner = make(db_session, "patch-owner")
    doc = Document(scope="private", owner_id=owner.id, display_name="old.txt",
                   storage_path="s/p", checksum="1" * 64, size_bytes=1, status="indexed")
    db_session.add(doc)
    db_session.commit()
    doc_id = str(doc.id)
    csrf = login(client, "patch-owner")

    r = client.patch(f"/api/documents/{doc_id}", json={"display_name": "renamed.txt"},
                     headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert _doc_row(db_session, doc_id).display_name == "renamed.txt"

    # DECISION #3: scope/group are not patchable — 422, row unchanged
    for payload in (
        {"scope": "org"},
        {"group_id": "00000000-0000-0000-0000-000000000000"},
    ):
        r = client.patch(f"/api/documents/{doc_id}", json=payload,
                         headers={"X-CSRF-Token": csrf})
        assert r.status_code == 422, (payload, r.status_code)
    db_session.expire_all()
    row = _doc_row(db_session, doc_id)
    assert row.scope == "private" and row.group_id is None


def test_patch_group_doc_editor_only_admin_c3(client, db_session):
    editor = make(db_session, "pg-editor")
    member = make(db_session, "pg-member")
    admin = make(db_session, "pg-admin", role="admin")
    g = groups_svc.create_group(db_session, "pg-g")
    groups_svc.add_member(db_session, g, editor, role="editor")
    groups_svc.add_member(db_session, g, member, role="member")
    groups_svc.add_member(db_session, g, admin, role="member")  # admin who IS a member
    doc = Document(scope="group", group_id=g.id, display_name="t.txt",
                   storage_path="s/g", checksum="2" * 64, size_bytes=1, status="indexed")
    db_session.add(doc)
    db_session.commit()
    doc_id = str(doc.id)

    csrf = login(client, "pg-editor")
    assert client.patch(f"/api/documents/{doc_id}", json={"display_name": "t2.txt"},
                        headers={"X-CSRF-Token": csrf}).status_code == 200
    # member but not editor: can READ, cannot manage → 403
    csrf = login(client, "pg-member")
    assert client.patch(f"/api/documents/{doc_id}", json={"display_name": "x"},
                        headers={"X-CSRF-Token": csrf}).status_code == 403
    # admin who is a MEMBER but not editor: C3 — no admin update of group docs → 403
    csrf = login(client, "pg-admin")
    assert client.patch(f"/api/documents/{doc_id}", json={"display_name": "x"},
                        headers={"X-CSRF-Token": csrf}).status_code == 403
    # an admin NOT in the group → 404 like any outsider
    make(db_session, "pg-admin2", role="admin")
    csrf = login(client, "pg-admin2")
    assert client.patch(f"/api/documents/{doc_id}", json={"display_name": "x"},
                        headers={"X-CSRF-Token": csrf}).status_code == 404


def test_patch_org_doc_admin_only(client, db_session):
    admin = make(db_session, "po-admin", role="admin")
    plain = make(db_session, "po-plain")
    doc = Document(scope="org", display_name="org.txt", storage_path="s/o",
                   checksum="3" * 64, size_bytes=1, status="indexed")
    db_session.add(doc)
    db_session.commit()
    doc_id = str(doc.id)

    csrf = login(client, "po-plain")
    # org is readable by everyone, but management is admin-only → 403
    assert client.patch(f"/api/documents/{doc_id}", json={"display_name": "x"},
                        headers={"X-CSRF-Token": csrf}).status_code == 403
    csrf = login(client, "po-admin")
    assert client.patch(f"/api/documents/{doc_id}", json={"display_name": "org2.txt"},
                        headers={"X-CSRF-Token": csrf}).status_code == 200


def test_delete_owner_flow_vectors_first(client, db_session, monkeypatch):
    import app.api.documents as doc_api

    def fake_vectors(doc_id):
        row = _doc_row(db_session, doc_id)
        assert row is not None, "row vanished before the vector delete (§A9.1)"
        assert row.status == "deleting"

    monkeypatch.setattr(doc_api, "delete_document_vectors", fake_vectors)
    u = make(db_session, "del-owner")
    doc = Document(scope="private", owner_id=u.id, display_name="del.txt",
                   storage_path="s/d", checksum="4" * 64, size_bytes=1, status="indexed")
    db_session.add(doc)
    db_session.commit()
    doc_id = str(doc.id)
    csrf = login(client, "del-owner")

    r = client.delete(f"/api/documents/{doc_id}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert _doc_row(db_session, doc_id) is None


def test_delete_authz_matrix(client, db_session, monkeypatch):
    import app.api.documents as doc_api

    deleted = []
    monkeypatch.setattr(doc_api, "delete_document_vectors", lambda i: deleted.append(i))

    owner = make(db_session, "dm-owner")
    member = make(db_session, "dm-member")
    editor = make(db_session, "dm-editor")
    admin = make(db_session, "dm-admin", role="admin")
    g = groups_svc.create_group(db_session, "dm-g")
    groups_svc.add_member(db_session, g, member, role="member")
    groups_svc.add_member(db_session, g, editor, role="editor")
    priv = Document(scope="private", owner_id=owner.id, display_name="p.txt",
                    storage_path="s/p", checksum="5" * 64, size_bytes=1, status="indexed")
    gdoc = Document(scope="group", group_id=g.id, display_name="g.txt",
                    storage_path="s/g", checksum="6" * 64, size_bytes=1, status="indexed")
    db_session.add_all([priv, gdoc])
    db_session.commit()
    priv_id, gdoc_id = str(priv.id), str(gdoc.id)

    # outsider on a private doc: 404 (existence not leaked)
    csrf = login(client, "dm-member")
    assert client.delete(f"/api/documents/{priv_id}",
                         headers={"X-CSRF-Token": csrf}).status_code == 404
    # member (non-editor) on a group doc: readable, not manageable → 403
    assert client.delete(f"/api/documents/{gdoc_id}",
                         headers={"X-CSRF-Token": csrf}).status_code == 403
    # editor on the group doc: 200
    csrf = login(client, "dm-editor")
    assert client.delete(f"/api/documents/{gdoc_id}",
                         headers={"X-CSRF-Token": csrf}).status_code == 200
    # owner on their private doc: 200
    csrf = login(client, "dm-owner")
    assert client.delete(f"/api/documents/{priv_id}",
                         headers={"X-CSRF-Token": csrf}).status_code == 200
    # admin deletes ANY doc (C3) — here another user's would-be private doc
    priv2 = Document(scope="private", owner_id=member.id, display_name="p2.txt",
                     storage_path="s/p2", checksum="7" * 64, size_bytes=1, status="indexed")
    db_session.add(priv2)
    db_session.commit()
    priv2_id = str(priv2.id)
    csrf = login(client, "dm-admin")
    r = client.delete(f"/api/documents/{priv2_id}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    assert deleted == [gdoc_id, priv_id, priv2_id]
    db_session.expire_all()
    assert _doc_row(db_session, gdoc_id) is None
    assert _doc_row(db_session, priv_id) is None
    assert _doc_row(db_session, priv2_id) is None


def test_upload_rejects_bad_names_types_and_content(client, db_session):
    from app.services.validation import display_name_problems

    u = make(db_session, "bad-upload")
    csrf = login(client, "bad-upload")

    # F3, direct contract: NUL (and any control char) is rejected by the
    # name validator. A raw NUL cannot even survive multipart transport
    # (clients percent-encode it to a literal "%00"), so the route-level
    # probe uses a reserved char that DOES survive.
    assert display_name_problems("bad\x00name.txt")
    r = upload(client, csrf, name="bad|name.txt")
    assert r.status_code == 422, r.text
    # unsupported type
    r = upload(client, csrf, name="script.exe", content=b"MZ")
    assert r.status_code == 400, r.text
    # F6: .pdf magic must match the claimed type
    r = upload(client, csrf, name="fake.pdf", content=b"definitely not a PDF header")
    assert r.status_code == 415, r.text
    # F6: text uploads must be UTF-8 without NUL — validated while streaming
    r = upload(client, csrf, name="binary.txt", content=b"\x00\x01\x02")
    assert r.status_code == 422, r.text
    # no document rows survived any of the rejections
    assert db_session.execute(select(Document)).scalars().all() == []


def test_audit_events_carry_no_filenames(client, db_session):
    from app.models.audit_event import AuditEvent

    u = make(db_session, "audit-up")
    csrf = login(client, "audit-up")
    r = upload(client, csrf, name="salary bands.pdf.txt", content=b"x")
    assert r.status_code == 201, r.text
    events = db_session.execute(
        select(AuditEvent).where(AuditEvent.event_type == "document_uploaded")
    ).scalars().all()
    assert len(events) == 1
    blob = str(events[0].details)
    assert "salary" not in blob and ".txt" not in blob  # no filenames in audits
    assert events[0].details.get("scope") == "private"


def test_unknown_document_id_is_404_on_every_verb(client, db_session):
    """A6 sweep: the §A8 404-rule holds for the whole documents surface —
    an unknown id is indistinguishable from a foreign id on every verb
    (existence is never leaked)."""
    u = make(db_session, "a6-user")
    csrf = login(client, "a6-user")
    ghost = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/documents/{ghost}").status_code == 404
    assert client.patch(f"/api/documents/{ghost}", json={"display_name": "x"},
                        headers={"X-CSRF-Token": csrf}).status_code == 404
    assert client.delete(f"/api/documents/{ghost}",
                         headers={"X-CSRF-Token": csrf}).status_code == 404
    assert db_session.execute(select(Document)).scalars().all() == []


def test_documents_service_exposes_no_audience_move_function():
    """A14.8 #5 (DECISION #3): a document NEVER changes scope/group after
    creation — the service layer must not even HAVE a move/rescope/re-home
    function to call. (Route-level 422s are proven by
    test_patch_display_name_and_no_moves; this pins the deeper layer.)"""
    import re

    from app.services import documents as docs_service

    def is_move_name(name: str) -> bool:
        # "move" NOT as part of "remove" (remove_stored_file stays a file
        # operation, not an audience operation)
        return bool(re.search(r"(?<!re)move", name.lower())) or any(
            f in name.lower()
            for f in ("rescope", "rehome", "re_home", "change_scope",
                      "convert_scope", "retag")
        )

    names = {n for n in dir(docs_service) if callable(getattr(docs_service, n))}
    offenders = {n for n in names if is_move_name(n)}
    assert not offenders, f"audience-move functions must not exist: {offenders}"


def test_upload_into_archived_group_is_404(client, db_session):
    """H13 semantics on the write path: an archived group is in NOBODY's
    audience — uploads into it are refused like any unknown group."""
    editor = make(db_session, "arch-ed")
    g = groups_svc.create_group(db_session, "arch-upload-g")
    groups_svc.add_member(db_session, g, editor, role="editor")
    groups_svc.update_group(db_session, g, is_active=False)  # archive
    db_session.commit()
    csrf = login(client, "arch-ed")
    r = upload(client, csrf, name="x.txt", group_id=str(g.id))
    assert r.status_code == 404, (r.status_code, r.text)
    assert db_session.execute(select(Document)).scalars().all() == []
