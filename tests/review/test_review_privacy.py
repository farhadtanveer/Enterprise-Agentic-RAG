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
import json
import logging
import sqlite3
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import get_settings
from app.main import app as fastapi_app

PEER = ("203.0.113.5", 50000)
MARKER = "PRIVACY-MARKER-REVIEW-77"


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


# --- legacy audit db transitional state (accepted, but pinned) --------------------


def test_legacy_audit_still_carries_question_until_b5(legacy_client, tmp_path, monkeypatch):
    """Pin of the ACCEPTED transitional behavior (§A10): legacy /api/chat
    still writes question + trace JSON into the legacy SQLite audit db.
    This documents WHY B5 must run the confirmation-gated drop script —
    anyone reading the evidence needs to know this table holds plaintext
    questions until then."""
    monkeypatch.setattr(get_settings(), "audit_db_path", str(tmp_path / "audit-review.db"))
    from app.services import audit as legacy_audit

    legacy_audit.init_db()
    from app.api import routes as legacy_routes

    with patch.object(legacy_routes, "ask", return_value={
        "answer": "a", "source_used": "private_kb",
        "trace": ["Router → KB", "Query rewrite → my trace line"],
        "citations": [], "current_query": "my trace line",
    }):
        r = legacy_client.post("/api/chat", json={"question": "legacy question 42"})
    assert r.status_code == 200

    con = sqlite3.connect(str(tmp_path / "audit-review.db"))
    try:
        rows = con.execute(
            "select question, trace_json from query_audit"
        ).fetchall()
    finally:
        con.close()
    assert any("legacy question 42" in (q or "") for q, _ in rows), (
        "legacy audit behavior changed — update this pin and the B5 plan"
    )
    assert any("my trace line" in json.dumps(t) for _, t in rows)
