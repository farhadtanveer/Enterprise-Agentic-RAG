"""Phase 3: the web UI can send the chat API key (X-API-Key from localStorage)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ui_has_chat_key_input():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="chatKey"' in html
    assert 'type="password"' in html


def test_chat_requests_carry_the_key_header():
    js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert 'X-API-Key' in js
    assert 'localStorage.getItem("chatApiKey")' in js
    assert 'localStorage.setItem("chatApiKey"' in js
    # the header must ride on the chat call specifically
    assert js.index('"/api/chat"') < js.index('"X-API-Key"')
