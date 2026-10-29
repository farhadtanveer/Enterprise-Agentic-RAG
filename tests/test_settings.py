"""B1: multi-user settings exist with safe defaults and honor env overrides."""
from app.core.config import Settings


def _settings(**kwargs):
    # _env_file=None keeps real .env values out of the assertions
    return Settings(_env_file=None, **kwargs)


def test_database_settings_defaults(monkeypatch):
    # Clear the env the suite itself may run with: we're asserting the CODE
    # defaults here, and TEST_DATABASE_URL legitimately overrides them.
    # COOKIE_SECURE is cleared because conftest forces it false so the
    # TestClient cookie jar works over plain http (see conftest.py).
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
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


# --- review fix 1: production settings validation ------------------------------

GOOD_PROD = dict(
    app_env="production",
    cookie_secure=True,
    login_throttle_pepper="real-pepper-secret",
    database_url="postgresql+psycopg://app:s3cret@db.example:5432/app",
)


def test_production_settings_accept_a_hardened_config():
    from app.core.config import validate_production_settings

    validate_production_settings(Settings(_env_file=None, **GOOD_PROD))  # no raise


def test_production_refuses_insecure_cookies():
    import pytest as _pytest

    from app.core.config import validate_production_settings

    with _pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        validate_production_settings(
            Settings(_env_file=None, **{**GOOD_PROD, "cookie_secure": False})
        )


def test_production_refuses_missing_or_dev_throttle_pepper():
    import pytest as _pytest

    from app.core.config import DEV_THROTTLE_PEPPER_FALLBACK, validate_production_settings

    for bad in ("", DEV_THROTTLE_PEPPER_FALLBACK):
        with _pytest.raises(RuntimeError, match="LOGIN_THROTTLE_PEPPER"):
            validate_production_settings(
                Settings(_env_file=None, **{**GOOD_PROD, "login_throttle_pepper": bad})
            )


def test_production_refuses_dev_database_credentials():
    import pytest as _pytest

    from app.core.config import validate_production_settings

    for bad in (
        "postgresql+psycopg://rag:rag@db:5432/rag",
        "postgresql+psycopg://someone:rag@internal:5432/x",
        "postgresql+psycopg://app@internal:5432/x",  # no password at all
    ):
        with _pytest.raises(RuntimeError, match="DATABASE_URL"):
            validate_production_settings(
                Settings(_env_file=None, **{**GOOD_PROD, "database_url": bad})
            )


def test_development_env_skips_validation_entirely():
    from app.core.config import validate_production_settings

    # every dev default in place — must NOT raise in development
    validate_production_settings(Settings(_env_file=None, app_env="development"))
