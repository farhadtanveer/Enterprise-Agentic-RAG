"""Concurrency review (review task 5).

The live B3 evidence seeded a pending row and fired two concurrent sends at
an ALREADY-PENDING conversation — that proves the reject branch exists, not
that the FOR UPDATE lock serializes a real race. These tests run the race
itself: a slow fake graph, two genuinely simultaneous sends on a FRESH
conversation, repeated 20 times — exactly one 200 and one 409 each round,
no exceptions, no interleaved writes.

Also: delete-while-send-in-flight (the delete path takes NO row lock, so it
can interleave with TX2 → today that surfaces as an unhandled 500, encoded
as a failing test).
"""
import threading
import time
from uuid import uuid4

from sqlalchemy import select

from app.api import conversations as conv_api
from app.models.message import Message
from app.services import conversations as svc

PASSWORD = "a valid longer passphrase"
ROUNDS = 20
GRAPH_DELAY = 0.15  # seconds — long enough that both requests overlap


def slow_ask(calls, started_event=None, delay=GRAPH_DELAY):
    """A graph stand-in that is genuinely SLOW — the race window itself."""

    def fake_ask(question, retrieval_filter=None):
        calls.append(question)
        if started_event is not None:
            started_event.set()
        time.sleep(delay)
        return {
            "answer": f"Answer to: {question}",
            "source_used": "private_kb",
            "trace": ["Router → KB"],
            "citations": [],
            "current_query": question,
            "token_usage": {},
        }

    return fake_ask


def _login(factory, username):
    """One session for both racing clients (they act as the same user's two
    browser tabs); returns (sid, csrf) cookie values."""
    setup = factory["client_for"]()
    r = setup.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.cookies.get("sid"), r.cookies.get("csrf")


def _make_user_and_conv(factory, tag):
    from app.core.db import get_sessionmaker

    db = get_sessionmaker(factory["test_url"])()
    try:
        user = factory["make_user"](db, f"{tag}-{uuid4().hex[:8]}")
        conv = svc.create_conversation(db, user.id)
        db.commit()
        return user.username, conv.id
    finally:
        db.close()


def test_double_send_on_fresh_conversation_exactly_one_200_one_409(
    prod_client_factory, monkeypatch
):
    """THE race, for real: two simultaneous sends on a fresh conversation
    while the graph is slow. TX1's FOR UPDATE + committed pending row must
    serialize them: exactly one 200 and one 409 message_pending per round,
    20/20 rounds — and the loser never reaches the graph."""
    from app.core.db import get_sessionmaker

    factory = prod_client_factory
    username, conv_id = _make_user_and_conv(factory, "racer")
    sid, csrf = _login(factory, username)

    calls = []
    monkeypatch.setattr(conv_api, "ask", slow_ask(calls))
    path = f"/api/conversations/{conv_id}/messages"

    for round_no in range(ROUNDS):
        barrier = threading.Barrier(2)
        results = {}

        def send(tag):
            c = factory["client_for"](cookie_token=sid, csrf=csrf)
            barrier.wait()
            resp = c.post(
                path,
                json={"question": f"race round {round_no} {tag}"},
                headers={"X-CSRF-Token": csrf},
            )
            results[tag] = resp.status_code

        threads = [threading.Thread(target=send, args=(t,)) for t in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        codes = sorted(results.values())
        assert codes == [200, 409], (
            f"round {round_no} produced {results!r} — want exactly one 200 and "
            "one 409 (message_pending); anything else means the pending check "
            "raced or an error leaked"
        )

    # exactly ROUNDS user+assistant pairs were written, nothing interleaved
    check = get_sessionmaker(factory["test_url"])()
    try:
        msgs = check.execute(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.created_at, Message.id)
        ).scalars().all()
        assert len(msgs) == 2 * ROUNDS, f"expected {2*ROUNDS} messages, got {len(msgs)}"
        roles = [m.msg_role for m in msgs]
        assert roles == ["user", "assistant"] * ROUNDS
        assert len(calls) == ROUNDS, "the 409'd send reached the graph"
    finally:
        check.close()


def test_delete_during_pending_send_must_not_500(prod_client_factory, monkeypatch):
    """Delete while a send's graph is in flight: the delete path takes no
    FOR UPDATE lock, so TX2's INSERT hits a vanished parent (FK violation)
    and today surfaces as an unhandled 500 — the user's question is
    cascade-deleted, the answer is lost, no error-status marker is written.
    DESIRED: the send ends 200/409/502 (answer persisted or a clean error
    marker), never a raw framework 500."""
    factory = prod_client_factory
    username, conv_id = _make_user_and_conv(factory, "deleter")
    sid, csrf = _login(factory, username)

    started = threading.Event()
    calls = []
    monkeypatch.setattr(
        conv_api, "ask", slow_ask(calls, started_event=started, delay=0.5)
    )

    sender = factory["client_for"](
        cookie_token=sid, csrf=csrf, raise_server_exceptions=False
    )
    deleter = factory["client_for"](cookie_token=sid, csrf=csrf)

    send_result = {}

    def do_send():
        send_result["resp"] = sender.post(
            f"/api/conversations/{conv_id}/messages",
            json={"question": "delete me mid-flight"},
            headers={"X-CSRF-Token": csrf},
        )

    t = threading.Thread(target=do_send)
    t.start()
    assert started.wait(timeout=5), "graph never started"
    # delete WHILE the graph runs (TX1 already committed the pending row)
    r_del = deleter.delete(f"/api/conversations/{conv_id}", headers={"X-CSRF-Token": csrf})
    assert r_del.status_code == 200, r_del.text
    t.join(timeout=10)

    code = send_result["resp"].status_code
    assert code in (200, 409, 502), (
        f"delete-during-send produced an unhandled {code} (raw framework 500, "
        "answer lost, no error-status marker row) — serialize the delete "
        "against TX2 or translate the FK violation into a clean 502"
    )


def test_send_after_delete_completed_is_clean_404(prod_client_factory, monkeypatch):
    """F2, second order: the delete COMPLETES first, then a send arrives.
    The normal ownership lookup applies — a clean 404 (same as any
    nonexistent conversation), never a crash. Together with the in-flight
    order above this covers both interleavings: delete-wins semantics with
    the send ending 200/404/409/502 — never a raw framework 500."""
    factory = prod_client_factory
    username, conv_id = _make_user_and_conv(factory, "deleted-first")
    sid, csrf = _login(factory, username)

    calls = []
    monkeypatch.setattr(conv_api, "ask", slow_ask(calls, delay=0.05))

    deleter = factory["client_for"](cookie_token=sid, csrf=csrf)
    r_del = deleter.delete(f"/api/conversations/{conv_id}", headers={"X-CSRF-Token": csrf})
    assert r_del.status_code == 200, r_del.text

    sender = factory["client_for"](cookie_token=sid, csrf=csrf)
    r_send = sender.post(
        f"/api/conversations/{conv_id}/messages",
        json={"question": "after delete"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_send.status_code == 404, (
        f"send to a deleted conversation returned {r_send.status_code} — "
        "want the clean ownership 404"
    )
    assert calls == [], "the graph must never run for a deleted conversation"


def _assert_deleted_marker(conv_id, user_id, url):
    """S1: exactly one chat_failed audit row for the vanished conversation,
    attributed to the sender, with no content anywhere in the row."""
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.audit_event import AuditEvent

    db = get_sessionmaker(url)()
    try:
        events = db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == "chat_failed",
                AuditEvent.object_id == conv_id,
            )
        ).scalars().all()
        assert len(events) == 1, f"expected exactly 1 chat_failed event, got {len(events)}"
        ev = events[0]
        assert str(ev.actor_user_id) == str(user_id)
        assert ev.outcome == "error"
        dump = "|".join(
            str(getattr(ev, c))
            for c in ("event_type", "object_type", "outcome", "route")
        ) + f"|{str(ev.details)}"
        assert "delete me mid-flight" not in dump, "question text leaked into the audit row"
    finally:
        db.close()


def test_delete_during_send_returns_409_conversation_deleted_with_audit(
    prod_client_factory, monkeypatch
):
    """S1 (mandated semantics change): the send whose conversation is deleted
    mid-graph answers 409 {"code": "conversation_deleted"} — not 502 — and
    TX2 writes the chat_failed audit event (no FK on audit_events.object_id,
    so the event survives the cascade). No answer text is persisted, no raw
    500, and the audit row carries no question text."""
    factory = prod_client_factory
    username, conv_id = _make_user_and_conv(factory, "s1-deleter")
    sid, csrf = _login(factory, username)
    from app.core.db import get_sessionmaker
    from app.models.user import User

    db = get_sessionmaker(factory["test_url"])()
    try:
        user_id = db.execute(
            select(User).where(User.username == username)
        ).scalar_one().id
    finally:
        db.close()

    started = threading.Event()
    calls = []
    monkeypatch.setattr(
        conv_api, "ask", slow_ask(calls, started_event=started, delay=0.5)
    )

    sender = factory["client_for"](
        cookie_token=sid, csrf=csrf, raise_server_exceptions=False
    )
    deleter = factory["client_for"](cookie_token=sid, csrf=csrf)

    send_result = {}

    def do_send():
        send_result["resp"] = sender.post(
            f"/api/conversations/{conv_id}/messages",
            json={"question": "delete me mid-flight"},
            headers={"X-CSRF-Token": csrf},
        )

    t = threading.Thread(target=do_send)
    t.start()
    assert started.wait(timeout=5), "graph never started"
    r_del = deleter.delete(f"/api/conversations/{conv_id}", headers={"X-CSRF-Token": csrf})
    assert r_del.status_code == 200, r_del.text
    t.join(timeout=10)

    r = send_result["resp"]
    assert r.status_code == 409, f"want 409 conversation_deleted, got {r.status_code}: {r.text}"
    assert r.json()["detail"] == {"code": "conversation_deleted"}, r.json()
    # the graph DID run (the delete landed mid-flight); that nothing was
    # persisted is proven by the FK error itself — the conversation and all
    # its messages are gone
    _assert_deleted_marker(conv_id, user_id, factory["test_url"])


def test_delete_during_failing_graph_also_409_with_audit(
    prod_client_factory, monkeypatch
):
    """S1, failure-path variant: the graph ALSO fails AND the conversation
    is deleted mid-flight. TX2's error-marker insert fails the same way;
    the response is still 409 conversation_deleted with the chat_failed
    audit event — never 502, never a raw 500."""
    factory = prod_client_factory
    username, conv_id = _make_user_and_conv(factory, "s1-failer")
    sid, csrf = _login(factory, username)
    from app.core.db import get_sessionmaker
    from app.models.user import User

    db = get_sessionmaker(factory["test_url"])()
    try:
        user_id = db.execute(
            select(User).where(User.username == username)
        ).scalar_one().id
    finally:
        db.close()

    started = threading.Event()

    def failing_ask(question, retrieval_filter=None):
        started.set()
        time.sleep(0.5)
        raise RuntimeError("graph exploded")

    monkeypatch.setattr(conv_api, "ask", failing_ask)

    sender = factory["client_for"](
        cookie_token=sid, csrf=csrf, raise_server_exceptions=False
    )
    deleter = factory["client_for"](cookie_token=sid, csrf=csrf)

    send_result = {}

    def do_send():
        send_result["resp"] = sender.post(
            f"/api/conversations/{conv_id}/messages",
            json={"question": "delete me mid-flight"},
            headers={"X-CSRF-Token": csrf},
        )

    t = threading.Thread(target=do_send)
    t.start()
    assert started.wait(timeout=5), "graph never started"
    r_del = deleter.delete(f"/api/conversations/{conv_id}", headers={"X-CSRF-Token": csrf})
    assert r_del.status_code == 200, r_del.text
    t.join(timeout=10)

    r = send_result["resp"]
    assert r.status_code == 409, f"want 409 conversation_deleted, got {r.status_code}: {r.text}"
    assert r.json()["detail"] == {"code": "conversation_deleted"}, r.json()
    _assert_deleted_marker(conv_id, user_id, factory["test_url"])
