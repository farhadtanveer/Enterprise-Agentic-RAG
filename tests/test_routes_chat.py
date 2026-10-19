"""H2: chat failures must not leak exception details to clients."""
from fastapi.testclient import TestClient

import app.api.routes as routes
from app.main import app

client = TestClient(app)


def test_chat_error_is_generic_and_logged(monkeypatch, caplog):
    def boom(question):
        raise RuntimeError("pinecone query failed: api_key=sk-SECRET host=idx-123")

    monkeypatch.setattr(routes, "ask", boom)
    with caplog.at_level("ERROR", logger="app.api.routes"):
        resp = client.post("/api/chat", json={"question": "how do I reset my password?"})

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Internal error"
    assert "sk-SECRET" not in resp.text
    assert "pinecone" not in resp.text.lower()
    assert "chat request failed" in caplog.text
