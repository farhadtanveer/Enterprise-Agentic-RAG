"""Phase 3: audit rows carry caller attribution and latency; old DBs migrate."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app.api.routes as routes
import app.services.audit as audit
from app.core.config import get_settings
from app.main import app

client = TestClient(app)


def _rows(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM query_audit")]
    con.close()
    return rows


def test_chat_writes_attribution_and_duration(monkeypatch, tmp_path):
    db = tmp_path / "audit.db"
    monkeypatch.setattr(get_settings(), "audit_db_path", str(db))
    audit.init_db()
    monkeypatch.setattr(routes, "ask", lambda q: {"answer": "ok", "source_used": "direct",
                                                 "trace": ["Router → DIRECT"], "citations": [],
                                                 "current_query": q})

    resp = client.post("/api/chat", json={"question": "hi there"})

    assert resp.status_code == 200
    (row,) = _rows(db)
    assert row["client_ip"] == "testclient"  # starlette TestClient host
    assert row["authenticated"] == 0  # CHAT_API_KEY unset in tests
    assert isinstance(row["duration_ms"], int) and row["duration_ms"] >= 0


def test_legacy_db_migrates_without_losing_rows(monkeypatch, tmp_path):
    db = tmp_path / "legacy.db"
    monkeypatch.setattr(get_settings(), "audit_db_path", str(db))
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE query_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            question TEXT NOT NULL,
            source_used TEXT NOT NULL,
            trace_json TEXT NOT NULL)"""
    )
    con.execute(
        "INSERT INTO query_audit(created_at, question, source_used, trace_json) VALUES ('t', 'old q', 'direct', '[]')"
    )
    con.commit()
    con.close()

    audit.init_db()  # must ALTER, not fail, and keep the old row

    rows = _rows(db)
    assert len(rows) == 1 and rows[0]["question"] == "old q"
    assert {"client_ip", "authenticated", "duration_ms"} <= set(rows[0])


def test_migration_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "audit_db_path", str(tmp_path / "again.db"))
    audit.init_db()
    audit.init_db()  # second run must not raise
