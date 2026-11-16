"""A5 (docs/tasks/g2.md, DECISION #1 + C4): the admin documents listing
shows ORG and GROUP documents per-document (meta only: name, size, status,
review dates once B3 adds them) and NOTHING per-document about private
documents — no rows, no filenames ("salary bands.pdf" is itself a leak),
not even existence. Private volume is served by SYSTEM-WIDE AGGREGATES
suppressed below 5 users (D3) plus the enforcement-relevant list of users
OVER or NEAR quota. The bulk purge endpoint stays (B6).
"""
import pytest
from fastapi.testclient import TestClient
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

PEER = ("203.0.113.19", 50000)
PASSWORD = "a valid longer passphrase"
MB = 1024 * 1024


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


def _admin(db, name="a5-admin"):
    u = users_svc.create_user(db, name, password=PASSWORD, role="admin")
    db.commit()
    return u


def _login(client, db, name):
    if db.execute(
        select(User.id).where(User.username == name)
    ).scalar_one_or_none() is None:
        users_svc.create_user(db, name, password=PASSWORD, role="admin")
        db.commit()
    r = client.post("/api/auth/login", json={"username": name, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


from sqlalchemy import select  # noqa: F401  (re-exported for clarity)


def _doc(db, **kw):
    d = Document(scope=kw.pop("scope"), group_id=kw.pop("group_id", None),
                 owner_id=kw.pop("owner_id", None),
                 uploaded_by=kw.pop("uploaded_by", None),
                 display_name=kw.pop("display_name"),
                 storage_path="s/x", checksum="c" * 64,
                 size_bytes=kw.pop("size_bytes", 1), status="indexed")
    db.add(d)
    return d


def test_admin_listing_shows_org_and_group_documents(client, db_session):
    csrf = _login(client, db_session, "a5-admin")
    g = groups_svc.create_group(db_session, "a5-g")
    db_session.add(_doc(db_session, scope="org", display_name="orgdoc.txt"))
    db_session.add(_doc(db_session, scope="group", group_id=g.id, display_name="groupdoc.txt"))
    db_session.commit()

    r = client.get("/api/admin/documents", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    body = r.json()
    names = {i["display_name"] for i in body["items"]}
    assert {"orgdoc.txt", "groupdoc.txt"} <= names
    assert body["total_org"] >= 1 and body["total_group"] >= 1
    for i in body["items"]:
        assert i["status"] == "indexed"
        assert isinstance(i["size_bytes"], int)
        assert "content" not in i and "storage_path" not in i


def test_private_documents_never_appear_per_document(client, db_session):
    csrf = _login(client, db_session, "a5-admin2")
    owners = [users_svc.create_user(db_session, f"a5-owner{i}", password=PASSWORD)
              for i in range(6)]
    for i, o in enumerate(owners):
        db_session.add(_doc(db_session, scope="private", owner_id=o.id,
                            display_name=f"salary bands {i}.pdf", size_bytes=2 * MB))
    db_session.commit()
    db_session.expire_all()

    r = client.get("/api/admin/documents", headers={"X-CSRF-Token": csrf})
    blob = r.text
    assert "salary" not in blob.lower()
    assert ".pdf" not in blob.lower()  # no private filename anywhere
    items = r.json()["items"]
    for i in items:
        assert i["scope"] in ("org", "group")
    # 6 owners ≥ 5 → the aggregate bucket is present
    agg = r.json()["private_aggregates"]
    assert agg is not None
    assert agg["users"] == 6
    assert agg["total_docs"] == 6
    assert agg["total_bytes"] == 12 * MB


def test_private_aggregates_suppressed_below_five_users(client, db_session):
    csrf = _login(client, db_session, "a5-admin3")
    owners = [users_svc.create_user(db_session, f"a5-small{i}", password=PASSWORD)
              for i in range(4)]
    for o in owners:
        db_session.add(_doc(db_session, scope="private", owner_id=o.id,
                            display_name=f"private-{o.username}.txt"))
    db_session.commit()
    db_session.expire_all()

    body = client.get("/api/admin/documents", headers={"X-CSRF-Token": csrf}).json()
    assert body["private_aggregates"] is None  # 4 users < k=5 → suppressed
    blob = str(body)
    assert "private-small" not in blob  # usernames of small-scale owners leak nothing either


def test_near_quota_users_listed(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "user_upload_total_mb", 1)
    monkeypatch.setattr(get_settings(), "user_upload_max_docs", 50)
    csrf = _login(client, db_session, "a5-admin4")
    near = users_svc.create_user(db_session, "a5-near", password=PASSWORD)
    far = users_svc.create_user(db_session, "a5-far", password=PASSWORD)
    # near: 0.85 MB of 1 MB (85% ≥ the 80% NEAR threshold)
    db_session.add(_doc(db_session, scope="private", owner_id=near.id,
                        display_name="big.txt", size_bytes=int(0.85 * MB)))
    # far: tiny usage
    db_session.add(_doc(db_session, scope="private", owner_id=far.id,
                        display_name="tiny.txt", size_bytes=100))
    db_session.commit()
    db_session.expire_all()

    body = client.get("/api/admin/documents", headers={"X-CSRF-Token": csrf}).json()
    near_list = body["near_quota_users"]
    ids = {u["user_id"] for u in near_list}
    assert str(near.id) in ids
    assert str(far.id) not in ids
    entry = next(u for u in near_list if u["user_id"] == str(near.id))
    assert entry["private_bytes"] == int(0.85 * MB)
    assert entry["private_docs"] == 1
    # usernames ARE on this list by design (the enforcement view needs them)


def test_near_quota_doc_count_axis(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "user_upload_max_docs", 2)
    csrf = _login(client, db_session, "a5-admin5")
    u = users_svc.create_user(db_session, "a5-manydocs", password=PASSWORD)
    for i in range(2):  # 2 of 2 docs = 100% ≥ NEAR
        db_session.add(_doc(db_session, scope="private", owner_id=u.id,
                            display_name=f"d{i}.txt", size_bytes=10))
    db_session.commit()
    db_session.expire_all()

    body = client.get("/api/admin/documents", headers={"X-CSRF-Token": csrf}).json()
    ids = {u["user_id"] for u in body["near_quota_users"]}
    assert str(u.id) in ids


def test_plain_user_gets_403_on_admin_listing(client, db_session):
    users_svc.create_user(db_session, "a5-plain", password=PASSWORD)
    db_session.commit()
    csrf = client.post("/api/auth/login",
                       json={"username": "a5-plain", "password": PASSWORD}).json()["csrf_token"]
    r = client.get("/api/admin/documents", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403, r.text
