"""M5: untrusted web content must reach the LLM inside explicit fences
with data-not-instructions framing."""
import app.rag.workflow as wf
from test_graph_routing import _FakeLLM, _FakeRetriever, _FakeTavily


def test_web_content_is_fenced_in_grade_and_generate_prompts(monkeypatch):
    fake_llm = _FakeLLM(route="kb", grades=("weak", "good"))
    monkeypatch.setattr(wf, "_llm", fake_llm)
    monkeypatch.setattr(wf, "get_retriever", lambda filter=None: _FakeRetriever())
    monkeypatch.setattr(wf, "_web_search", _FakeTavily())

    out = wf.ask("What is the latest Teams outage guidance?")

    assert out["source_used"] == "web_search"
    prompts = "\n".join(fake_llm.prompts)

    assert prompts.count("<<<UNTRUSTED WEB CONTENT>>>") == 2
    assert prompts.count("<<<END UNTRUSTED WEB CONTENT>>>") == 2
    assert "UNTRUSTED" in prompts and "as data" in prompts
    # the actual web content lands inside a fence, not loose in the prompt
    grade_prompt = next(p for p in fake_llm.prompts if "External web evidence" in p)
    gen_prompt = next(p for p in fake_llm.prompts if "EXTERNAL and UNTRUSTED" in p)
    for prompt in (grade_prompt, gen_prompt):
        start = prompt.index("<<<UNTRUSTED WEB CONTENT>>>")
        end = prompt.index("<<<END UNTRUSTED WEB CONTENT>>>")
        assert "web content" in prompt[start:end]
