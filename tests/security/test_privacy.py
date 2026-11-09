"""Privacy leakage review (review task 3): where can question/answer text
reach besides the owner's messages row?

Findings encoded here:
- PASSING  test_postgres_engine_hides_bound_parameters — behavioral proof on
  REAL Postgres (tests/test_db.py only proves it on sqlite) that bound
  values never appear in DBAPIError text or captured logs.
- FAILING  test_legacy_chat_exception_text_must_not_reach_logs — legacy
  /api/chat uses logger.exception("chat request failed"); an upstream
  exception whose text echoes the question (OpenAI/psycopg style errors do)
  lands verbatim in the logs. The per-user path (conversations.py) logs a
  static line and is clean; the legacy path is not.
- FAILING  test_tavily_error_text_must_be_reduced_to_class — workflow.py
  logs the raw Tavily error payload ("Tavily search failed: %s"); upstream
  HTTP errors can embed the query. Design §A6 already schedules this line
  to be "reduced to error class only" (B5) — this pins that requirement.
- PASSING  test_legacy_audit_still_carries_question_until_b5 — documents
  the ACCEPTED transitional state (§A10): legacy /api/chat writes question
  + trace into the legacy SQLite audit db until B5 replaces it.
"""
import logging
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.core.config import get_settings
from app.main import app as fastapi_app

PEER = ("203.0.113.5", 50000)
MARKER = "PRIVACY-MARKER-REVIEW-77"
PASSWORD = "a valid longer passphrase"


# --- SQLAlchemy bind-parameter privacy (real Postgres) ---------------------------


def test_postgres_engine_hides_bind_parameters_in_statement_rendering(pg_engine):
    """On the REAL Postgres engine (app.core.db.get_engine,
    hide_parameters=True): a failing statement's rendered error must hide
    the parameters section. A raw-psycopg CONTROL first proves the server
    itself echoes the bound value ('invalid input syntax for type uuid:
    "<marker>"') — so the assertion below is meaningful, not vacuous.

    Known residual gap (documented in docs/review/security-review.md,
    LOW): hide_parameters redacts the [SQL]/[parameters] sections only;
    the DRIVER's own message (e.orig) can still embed server-echoed bind
    values for cast/constraint errors. Reachable content (message text)
    doesn't hit value-echoing error classes in practice, but the commit
    message 'bound values never render in errors' is broader than the
    mechanism delivers."""
    marker = "SECRET-MARKER-DB-REVIEW"
    import os

    from app.core.db import get_engine

    # get_engine is lru_cached by URL; pg_engine was built from
    # TEST_DATABASE_URL, so this returns the SAME app-configured engine.
    engine = get_engine(os.environ["TEST_DATABASE_URL"])

    # CONTROL: the driver/server DOES echo the value outside our control
    raw = engine.raw_connection()
    try:
        echoed = False
        try:
            with raw.cursor() as cur:
                cur.execute("select cast(%s as uuid)", (marker,))
        except Exception as exc:
            echoed = marker in str(exc)
        assert echoed, (
            "test setup: Postgres stopped echoing bind values; this control "
            "must hold for the hide-assertion below to mean anything"
        )
    finally:
        raw.rollback()
        raw.close()

    # THE APP ENGINE: statement rendering hides the parameters
    with pytest.raises(Exception) as caught:
        with engine.connect() as conn:
            conn.execute(text("select cast(:m as uuid)"), {"m": marker})
    msg = str(caught.value)
    assert "[SQL parameters hidden due to hide_parameters=True]" in msg, (
        f"hide_parameters inactive or rendering changed: {msg!r}"
    )
    sql_lines = [ln for ln in msg.splitlines() if ln.lstrip().startswith("[SQL:")]
    for ln in sql_lines:
        assert marker not in ln, "hide_parameters lost: bind value in SQL section"
    # KNOWN GAP (LOW, see docs/review/security-review.md): the DRIVER's own
    # message (e.orig, rendered as the leading parenthesized line) still
    # carries the server-echoed value for cast/constraint errors. This pin
    # documents that current state — scrubbing e.orig would rightly break it.
    driver_line = msg.splitlines()[0]
    assert marker in driver_line, (
        "driver no longer echoes bind values — update the report's "
        "hide_parameters residual-gap finding"
    )


# --- legacy /api/chat exception-text leak ----------------------------------------


@pytest.fixture()
def legacy_client(db_session):
    from app.core.db import get_db

    def _override():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(fastapi_app, client=PEER)
    finally:
        fastapi_app.dependency_overrides.clear()


def test_legacy_chat_exception_text_must_not_reach_logs(legacy_client, caplog, monkeypatch):
    """HOLE (fails today): app/api/routes.py logs with logger.exception, so
    the full exception — whose text can echo the question — reaches the
    logs. The multi-user path proved this is fixable: it logs a static line
    only (tests/test_conversations_api.py::test_no_question_text_in_audit_or_logs_marker_test).
    The design (§A6) pins 'a Phase B test asserts question strings never
    appear in captured logs' — the legacy path must follow the same rule."""
    from app.api import routes as legacy_routes

    def boom(question, retrieval_filter=None):
        raise RuntimeError(f"upstream provider echoed the question: {question}")

    monkeypatch.setattr(legacy_routes, "ask", boom)
    with caplog.at_level(logging.ERROR, logger="app.api.routes"):
        r = legacy_client.post("/api/chat", json={"question": f"reset my passphrase {MARKER}"})
    assert r.status_code == 500
    assert MARKER not in r.text  # the response body is clean
    assert MARKER not in caplog.text, (
        "question text reached the logs through the legacy chat's "
        "logger.exception — switch to a static log line like conversations.py"
    )


def test_legacy_chat_500_body_hides_question(legacy_client, caplog, monkeypatch):
    """Pin (correct today): the response body is the generic 'Internal
    error', never the exception text."""
    from app.api import routes as legacy_routes

    def boom(question, retrieval_filter=None):
        raise RuntimeError(f"exploded on {question}")

    monkeypatch.setattr(legacy_routes, "ask", boom)
    r = legacy_client.post("/api/chat", json={"question": "super secret question"})
    assert r.status_code == 500
    assert "super secret question" not in r.text


def test_tavily_error_text_must_be_reduced_to_class(caplog, monkeypatch):
    """HOLE (fails today, design §A6 says B5 fixes it): workflow.search_web
    logs the raw Tavily error payload, which can embed the query that
    triggered it. Only an error CLASS may reach the logs; the detailed text
    may live in the caller's own message trace, not in shared logs."""
    from app.rag import workflow as wf

    class _FakeTool:
        def invoke(self, payload):
            return {"error": f"Tavily rejected query '{MARKER}' (400 bad request)"}

    monkeypatch.setattr(wf, "web_search_tool", _FakeTool)
    with caplog.at_level(logging.WARNING, logger="app.rag.workflow"):
        result = wf.search_web({"current_query": MARKER})
    assert result["source_used"] == "web"  # the failure was handled, not fatal
    assert MARKER in str(result["trace"])  # caller-owned trace may carry it
    assert MARKER not in caplog.text, (
        "raw Tavily error payload reached the logs — reduce to an error class "
        "(design §A6: 'the Tavily error-logging line gets reduced to error "
        "class only')"
    )


# --- legacy audit writer is GONE (B5, correction C8) -------------------------------


def test_legacy_chat_creates_no_audit_db(legacy_client, tmp_path, monkeypatch):
    """B5 (C8): the legacy SQLite audit writer is REMOVED — legacy /api/chat
    must not write question/trace text ANYWHERE, and no audit db file may be
    created or written by serving a chat. (Supersedes the transitional pin
    test_legacy_audit_still_carries_question_until_b5, whose subject — the
    accepted until-B5 state — no longer exists.)"""
    from app.api import routes as legacy_routes

    # the legacy writer module must be GONE (C8)
    with pytest.raises(ModuleNotFoundError):
        import app.services.audit  # noqa: F401

    monkeypatch.chdir(tmp_path)  # any file write would have to land here

    with patch.object(legacy_routes, "ask", return_value={
        "answer": "a", "source_used": "private_kb",
        "trace": ["Router → KB", "Query rewrite → my trace line"],
        "citations": [], "current_query": "my trace line",
    }):
        r = legacy_client.post("/api/chat", json={"question": "legacy question 42"})
    assert r.status_code == 200
    # the answer still carries the trace to the CALLER (their own data),
    # but nothing was persisted server-side
    assert "my trace line" in r.text
    created = [p for p in tmp_path.rglob("*.db")]
    assert created == [], f"a legacy audit db was created by a chat request: {created}"


def test_legacy_ingest_creates_no_audit_db(db_session, tmp_path, monkeypatch):
    """B5: the legacy ingest path likewise leaves no audit db behind."""
    import io
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app.core.db import get_db
    from app.main import app as fastapi_app

    from app.api import routes as legacy_routes

    monkeypatch.setattr(legacy_routes.settings, "admin_api_key", "test-admin-key")
    monkeypatch.setattr(legacy_routes.settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.chdir(tmp_path)  # any file write would have to land here

    class C:
        page_content = "c"
        metadata = {"start_index": "0"}

    def _override():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override
    try:
        client = TestClient(fastapi_app, client=("203.0.113.5", 50000))
        with patch.object(legacy_routes, "load_file", return_value=[C()]),              patch.object(legacy_routes, "chunk_documents", return_value=[C()]),              patch.object(legacy_routes, "add_documents", return_value=["id1"]):
            r = client.post(
                "/api/ingest",
                files={"file": ("note.txt", io.BytesIO(b"hello"), "text/plain")},
                headers={"X-Admin-Key": "test-admin-key"},
            )
        assert r.status_code == 200, r.text
    finally:
        fastapi_app.dependency_overrides.clear()
    created = [p for p in tmp_path.rglob("*.db")]
    assert created == [], f"a legacy audit db was created by an ingest: {created}"


# --- DB-error content must not reach logs (B5 mandated test) -----------------------


def test_db_error_marker_never_reaches_logs(client, db_session, caplog):
    """B5 mandated: force a DB error whose TEXT carries a marker string
    (driver messages can echo bound values — the hide_parameters residual,
    review finding #7) through the chat failure path; the marker must be
    absent from captured logs AND from the response body. The handler logs
    a static line + exception CLASS only."""
    import logging
    import uuid

    from sqlalchemy.exc import DBAPIError

    from app.models.audit_event import AuditEvent
    from app.models.user import User
    from app.services import users as users_svc

    MARKER = "DBERROR-MARKER-REVIEW-91"

    users_svc.create_user(db_session, "dberr-user", password=PASSWORD)
    db_session.commit()
    bob = db_session.execute(
        select(User).where(User.username == "dberr-user")
    ).scalar_one()
    r = client.post("/api/auth/login", json={"username": "dberr-user", "password": PASSWORD})
    csrf = r.json()["csrf_token"]
    conv = client.post("/api/conversations", json={}, headers={"X-CSRF-Token": csrf}).json()

    # a faithful DBAPIError: statement + driver message both carrying the
    # marker (what a bind-value-echoing driver failure looks like)
    boom = DBAPIError(
        f"INSERT INTO messages (content) VALUES ('question with {MARKER}')",
        None,
        Exception(f'duplicate key value violates unique constraint "messages_pkey" {MARKER}'),
    )
    boom.statement = f"INSERT INTO messages (content) VALUES ('question with {MARKER}')"

    from app.api import conversations as conv_api

    orig_add = conv_api.conv_service.add_message
    calls = {"n": 0}

    def failing_add(*args, **kwargs):
        # TX1's user-message insert must SUCCEED (that is the realistic
        # shape: the question is saved, the graph runs, THEN the answer
        # write hits the DB error); only the TX2 assistant insert fails.
        calls["n"] += 1
        if calls["n"] == 1:
            return orig_add(*args, **kwargs)
        raise boom

    with patch.object(conv_api.conv_service, "add_message", side_effect=failing_add):
        with caplog.at_level(logging.ERROR):
            resp = client.post(
                f"/api/conversations/{conv['id']}/messages",
                json={"question": f"please log me not {MARKER}"},
                headers={"X-CSRF-Token": csrf},
            )

    assert resp.status_code == 502
    assert MARKER not in resp.text, "DB error text leaked into the response body"
    assert MARKER not in caplog.text, (
        "DB error text (which can echo bound values) reached the logs — "
        "handlers must log a static line + exception class only"
    )
    # the failure is still COUNTED: exactly one chat_failed audit event,
    # and the error details carry no content either
    events = db_session.execute(
        select(AuditEvent).where(
            AuditEvent.event_type == "chat_failed",
            AuditEvent.object_id == uuid.UUID(conv["id"]),
        )
    ).scalars().all()
    assert len(events) == 1
    assert MARKER not in str(events[0].details)
