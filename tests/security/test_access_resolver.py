"""G1.2 (docs/tasks/g1.md PART 1): the AccessResolver — the single seam
every retrieval filter and listing check must go through (§A14.3).

Backend-agnostic: these tests run against the real resolver with the real
test DB (memberships) and a Pinecone-faithful fake index for the isolation
proofs. Pinned behaviors:

- audiences_for == ["org", "group:<g1>", "group:<g2>", "user:<self>"] for
  0/1/2 groups, recomputed from the DB on EVERY call (a revoke applies to
  the very next call — no caching anywhere);
- the filter is the SAME single $in condition;
- can_read mirrors the filter (listings and retrieval can never disagree),
  FAILS CLOSED on unknown scopes / dangling groups;
- archived groups are in NO audience — their documents are retrievable by
  no one (explicitly pinned);
- membership role member↔editor flips ONLY editor powers (C1) — the
  audience never changes;
- group isolation through the FULL chat path: A (G1) never receives a G2
  chunk or citation; positive control proves G1 DOES reach A;
- admin has NO implicit group access (no-membership filter matches no
  group chunk; a self-join changes the very next filter).
"""
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.db import get_db as app_get_db
from app.main import app as fastapi_app
from app.models.document import Document
from app.models.group import Group, GroupMembership
from app.services.access import DbAccessResolver
from app.services import users as users_svc
from test_groups_schema import _doc

PASSWORD = "a valid longer passphrase"
PEER = ("203.0.113.5", 50000)

G1_ID = "11111111-1111-7111-8111-111111111111"
G2_ID = "22222222-2222-7222-8222-222222222222"

G1_TEXT = "Group-one quarterly revenue: 12.4M USD."
G2_TEXT = "Group-two salaries: senior 180k."
ORG_TEXT = "Company VPN policy: use the portal, MFA required."


def _user(db, name, *, role="user"):
    u = users_svc.create_user(db, name, password=PASSWORD, role=role)
    db.commit()
    return u


def _group(db, name, *, is_active=True, id=None):
    g = Group(name=name, kind="department", is_active=is_active, id=id)
    db.add(g)
    db.commit()
    return g


def _membership(db, g, u, *, role="member"):
    m = GroupMembership(group_id=g.id, user_id=u.id, role=role)
    db.add(m)
    db.commit()
    return m


# --- audience computation (A14.8 #1) ------------------------------------------------


def test_audiences_for_zero_one_two_groups(db_session):
    u = _user(db_session, "aud-u")
    r = DbAccessResolver(db_session)
    assert r.audiences_for(u) == ["org", f"user:{u.id}"]

    g1 = _group(db_session, "aud-g1")
    _membership(db_session, g1, u)
    assert r.audiences_for(u) == ["org", f"group:{g1.id}", f"user:{u.id}"]

    g2 = _group(db_session, "aud-g2")
    _membership(db_session, g2, u)
    # pinned order: org, then groups, then self (A14.8 #1)
    assert r.audiences_for(u) == [
        "org", f"group:{g1.id}", f"group:{g2.id}", f"user:{u.id}",
    ]
    assert r.filter_for(u) == {
        "read_access": {"$in": ["org", f"group:{g1.id}", f"group:{g2.id}", f"user:{u.id}"]}
    }


def test_revoked_membership_applies_on_the_very_next_call(db_session):
    """Same live user object, same resolver — the second call must see the
    revoke (recomputed from the DB every call, never cached)."""
    u = _user(db_session, "revoke-u")
    g = _group(db_session, "revoke-g")
    _membership(db_session, g, u)
    r = DbAccessResolver(db_session)
    assert f"group:{g.id}" in r.audiences_for(u)
    m = db_session.execute(
        select(GroupMembership).where(GroupMembership.user_id == u.id)
    ).scalar_one()
    db_session.delete(m)
    db_session.commit()
    assert f"group:{g.id}" not in r.audiences_for(u), (
        "audiences_for served a stale/cached audience after revoke"
    )


def test_membership_role_change_flips_only_editor_powers_c1(db_session):
    u = _user(db_session, "roleflip-u")
    g = _group(db_session, "roleflip-g")
    _membership(db_session, g, u, role="member")
    r = DbAccessResolver(db_session)
    before = r.audiences_for(u)
    assert r.is_editor(u, g.id) is False
    m = db_session.execute(
        select(GroupMembership).where(GroupMembership.user_id == u.id)
    ).scalar_one()
    m.role = "editor"
    db_session.commit()
    assert r.audiences_for(u) == before, "role change must NOT change the audience"
    assert r.is_editor(u, g.id) is True


# --- can_read mirrors the filter, fail closed ----------------------------------------


def test_can_read_mirrors_audiences_and_fails_closed(db_session):
    owner = _user(db_session, "canon-owner")
    member = _user(db_session, "canon-member")
    outsider = _user(db_session, "canon-outsider")
    admin = _user(db_session, "canon-admin", role="admin")
    g = _group(db_session, "canon-g")
    _membership(db_session, g, member)
    org_doc = _doc(scope="org", checksum="a" * 64)
    priv = _doc(scope="private", owner_id=owner.id, checksum="b" * 64)
    gdoc = _doc(scope="group", group_id=g.id, checksum="c" * 64)
    db_session.add_all([org_doc, priv, gdoc])
    db_session.commit()
    r = DbAccessResolver(db_session)

    for u in (owner, member, outsider, admin):
        assert r.can_read(u, org_doc) is True
    assert r.can_read(owner, priv) is True
    for u in (member, outsider, admin):
        assert r.can_read(u, priv) is False, "admins have NO implicit private access"
    assert r.can_read(member, gdoc) is True
    for u in (owner, outsider):
        assert r.can_read(u, gdoc) is False
    assert r.can_read(admin, gdoc) is False, "DECISION #4: no implicit group access"
    # fail closed, schema level: a group doc pointing at a nonexistent group
    # cannot even be stored (FK RESTRICT)
    dangling = _doc(scope="group", group_id=UUID(int=0), checksum="d" * 64)
    db_session.add(dangling)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    # ...and can_read refuses such a row should one ever exist in memory
    assert r.can_read(member, dangling) is False
    # unknown scope — fail closed: the DB CHECK already rejects such rows
    # (schema-level fail closed), so probe can_read with an in-memory row
    weird = _doc(scope="weird", checksum="e" * 64)  # deliberately not persisted
    assert r.can_read(admin, weird) is False


def test_archived_group_is_in_no_audience_and_readable_by_no_one(db_session):
    """EXPLICIT PIN (g1.md): what members of an archived group can retrieve —
    org docs + their own private docs. NOTHING of the archived group:
    not the chunk (filter), not the listing (can_read)."""
    u = _user(db_session, "arch-u")
    g = _group(db_session, "arch-g")
    _membership(db_session, g, u)
    gdoc = _doc(scope="group", group_id=g.id, checksum="f" * 64)
    org_doc = _doc(scope="org", checksum="0" * 64)
    db_session.add_all([gdoc, org_doc])
    db_session.commit()
    r = DbAccessResolver(db_session)
    assert f"group:{g.id}" in r.audiences_for(u)

    g.is_active = False
    db_session.commit()
    assert r.audiences_for(u) == ["org", f"user:{u.id}"]
    assert r.can_read(u, gdoc) is False
    assert r.can_read(u, org_doc) is True

    g.is_active = True  # re-activation restores everything, same session
    db_session.commit()
    assert f"group:{g.id}" in r.audiences_for(u)
    assert r.can_read(u, gdoc) is True


# --- group isolation through the FULL chat path (A14.8 #2) ---------------------------


class FakeIndex:
    """Pinecone-faithful: $in over the read_access ARRAY, missing key ⇒ no match."""

    def __init__(self, vectors):
        self.vectors = vectors

    def query(self, vector=None, filter=None, top_k=4, namespace=None, **kw):
        allowed = set(filter["read_access"]["$in"])
        hits = [
            {"id": vid, "score": 1.0, "metadata": {**v["metadata"], "text": v["text"]}}
            for vid, v in self.vectors.items()
            if allowed & set(v["metadata"].get("read_access", []))
        ]
        return {"matches": hits[:top_k]}


class FakeStore:
    def __init__(self, index):
        self.index = index

    def as_retriever(self, search_kwargs):
        index = self.index

        class _Retriever:
            def invoke(self, _query):
                return [
                    type("D", (), {
                        "page_content": h["metadata"]["text"],
                        "metadata": {"source": h["metadata"]["source"]},
                    })()
                    for h in index.query(filter=search_kwargs["filter"])["matches"]
                ]

        return _Retriever()


@pytest.fixture()
def isolated_vectors():
    return {
        "g1v": {"metadata": {"doc_id": "doc-g1", "read_access": [f"group:{G1_ID}"],
                             "source": "g1-revenue.txt"}, "text": G1_TEXT},
        "g2v": {"metadata": {"doc_id": "doc-g2", "read_access": [f"group:{G2_ID}"],
                             "source": "g2-salaries.txt"}, "text": G2_TEXT},
        "orgv": {"metadata": {"doc_id": "doc-org", "read_access": ["org"],
                              "source": "vpn-policy.txt"}, "text": ORG_TEXT},
    }


def test_full_chat_path_group_isolation(db_session, client, monkeypatch, isolated_vectors):
    """A (member of G1) asks; B (member of G2 only) asks the SAME question.
    B's answer/citations must never contain the G1 chunk; A's must. The LLM
    is stubbed (route kb, grade good); the store is the fake index."""
    from app.rag import vectorstore as vs
    from app.rag import workflow as wf
    from app.rag.state import EvidenceGrade, RouteDecision

    a = _user(db_session, "iso-a")
    b = _user(db_session, "iso-b")
    g1 = _group(db_session, "iso-g1", id=UUID(G1_ID))
    g2 = _group(db_session, "iso-g2", id=UUID(G2_ID))
    _membership(db_session, g1, a)
    _membership(db_session, g2, b)

    fake = FakeStore(FakeIndex(isolated_vectors))
    monkeypatch.setattr(vs, "get_vectorstore", lambda: fake)

    class FakeLLM:
        prompts = []

        def with_structured_output(self, schema, method=None):
            class _S:
                def invoke(self, prompt):
                    if schema is RouteDecision:
                        return RouteDecision(route="kb")
                    return EvidenceGrade(grade="good")

            return _S()

        def invoke(self, prompt):
            FakeLLM.prompts.append(str(prompt))
            return type("R", (), {"content": "KB answer."})()

    monkeypatch.setattr(wf, "llm", lambda: FakeLLM())

    def _override():
        yield db_session

    fastapi_app.dependency_overrides[app_get_db] = _override
    try:
        answers = {}
        for name in ("a", "b"):
            c = TestClient(fastapi_app, client=PEER)
            r = c.post("/api/auth/login", json={"username": f"iso-{name}", "password": PASSWORD})
            assert r.status_code == 200, r.text
            csrf = r.json()["csrf_token"]
            conv = c.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()
            r = c.post(
                f"/api/conversations/{conv['id']}/messages",
                json={"question": "what are the quarterly numbers?"},
                headers={"X-CSRF-Token": csrf},
            )
            assert r.status_code == 200, r.text
            answers[name] = r.json()

        everything_b = answers["b"]["content"] + "".join(answers["b"]["trace"]) + str(answers["b"]["citations"])
        assert G1_TEXT not in everything_b, "G1 chunk leaked to B (G2 member)"
        assert "g1-revenue" not in everything_b, "G1 citation leaked to B"
        assert G2_TEXT in everything_b or any(
            G2_TEXT in p for p in FakeLLM.prompts
        ), "positive control failed: G2 chunk did not reach B"

        # the G1 chunk DID flow into A's generation prompt (positive control)
        # and its citation surfaced
        assert "g1-revenue" in str(answers["a"]["citations"]), (
            "positive control failed: G1 chunk did not reach A"
        )
        assert any(G1_TEXT in p for p in FakeLLM.prompts), (
            "positive control failed: G1 chunk never entered any prompt"
        )
        everything_a = answers["a"]["content"] + "".join(answers["a"]["trace"]) + str(answers["a"]["citations"])
        assert "g2-salaries" not in everything_a, "G2 citation leaked to A"
        assert G2_TEXT not in everything_a, "G2 content leaked to A"
    finally:
        fastapi_app.dependency_overrides.clear()


def test_admin_no_implicit_group_access_until_self_join(db_session, isolated_vectors):
    """Admin filter matches NO group chunk; after a self-join (the membership
    row IS the join; the audit + editor-visibility pins live on the G1.4
    endpoints) the very next filter matches the G1 chunk."""
    admin = _user(db_session, "iso-admin", role="admin")
    g = _group(db_session, "iso-admin-g", id=UUID(G1_ID))
    r = DbAccessResolver(db_session)
    assert r.filter_for(admin)["read_access"]["$in"] == ["org", f"user:{admin.id}"]
    allowed = set(r.filter_for(admin)["read_access"]["$in"])
    matched = {vid for vid, v in isolated_vectors.items()
               if allowed & set(v["metadata"].get("read_access", []))}
    assert matched == {"orgv"}, f"admin implicitly matched group chunks: {matched}"

    _membership(db_session, g, admin)  # the self-join
    allowed = set(r.filter_for(admin)["read_access"]["$in"])
    matched = {vid for vid, v in isolated_vectors.items()
               if allowed & set(v["metadata"].get("read_access", []))}
    assert matched == {"orgv", "g1v"}, "self-join did not widen the NEXT filter"
