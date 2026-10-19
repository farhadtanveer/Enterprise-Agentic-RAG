"""M1: Tavily error dicts must be surfaced, not treated as 'no results'."""
import app.rag.workflow as wf


class _FakeTavily:
    def invoke(self, payload):
        return {"error": RuntimeError("Invalid API key")}


def _state():
    return {
        "question": "teams outage?",
        "current_query": "teams outage?",
        "kb_docs": [],
        "kb_grade": "weak",
        "retry_count": 0,
        "trace": [],
    }


def test_tavily_error_goes_to_trace_and_log(monkeypatch, caplog):
    monkeypatch.setattr(wf, "_web_search", _FakeTavily())
    with caplog.at_level("WARNING", logger="app.rag.workflow"):
        out = wf.search_web(_state())

    assert out["web_results"] == ""
    assert out["citations"] == []
    assert "TAVILY ERROR" in out["trace"][-1]
    assert "Invalid API key" in out["trace"][-1]
    assert "Tavily search failed" in caplog.text
