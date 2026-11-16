"""H7 settings audit (docs/tasks/g2.md PART 0).

Two guarantees, both enforced here:

1. REFERENCE — every field of ``Settings`` is read by production code
   somewhere under ``app/`` (the H4 lesson: ENABLE_WEB_FALLBACK sat
   decorative for weeks because nothing failed when a field stopped being
   read). A field that is not referenced in ``app/`` fails this test unless
   it is consciously on ``ALLOWLIST`` WITH a written reason. The allowlist
   exists for wiring-only fields whose effect is proven outside pytest; it
   must stay SHORT and every entry carries its justification.

2. EFFECT — a setting is only real when some test observes what it CHANGES.
   The fields with existing effect tests (rates, lifetimes, caps, flags...)
   keep those tests; the three fields whose effect had NO test anywhere
   (``app_name``, ``chat_rate_limit_per_minute``, ``tavily_api_key``) get
   theirs here.
"""
import re
from pathlib import Path

import pytest

import app.api.routes as routes
from app.core.config import Settings, get_settings

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# (field, reason) pairs allowed to have NO reference under app/. Keep this
# list EMPTY unless a field genuinely cannot carry an in-app reference —
# every entry needs the reason inline. Empty as of H7: the four decorative
# fields the audit found (sample_kb_dir, pinecone_test_namespace,
# conversation_retention_days, test_database_url) were REMOVED, not
# allowlisted; run_migrations_on_startup IS referenced (app/main.py) and its
# effect is exercised by every verify-script container whose scratch DB
# needs the startup migration.
ALLOWLIST: dict[str, str] = {}


def _settings_field_names() -> list[str]:
    return list(Settings.model_fields.keys())


def _referenced_in_app(field: str) -> bool:
    """True when the field name appears anywhere under app/, except for the
    field DEFINITION block itself (config.py's Settings class body, found
    via AST — everything outside that block in config.py, e.g. the
    validate_* functions, counts as a real production read)."""
    import ast

    config_py = APP_DIR / "core" / "config.py"
    class_spans: list[tuple[int, int]] = []
    tree = ast.parse(config_py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            class_spans.append((node.body[0].lineno, node.body[-1].end_lineno))

    for py in APP_DIR.rglob("*.py"):
        lines = py.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, start=1):
            if py == config_py and any(lo <= i <= hi for lo, hi in class_spans):
                continue  # the definition itself proves nothing
            if re.search(rf"\b{re.escape(field)}\b", line):
                return True
    return False


def test_every_setting_is_referenced_by_production_code():
    unreferenced = [f for f in _settings_field_names() if not _referenced_in_app(f)]
    unallowlisted = [f for f in unreferenced if f not in ALLOWLIST]
    assert not unallowlisted, (
        "Settings fields not read anywhere under app/ — remove them or "
        f"allowlist them WITH a reason: {unallowlisted}"
    )
    stale = [f for f in ALLOWLIST if f not in _settings_field_names()]
    assert not stale, f"allowlist entries for removed fields: {stale}"


# --- the three fields whose EFFECT had no test anywhere ------------------------


def test_suite_runs_on_the_hermetic_pins():
    """H8: the pins in tests/conftest.py are LOAD-BEARING — this fails if a
    pin is removed or renamed, i.e. the suite would silently start reading
    the developer's .env again."""
    s = get_settings()
    assert s.app_env == "development"
    assert s.run_migrations_on_startup is False
    assert s.enable_web_fallback is False
    assert s.admin_api_key == "change-me"
    assert s.chat_api_key == ""
    assert s.database_url.endswith("hermetic.invalid/test")
    assert s.pinecone_namespace == "multi-user-test"


def test_app_name_is_the_health_payload_and_app_title():
    s = get_settings()
    assert routes.health()["service"] == s.app_name  # the read effect
    from app.main import app as fastapi_app

    assert fastapi_app.title == s.app_name


def test_chat_rate_limit_setting_feeds_the_legacy_chat_limiter():
    s = get_settings()
    # routes.py builds the module-level limiter FROM the setting; if the
    # wiring is broken (hardcoded number), the limiter no longer matches.
    assert routes.chat_limiter.limit == s.chat_rate_limit_per_minute


def test_upload_rate_setting_feeds_the_documents_upload_limiter():
    """A2 (§A14.6 order 4): the hourly upload limiter is built FROM the
    setting, with the one-hour window."""
    import app.api.documents as doc_api

    s = get_settings()
    assert doc_api.upload_limiter.limit == s.user_uploads_per_hour
    assert doc_api.upload_limiter.window == 3600.0


def test_ingest_concurrency_setting_feeds_the_ingest_gate():
    """A3 (DECISION #7 addition): the global parse/embed gate is built
    FROM the setting."""
    from app.services.ingestion import ingest_gate

    assert ingest_gate.limit == get_settings().ingest_concurrency


def test_empty_tavily_key_refuses_tool_construction(monkeypatch):
    import app.rag.workflow as wf

    monkeypatch.setattr(wf.settings, "tavily_api_key", "")
    monkeypatch.setattr(wf, "_web_search", None)
    with pytest.raises(RuntimeError, match="TAVILY_API_KEY"):
        wf.web_search_tool()


def test_set_tavily_key_constructs_the_tool(monkeypatch):
    import app.rag.workflow as wf

    monkeypatch.setattr(wf.settings, "tavily_api_key", "fake-key-for-construction")
    monkeypatch.setattr(wf, "_web_search", None)
    try:
        tool = wf.web_search_tool()
        assert tool is wf._web_search  # the key-bearing instance was cached
    finally:
        monkeypatch.setattr(wf, "_web_search", None)  # never leak a fake tool
