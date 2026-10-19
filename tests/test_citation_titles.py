"""L1: KB citation titles must be basenames, not full stored paths."""
from langchain_core.documents import Document

import app.rag.workflow as wf


class _FakeMsg:
    content = "answer"


class _FakeLLM:
    def invoke(self, prompt):
        return _FakeMsg()


def test_kb_citation_title_uses_basename(monkeypatch):
    monkeypatch.setattr(wf, "_llm", _FakeLLM())
    state = {
        "question": "How do I connect to the company VPN?",
        "kb_docs": [
            Document(page_content="c", metadata={"source": r"C:\Users\m\docs\company_it_handbook.md"}),
            Document(page_content="c2", metadata={"source": "/app/data/sample_kb/runbook.md"}),
        ],
        "trace": [],
    }
    out = wf.generate_from_kb(state)

    titles = [c["title"] for c in out["citations"]]
    assert titles == ["company_it_handbook.md", "runbook.md"]
