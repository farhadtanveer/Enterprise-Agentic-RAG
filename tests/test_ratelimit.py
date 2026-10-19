"""H1b: sliding-window rate limiting on /api/chat."""
from fastapi.testclient import TestClient

import app.api.routes as routes
from app.services.ratelimit import SlidingWindowLimiter
from app.main import app

client = TestClient(app)


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_limiter_blocks_inside_window():
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(limit=2, clock=clock)

    assert limiter.allow("ip1") is True
    assert limiter.allow("ip1") is True
    assert limiter.allow("ip1") is False  # third hit inside window


def test_limiter_frees_after_window_passes():
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(limit=1, clock=clock)

    assert limiter.allow("ip1") is True
    assert limiter.allow("ip1") is False
    clock.now += 61  # window is 60s
    assert limiter.allow("ip1") is True


def test_limiter_keys_are_independent():
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(limit=1, clock=clock)

    assert limiter.allow("ip1") is True
    assert limiter.allow("ip2") is True
    assert limiter.allow("ip1") is False


def test_chat_endpoint_returns_429(monkeypatch):
    monkeypatch.setattr(routes, "chat_limiter", SlidingWindowLimiter(limit=1))
    monkeypatch.setattr(routes, "ask", lambda q: {"answer": "ok", "source_used": "direct",
                                                 "trace": [], "citations": [], "current_query": q})

    first = client.post("/api/chat", json={"question": "hi there"})
    second = client.post("/api/chat", json={"question": "hi there"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == "Rate limit exceeded. Try again shortly."
