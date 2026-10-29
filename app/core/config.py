import logging
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]  # Goes up 2 parent levels

DEFAULT_ADMIN_KEY = "change-me"
# Shared dev fallback for the login-throttle pepper (login_throttle uses it;
# production validation refuses it).
DEV_THROTTLE_PEPPER_FALLBACK = "dev-only-throttle-pepper-do-not-use-in-production"
DEV_DATABASE_URL = "postgresql+psycopg://rag:rag@db:5432/rag"

class Settings(BaseSettings):
    app_name: str = "Enterprise Agentic RAG"
    app_env: str = "development"
    openai_api_key: str = ""
    tavily_api_key: str = ""
    pinecone_api_key: str = ""
    pinecone_index_name: str = "enterprise-agentic-rag"
    pinecone_namespace: str = "enterprise-agentic-rag"
    embedding_model: str = "text-embedding-3-small"
    openai_model: str = "gpt-4o-mini"
    top_k: int = 4
    max_retries: int = 1
    index_ready_timeout_seconds: int = 120
    llm_timeout_seconds: int = 60
    web_search_timeout_seconds: int = 30
    admin_api_key: str = DEFAULT_ADMIN_KEY
    chat_api_key: str = ""
    chat_rate_limit_per_minute: int = 20
    audit_db_path: str = str(BASE_DIR / "data" / "audit.db")
    upload_dir: str = str(BASE_DIR / "uploads")
    sample_kb_dir: str = str(BASE_DIR / "data" / "sample_kb")

    # --- Multi-user foundation (docs/backend-design.md, Phase B) ---
    # Dev default is the compose-network hostname (db:5432): the dev DB has
    # no host port by design (LAN exposure). CI and prod override via env.
    database_url: str = "postgresql+psycopg://rag:rag@db:5432/rag"
    # Tests fail fast when this is unset; never default to the dev database.
    test_database_url: str = ""
    # Secure cookie flag: keep true everywhere except local plain-http dev.
    cookie_secure: bool = True
    session_idle_hours: int = 168  # 7 days sliding
    session_absolute_days: int = 30  # hard cap from login
    # D6 kill switch: false = web fallback node short-circuits, query text
    # never reaches Tavily.
    enable_web_fallback: bool = True
    max_upload_mb: int = 25  # 413 beyond this; enforced while streaming
    # D5: 0 = keep conversations until the user deletes them (sweep deferred).
    conversation_retention_days: int = 0
    pinecone_test_namespace: str = "multi-user-test"
    # --- B2 login protection (docs/backend-design.md §A3/§A5) ---
    # Per-IP sliding window on POST /api/auth/login (0 = off).
    login_rate_limit_per_minute: int = 10
    # Pepper for hashing attempted usernames in the login-throttle counters.
    # Empty = fixed dev fallback (with a startup warning). Set a real secret
    # in production so the throttle table can't be reversed into usernames.
    login_throttle_pepper: str = ""
    # Dev convenience: run `alembic upgrade head` at startup so the compose
    # app always has the current schema. Production keeps the documented
    # `alembic upgrade` deploy step (§A12 change table, app/main.py).
    run_migrations_on_startup: bool = False

    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), extra="ignore")


@lru_cache()
def get_settings() -> Settings:
    return Settings()


def validate_admin_key(settings: Settings) -> None:
    """Fail fast if the ingest endpoint is protected only by the default key."""
    if settings.admin_api_key != DEFAULT_ADMIN_KEY:
        return
    if settings.app_env != "development":
        raise RuntimeError(
            "ADMIN_API_KEY is still the default ('change-me'). "
            "Set a real key before running outside development."
        )
    logging.getLogger(__name__).warning(
        "ADMIN_API_KEY is the default ('change-me'); /api/ingest is effectively unprotected."
    )


def validate_chat_security(settings: Settings) -> None:
    """Chat auth is opt-in (the bundled UI sends no key); warn when it is off."""
    if not settings.chat_api_key:
        logging.getLogger(__name__).warning(
            "CHAT_API_KEY is not set; /api/chat is unauthenticated. "
            "Set CHAT_API_KEY to require the X-API-Key header."
        )


def validate_production_settings(settings: Settings) -> None:
    """Refuse to start outside development with dev-only safety settings."""
    if settings.app_env == "development":
        return
    problems = []
    if not settings.cookie_secure:
        problems.append(
            "COOKIE_SECURE=false — session cookies would travel without Secure"
        )
    if settings.login_throttle_pepper in ("", DEV_THROTTLE_PEPPER_FALLBACK):
        problems.append(
            "LOGIN_THROTTLE_PEPPER is unset or the dev fallback — throttle "
            "counters could be reversed into usernames"
        )
    if _uses_dev_db_credentials(settings.database_url):
        problems.append(
            "DATABASE_URL uses the dev default credentials (rag/rag or empty password)"
        )
    if problems:
        raise RuntimeError(
            "Refusing to start: development settings outside APP_ENV=development:\n  - "
            + "\n  - ".join(problems)
        )


def _uses_dev_db_credentials(database_url: str) -> bool:
    if database_url == DEV_DATABASE_URL:
        return True
    try:
        from sqlalchemy.engine import make_url

        url = make_url(database_url)
    except Exception:  # malformed URLs fail later at engine creation anyway
        return False
    return url.password in (None, "", "rag")
