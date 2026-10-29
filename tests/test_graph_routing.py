"""Routing-table tests for the LangGraph workflow.

Runs the real compiled graph end-to-end with scripted fakes at the
three external boundaries (LLM, Pinecone retriever, Tavily). No keys,
no network.
"""
from langchain_core.documents import Document

import app.rag.workflow as wf
from app.core.config import get_settings
from app.rag.state import EvidenceGrade, RouteDecision


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeStructured:
    def __init__(self, script):
        self._script = script

    def invoke(self, prompt):
        return self._script(prompt)


class _FakeLLM:
    """Dispatches on prompt text: router vs grader vs plain generation."""

    def __init__(self, route="kb", grades=("good",)):
        self.route = route
        self.grades = list(grades)
        self.prompts = []

    def with_structured_output(self, model, method=None):
        def script(prompt):
            self.prompts.append(prompt)
            if "route messages" in prompt:
                return RouteDecision(route=self.route)
            return EvidenceGrade(grade=self.grades.pop(0) if self.grades else self.grades_last)
        self.grades_last = self.grades[-1] if self.grades else "weak"
        return _FakeStructured(script)

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if "Rewrite this IT support question" in prompt:
            return _FakeMsg("rewritten query with more keywords")
        return _FakeMsg("final answer text")


class _FakeRetriever:
    def invoke(self, query):
        return [Document(page_content="vpn setup steps", metadata={"source": r"C:\kb\handbook.md"})]


class _FakeTavily:
    def invoke(self, payload):
        return {
            "answer": "web summary",
            "results": [{"title": "vendor doc", "url": "https://example.com/x", "content": "web content"}],
        }


def test_greeting_routes_to_direct_answer(monkeypatch):
    monkeypatch.setattr(wf, "_llm", _FakeLLM(route="direct"))
    monkeypatch.setattr(wf, "get_retriever", lambda filter=None: _FakeRetriever())
    monkeypatch.setattr(wf, "_web_search", _FakeTavily())

    out = wf.ask("thanks, you are great")

    assert out["source_used"] == "direct"
    assert out["answer"] == "final answer text"
    assert "Router → DIRECT" in out["trace"]
    assert not any("Private KB retrieval" in line for line in out["trace"])


def test_good_kb_evidence_answers_from_private_kb(monkeypatch):
    monkeypatch.setattr(wf, "_llm", _FakeLLM(route="kb", grades=("good",)))
    monkeypatch.setattr(wf, "get_retriever", lambda filter=None: _FakeRetriever())
    monkeypatch.setattr(wf, "_web_search", _FakeTavily())

    out = wf.ask("How do I connect to the company VPN?")

    assert out["source_used"] == "private_kb"
    assert out["citations"][0]["title"] == "handbook.md"
    assert "Web fallback" not in " ".join(out["trace"])


def test_weak_kb_good_web_answers_from_web(monkeypatch):
    monkeypatch.setattr(wf, "_llm", _FakeLLM(route="kb", grades=("weak", "good")))
    monkeypatch.setattr(wf, "get_retriever", lambda filter=None: _FakeRetriever())
    monkeypatch.setattr(wf, "_web_search", _FakeTavily())

    out = wf.ask("What is the latest Teams outage guidance?")

    assert out["source_used"] == "web_search"
    assert out["retry_count"] == 0  # web succeeded first try, no rewrite
    assert out["current_query"] == out["question"]


def test_all_weak_and_retries_exhausted_stops_honestly(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "max_retries", 1)
    monkeypatch.setattr(wf, "_llm", _FakeLLM(route="kb", grades=("weak", "weak")))
    monkeypatch.setattr(wf, "get_retriever", lambda filter=None: _FakeRetriever())
    monkeypatch.setattr(wf, "_web_search", _FakeTavily())

    out = wf.ask("obscure question")

    assert out["source_used"] == "insufficient_evidence"
    assert out["retry_count"] == 1  # exactly one rewrite round (max_retries=1)
    assert out["current_query"] != out["question"]  # query was rewritten
    assert out["trace"].count("Query rewrite → rewritten query with more keywords") == 1
    assert "enough reliable evidence" in out["answer"]


def test_rewrite_loop_is_bounded_even_with_bad_grades(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "max_retries", 2)
    monkeypatch.setattr(wf, "_llm", _FakeLLM(route="kb", grades=("weak", "weak", "weak")))
    monkeypatch.setattr(wf, "get_retriever", lambda filter=None: _FakeRetriever())
    monkeypatch.setattr(wf, "_web_search", _FakeTavily())

    out = wf.ask("obscure question")

    assert out["source_used"] == "insufficient_evidence"
    assert out["retry_count"] == 2
