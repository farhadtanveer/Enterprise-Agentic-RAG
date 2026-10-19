"""External-call timeouts: ChatOpenAI gets a timeout; hung Tavily searches
return through the M1 error path instead of blocking forever."""
import time

import app.rag.workflow as wf
from app.core.config import get_settings


class _SlowTavily:
    def invoke(self, payload):
        time.sleep(2)
        return {"answer": "too late"}


class _FastTavily:
    def invoke(self, payload):
        return {"answer": "quick", "results": [{"title": "t", "url": "https://e.com", "content": "c"}]}


def test_llm_constructed_with_timeout(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(wf, "_llm", None)
    monkeypatch.setattr(s, "openai_api_key", "test-key")

    model = wf.llm()

    assert model.request_timeout is not None
    # langchain-openai stores the timeout as a float number of seconds
    assert float(model.request_timeout) == float(s.llm_timeout_seconds)


def test_hung_tavily_search_times_out(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "web_search_timeout_seconds", 1)
    monkeypatch.setattr(wf, "_web_search", _SlowTavily())

    result = wf.tavily_search("query")

    assert "error" in result
    assert "timeout" in result["error"]


def test_tavily_timeout_surfaces_in_trace(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "web_search_timeout_seconds", 1)
    monkeypatch.setattr(wf, "_web_search", _SlowTavily())

    out = wf.search_web({"question": "q", "current_query": "q", "trace": []})

    assert out["web_results"] == ""
    assert "TAVILY ERROR" in out["trace"][-1]


def test_fast_tavily_search_unaffected(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "web_search_timeout_seconds", 30)
    monkeypatch.setattr(wf, "_web_search", _FastTavily())

    result = wf.tavily_search("query")

    assert result["answer"] == "quick"
