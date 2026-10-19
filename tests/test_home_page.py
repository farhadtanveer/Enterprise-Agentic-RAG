"""Regression: the HTML home page must survive starlette 1.x.

Found by the live container battery after the supply-chain bump — no
test hit "/" before, and TemplateResponse's old signature 500s under
starlette >= 1.0.
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_home_page_renders():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Enterprise Agentic RAG" in resp.text
    assert 'id="chatKey"' in resp.text  # the sidebar key field ships with the page
