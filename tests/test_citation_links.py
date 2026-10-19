"""L4: citation links must be restricted to http(s) schemes.

Static check on purpose: the frontend has no JS test runner; this pins
the guard that must exist in app.js. A browser-level test would belong
to a Phase 2 e2e suite.
"""
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "static" / "js" / "app.js"


def test_citation_links_limited_to_http_s():
    js = APP_JS.read_text(encoding="utf-8")
    assert "function safeHref" in js, "scheme guard missing"
    assert "https?" in js, "guard must allow only http/https"
    assert 'safeHref(c.url)' in js, "citations must go through the guard"
    assert 'href="${escapeHtml(href)}"' in js, "href must render only the guarded value"
