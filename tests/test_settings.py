"""B1: multi-user settings exist with safe defaults and honor env overrides."""
from app.core.config import Settings


def _settings(**kwargs):
    # _env_file=None keeps real .env values out of the assertions
    return Settings(_env_file=None, **kwargs)


def test_database_settings_defaults(monkeypatch):
    # Clear the env the suite itself may run with: we're asserting the CODE
    # defaults here, and TEST_DATABASE_URL legitimately overrides them.
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    s = _settings()
    assert s.database_url.startswith("postgresql+psycopg://")
    assert s.test_database_url == ""  # never silently point tests at the dev DB
    assert s.cookie_secure is True  # secure by default; opt out only for local http


def test_session_lifetime_defaults_match_design():
    s = _settings()
    assert s.session_idle_hours == 168  # 7 days idle
    assert s.session_absolute_days == 30  # 30 days absolute


def test_feature_flags_and_limits_defaults():
    s = _settings()
    assert s.enable_web_fallback is True  # D6: kill switch defaults on
    assert s.max_upload_mb == 25
    assert s.conversation_retention_days == 0  # D5: off until a policy exists
    assert s.pinecone_test_namespace == "multi-user-test"


def test_env_overrides_win(monkeypatch):
    monkeypatch.setenv("SESSION_IDLE_HOURS", "1")
    monkeypatch.setenv("MAX_UPLOAD_MB", "5")
    monkeypatch.setenv("ENABLE_WEB_FALLBACK", "false")
    s = Settings(_env_file=None)
    assert s.session_idle_hours == 1
    assert s.max_upload_mb == 5
    assert s.enable_web_fallback is False
