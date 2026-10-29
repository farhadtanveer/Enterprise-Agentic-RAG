"""B3: conversations + chat — ownership (§A11.1), TX1→graph→TX2 discipline
(§A8), failure path, caps, pending-409, per-user rate limit, audit privacy,
token/citation persistence.

Runs against real Postgres via the rollback-per-test fixtures; the graph is
faked offline (no API keys in tests, per house rules).
"""
import inspect
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api import auth as auth_api
from app.api import conversations as conv_api
from app.core.db import get_db
from app.main import app as fastapi_app
from app.models.audit_event import AuditEvent
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.rag import workflow as workflow_mod
from app.services import conversations as svc
from app.services import sessions as sessions_svc
from app.services import users
from app.services.ratelimit import SlidingWindowLimiter

PEER = ("203.0.113.5", 50000)
PASSWORD = "a valid longer passphrase"
MARKER = "SECRET-MARKER-B3-42"


@pytest.fixture(autouse=True)
def _fresh_limiters(monkeypatch):
    """Module-level limiter state must not leak between tests (chat is
    per-user; the login limiter is per-IP and this file logs in a lot)."""
    monkeypatch.setattr(conv_api, "chat_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(auth_api, "login_limiter", SlidingWindowLimiter(10**9))


@pytest.fixture()
def client(db_session):
    def _override():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override
    yield TestClient(fastapi_app, client=PEER)
    fastapi_app.dependency_overrides.clear()


def make_user(db, username, password=PASSWORD):
    user = users.create_user(db, username, password=password)
    db.commit()
    return user


def login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200
    return r.json()["csrf_token"]


def csrf_of(client):
    return client.cookies.get("csrf")


def fake_ask_result(question, **overrides):
    result = {
        "answer": f"Answer to: {question}",
        "source_used": "private_kb",
        "trace": ["Router → KB", f"Query rewrite → {question}"],
        "citations": [{"title": "policy.pdf", "url": "", "type": "private_kb"}],
        "current_query": f"{question} (rewritten)",
        "token_usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }
    result.update(overrides)
    return result


@pytest.fixture()
def ok_ask(monkeypatch):
    calls = []
    monkeypatch.setattr(
        conv_api, "ask", lambda q: calls.append(q) or fake_ask_result(q)
    )
    return calls


# --- CRUD + ownership (§A11.1) -----------------------------------------------


def test_create_list_get_rename_delete_own_conversation(client, db_session):
    make_user(db_session, "owner")
    csrf = login(client, "owner")

    r = client.post("/api/conversations", json={"title": "VPN help"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    conv = r.json()
    assert conv["title"] == "VPN help"
    assert conv["last_message_at"] is None

    r = client.get("/api/conversations")
    assert r.status_code == 200
    assert r.json()["total"] == 1 and r.json()["items"][0]["id"] == conv["id"]

    r = client.get(f"/api/conversations/{conv['id']}")
    assert r.status_code == 200

    r = client.patch(
        f"/api/conversations/{conv['id']}",
        json={"title": "VPN + MFA"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200 and r.json()["title"] == "VPN + MFA"

    r = client.delete(f"/api/conversations/{conv['id']}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert client.get(f"/api/conversations/{conv['id']}").status_code == 404


def test_idor_other_users_conversation_is_404_everywhere(client, db_session):
    """§A11.1: every access is ownership-checked; another user's real id
    behaves exactly like a nonexistent one (404, never 403)."""
    make_user(db_session, "conv-owner")
    make_user(db_session, "conv-attacker")
    login(client, "conv-owner")
    csrf_owner = csrf_of(client)
    r = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf_owner})
    conv_id = r.json()["id"]
    # give it messages
    with patch.object(conv_api, "ask", return_value=fake_ask_result("x")):
        r = client.post(
            f"/api/conversations/{conv_id}/messages",
            json={"question": "how do I reset my password?"},
            headers={"X-CSRF-Token": csrf_owner},
        )
    assert r.status_code == 200

    # the attacker logs in with their OWN session+csrf
    csrf_att = login(client, "conv-attacker")
    h = {"X-CSRF-Token": csrf_att}
    other = uuid4()  # sequential/guessed ids also 404
    for method, path, kwargs in (
        ("get", f"/api/conversations/{conv_id}", {}),
        ("get", f"/api/conversations/{other}", {}),
        ("patch", f"/api/conversations/{conv_id}", {"json": {"title": "hijacked"}}),
        ("delete", f"/api/conversations/{conv_id}", {}),
        ("get", f"/api/conversations/{conv_id}/messages", {}),
        ("post", f"/api/conversations/{conv_id}/messages", {"json": {"question": "mine now?"}}),
    ):
        r = getattr(client, method)(path, headers=h, **kwargs)
        assert r.status_code == 404, (method, path, r.status_code)
    # the attacker's list never shows the victim's conversation
    assert client.get("/api/conversations").json()["total"] == 0
    # and the rename/delete attempts changed nothing for the owner
    csrf_owner2 = login(client, "conv-owner")
    assert (
        client.get(f"/api/conversations/{conv_id}", headers={"X-CSRF-Token": csrf_owner2}).status_code
        == 200
    )


def test_unauthenticated_conversation_access_is_401(client, db_session):
    assert client.get("/api/conversations").status_code == 401
    assert client.request("POST", "/api/conversations", json={}).status_code == 401
    assert client.get(f"/api/conversations/{uuid4()}").status_code == 401
    assert client.get(f"/api/conversations/{uuid4()}/messages").status_code == 401
    assert client.request(
        "POST", f"/api/conversations/{uuid4()}/messages", json={"question": "hi there"}
    ).status_code == 401


def test_conversation_requires_csrf_on_mutations(client, db_session):
    make_user(db_session, "csrf-conv")
    login(client, "csrf-conv")
    assert client.post("/api/conversations", json={}).status_code == 403
    assert client.delete(f"/api/conversations/{uuid4()}").status_code == 403


# --- pagination (review requirement b) ----------------------------------------


def test_conversation_pagination(client, db_session):
    make_user(db_session, "pager")
    login(client, "pager")
    csrf = csrf_of(client)
    for i in range(25):
        r = client.post("/api/conversations", json={"title": f"c{i:02d}"}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200

    # force a deterministic activity order (within the shared test
    # transaction every row's server now() is identical, so natural
    # last_message_at ordering is undefined here — real requests each
    # commit their own transaction timestamp)
    def _touch(title, seconds):
        row = db_session.execute(
            select(Conversation).where(Conversation.title == title)
        ).scalar_one()
        from datetime import timedelta

        row.last_message_at = svc.utcnow() + timedelta(seconds=seconds)
        db_session.flush()

    _touch("c05", 1)
    _touch("c10", 2)

    r = client.get("/api/conversations?limit=10&offset=0")
    body = r.json()
    assert body["total"] == 25 and len(body["items"]) == 10
    assert body["limit"] == 10 and body["offset"] == 0
    assert body["items"][0]["title"] == "c10"  # most recent activity first
    assert body["items"][1]["title"] == "c05"
    r = client.get("/api/conversations?limit=10&offset=20")
    assert r.json()["total"] == 25 and len(r.json()["items"]) == 5
    # pagination windows don't overlap
    seen = {c["id"] for c in body["items"]}
    r_all = client.get("/api/conversations?limit=100").json()
    assert len({c["id"] for c in r_all["items"]}) == 25


def test_message_pagination_chronological(client, db_session, ok_ask):
    make_user(db_session, "msg-pager")
    csrf = login(client, "msg-pager")
    conv = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()
    for _ in range(15):
        r = client.post(
            f"/api/conversations/{conv['id']}/messages",
            json={"question": "tell me about the VPN"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200

    r = client.get(f"/api/conversations/{conv['id']}/messages?limit=10&offset=0")
    body = r.json()
    assert body["total"] == 30 and len(body["items"]) == 10  # 15 user + 15 assistant
    assert body["items"][0]["role"] == "user"  # chronological
    r = client.get(f"/api/conversations/{conv['id']}/messages?limit=10&offset=25")
    assert len(r.json()["items"]) == 5


# --- caps (review requirement e) -----------------------------------------------


def test_message_length_cap(client, db_session):
    make_user(db_session, "long-msg")
    csrf = login(client, "long-msg")
    conv = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()
    r = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"question": "x" * (conv_api.settings.max_message_length + 1)},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422


def test_messages_per_conversation_cap(client, db_session, ok_ask, monkeypatch):
    monkeypatch.setattr(conv_api.settings, "max_messages_per_conversation", 4)
    make_user(db_session, "cap-msg")
    csrf = login(client, "cap-msg")
    conv = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()
    for _ in range(2):  # 4 messages = 2 pairs
        r = client.post(
            f"/api/conversations/{conv['id']}/messages",
            json={"question": "printer help please"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200
    r = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"question": "one more please"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "message_limit_reached"}


def test_conversations_per_user_cap(client, db_session, monkeypatch):
    monkeypatch.setattr(conv_api.settings, "max_conversations_per_user", 2)
    make_user(db_session, "cap-conv")
    make_user(db_session, "cap-other")
    csrf = login(client, "cap-conv")
    for _ in range(2):
        assert client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).status_code == 200
    r = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "conversation_limit_reached"}
    # a different user is unaffected
    assert login(client, "cap-other")
    assert client.post(
        "/api/conversations", json={}, headers={"X-CSRF-Token": csrf_of(client)}
    ).status_code == 200


# --- pending serialization (review requirement d) -------------------------------


def test_send_while_previous_message_pending_409(client, db_session):
    make_user(db_session, "pending")
    csrf = login(client, "pending")
    conv = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()
    # seed an unanswered user message (graph conceptually in flight)
    conv_row = db_session.execute(
        select(Conversation).where(Conversation.id == conv["id"])
    ).scalar_one()
    svc.add_message(db_session, conv_row, "user", "earlier question")
    db_session.commit()

    r = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"question": "a second question while pending"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "message_pending"}


# --- chat success: persistence + audit (review requirements g, h) ----------------


def test_chat_success_persists_everything_and_audits_counts_only(client, db_session, ok_ask):
    make_user(db_session, "chatty")
    csrf = login(client, "chatty")
    conv = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()

    r = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"question": "how do I order a laptop?"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    msg = r.json()
    # (h): citations, trace, rewritten query, model, tokens, timings persisted
    assert msg["status"] == "ok"
    assert msg["content"] == "Answer to: how do I order a laptop?"
    assert msg["citations"] == [{"title": "policy.pdf", "url": "", "type": "private_kb"}]
    assert msg["trace"] == ["Router → KB", "Query rewrite → how do I order a laptop?"]
    assert msg["rewritten_query"] == "how do I order a laptop? (rewritten)"
    assert msg["prompt_tokens"] == 11 and msg["completion_tokens"] == 7
    assert msg["duration_ms"] >= 0
    # conversation metadata moved
    conv_now = client.get(f"/api/conversations/{conv['id']}").json()
    assert conv_now["last_message_at"] is not None

    # (g): audit carries counts/status/route only — never content
    ev = db_session.execute(
        select(AuditEvent).where(AuditEvent.event_type == "chat_asked")
    ).scalar_one()
    assert ev.outcome == "ok" and ev.route == conv_api.SEND_ROUTE
    assert ev.duration_ms == msg["duration_ms"] and ev.tokens_total == 18
    dump = f"{ev.event_type}|{ev.route}|{str(ev.details)}|{ev.outcome}"
    assert "laptop" not in dump and "Answer to" not in dump
    # content lives exactly once — in messages, owner-scoped
    contents = db_session.execute(select(Message.content)).scalars().all()
    assert sum("laptop" in c for c in contents) == 2  # user question + answer


def test_chat_failure_stores_error_marker_and_allows_retry(client, db_session, monkeypatch):
    make_user(db_session, "unlucky")
    csrf = login(client, "unlucky")
    conv = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()

    def boom(question):
        raise RuntimeError(f"pinecone exploded for: {question}")

    monkeypatch.setattr(conv_api, "ask", boom)
    r = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"question": "why is the printer on fire?"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "The assistant failed to answer. You can retry."

    # (c): the user message STAYS; the assistant failure marker is content-free
    rows = db_session.execute(
        select(Message).where(Message.conversation_id == conv["id"]).order_by(Message.created_at)
    ).scalars().all()
    assert [m.msg_role for m in rows] == ["user", "assistant"]
    assert rows[0].content == "why is the printer on fire?"
    assert rows[1].msg_status == "error" and rows[1].content == ""
    ev = db_session.execute(
        select(AuditEvent).where(AuditEvent.event_type == "chat_failed")
    ).scalar_one()
    assert ev.outcome == "error"
    assert "printer" not in f"{ev.event_type}|{ev.route}|{str(ev.details)}"

    # (c): retry works once the graph recovers
    monkeypatch.setattr(conv_api, "ask", lambda q: fake_ask_result(q))
    r = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"question": "retry the printer question"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_no_question_text_in_audit_or_logs_marker_test(client, db_session, caplog, monkeypatch, ok_ask):
    """(g): the marker flows the FULL chat path (question → answer → trace →
    a failing graph's exception) and must appear ONLY in messages — never in
    audit_events, never in any log record, never in a 502 body."""
    import logging as _logging

    make_user(db_session, "marker-user")
    csrf = login(client, "marker-user")
    conv = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()

    question = f"how do I reset {MARKER}?"
    r = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"question": question},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200

    # failure path with the marker inside the exception text
    def boom(q):
        raise RuntimeError(f"upstream exploded mentioning {MARKER}")

    monkeypatch.setattr(conv_api, "ask", boom)
    with caplog.at_level(_logging.ERROR, logger="app.api.conversations"):
        r502 = client.post(
            f"/api/conversations/{conv['id']}/messages",
            json={"question": question},
            headers={"X-CSRF-Token": csrf},
        )
    assert r502.status_code == 502
    assert MARKER not in r502.text

    # audit: scan EVERY row and column incl. details
    for ev in db_session.execute(select(AuditEvent)).scalars().all():
        dump = "|".join(
            str(getattr(ev, c)) for c in (
                "event_type", "object_type", "outcome", "route", "ip",
            )
        ) + f"|{str(ev.details)}"
        assert MARKER not in dump
    # logs: the static error line is fine, the marker is not
    assert MARKER not in caplog.text
    assert "chat graph failed" in caplog.text
    # messages: exactly the owner's rows carry it
    contents = db_session.execute(select(Message.content)).scalars().all()
    hits = [c for c in contents if MARKER in c]
    assert len(hits) == 3  # question ×2 + answer ×1 (all owner's own rows)


# --- per-user chat rate limit (review requirement f) -----------------------------


def test_per_user_chat_rate_limit(client, db_session, monkeypatch):
    monkeypatch.setattr(conv_api, "chat_limiter", SlidingWindowLimiter(2))
    make_user(db_session, "limited")
    make_user(db_session, "unlimited-pal")
    with patch.object(conv_api, "ask", return_value=fake_ask_result("x")):
        csrf_a = login(client, "limited")
        conv_a = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf_a}).json()
        path_a = f"/api/conversations/{conv_a['id']}/messages"
        assert client.post(path_a, json={"question": "first question"}, headers={"X-CSRF-Token": csrf_a}).status_code == 200
        # first send completed (assistant stored) → not pending → allowed
        assert client.post(path_a, json={"question": "second question"}, headers={"X-CSRF-Token": csrf_a}).status_code == 200
        r = client.post(path_a, json={"question": "third question"}, headers={"X-CSRF-Token": csrf_a})
        assert r.status_code == 429
        assert r.headers["Retry-After"] == "60"
        # another user, same "IP": unaffected (limit keyed on user id)
        csrf_b = login(client, "unlimited-pal")
        conv_b = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf_b}).json()
        r = client.post(
            f"/api/conversations/{conv_b['id']}/messages",
            json={"question": "different user question"},
            headers={"X-CSRF-Token": csrf_b},
        )
        assert r.status_code == 200


# --- TX discipline proof (review requirement a) -----------------------------------


def test_tx_discipline_pool_released_during_graph(pg_engine, monkeypatch):
    """(a) THE proof: sessions live exactly as in production — created per
    request, committed, closed — only pinned to the test URL. The pool must
    report zero checked-out connections while the graph runs."""
    import os

    test_url = os.environ["TEST_DATABASE_URL"]
    from app.core.db import get_db, get_engine, get_sessionmaker

    def production_style_db():
        """Same lifecycle as the real get_db: fresh short-lived session,
        caller commits, connection returns to the pool at close."""
        db = get_sessionmaker(test_url)()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = production_style_db
    try:
        db = get_sessionmaker(test_url)()
        try:
            user = users.create_user(db, f"pool-probe-{uuid4().hex[:8]}", password=PASSWORD)
            db.commit()
            conv = svc.create_conversation(db, user.id)
            db.commit()
            token, session_row = sessions_svc.create_session(db, user.id)
            db.commit()
            csrf = sessions_svc.csrf_token_for(session_row)
            user_id = user.id
        finally:
            db.close()

        seen = {}

        def fake_ask(question):
            seen["checkedout"] = get_engine(test_url).pool.checkedout()
            return fake_ask_result(question)

        monkeypatch.setattr(conv_api, "ask", fake_ask)
        client = TestClient(fastapi_app, client=PEER)
        client.cookies.set("sid", token)
        client.cookies.set("csrf", csrf)  # double-submit needs the cookie too
        r = client.post(
            f"/api/conversations/{conv.id}/messages",
            json={"question": "pool discipline check"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200, r.text
        assert seen["checkedout"] == 0, "a DB connection was held across the graph run"
        # data really persisted through the request path
        check = get_sessionmaker(test_url)()
        try:
            msgs = check.execute(
                select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
            ).scalars().all()
            assert [m.msg_role for m in msgs] == ["user", "assistant"]
            assert msgs[1].content == "Answer to: pool discipline check"
        finally:
            check.close()

        # cleanup: committed rows must go (this test bypasses the rollback fixture)
        cleanup = get_sessionmaker(test_url)()
        try:
            cleanup.execute(delete(AuditEvent).where(AuditEvent.actor_user_id == user_id))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()
        finally:
            cleanup.close()
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)


# --- D19 stays unimplemented (review requirement i) --------------------------------


def test_ask_has_no_followup_context_parameter():
    """D19 is explicitly open — ask() must not grow history/context knobs."""
    params = set(inspect.signature(workflow_mod.ask).parameters)
    assert params == {"question", "retrieval_filter"}


# --- legacy surface untouched (review requirement j) --------------------------------


def test_legacy_chat_still_registered_alongside_new_routes(client):
    # legacy /api/chat still validates its body (422, not 404/405) — full
    # behavior remains covered by tests/test_routes_chat.py etc.
    assert client.post("/api/chat", json={"question": ""}).status_code == 422
