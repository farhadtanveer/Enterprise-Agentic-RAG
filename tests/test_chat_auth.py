"""H1a: chat auth is opt-in via CHAT_API_KEY (X-API-Key header)."""
from fastapi.testclient import TestClient

import app.api.routes as routes
from app.core.config import Settings, get_settings, validate_chat_security
from app.main import app

client = TestClient(app)


def _enable_chat_key(monkeypatch, key):
    monkeypatch.setattr(get_settings(), "chat_api_key", key)
    monkeypatch.setattr(routes, "ask", lambda q: {"answer": "ok", "source_used": "direct",
                                                 "trace": [], "citations": [], "current_query": q})


def test_chat_open_when_no_key_configured(monkeypatch):
    _enable_chat_key(monkeypatch, "")
    resp = client.post("/api/chat", json={"question": "hi there"})
    assert resp.status_code == 200


def test_chat_rejects_missing_and_wrong_key(monkeypatch):
    _enable_chat_key(monkeypatch, "test-chat-key")

    missing = client.post("/api/chat", json={"question": "hi there"})
    wrong = client.post("/api/chat", headers={"X-API-Key": "nope"}, json={"question": "hi there"})
    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_chat_accepts_correct_key(monkeypatch):
    _enable_chat_key(monkeypatch, "test-chat-key")

    resp = client.post("/api/chat", headers={"X-API-Key": "test-chat-key"}, json={"question": "hi there"})
    assert resp.status_code == 200
    assert resp.json()["answer"] == "ok"


def test_unconfigured_chat_key_warns(caplog):
    validate_chat_security(Settings(_env_file=None, chat_api_key=""))
    assert "unauthenticated" in caplog.text


def test_configured_chat_key_is_silent(caplog):
    validate_chat_security(Settings(_env_file=None, chat_api_key="a-real-key"))
    assert "unauthenticated" not in caplog.text
