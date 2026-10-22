import logging
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]  # Goes up 2 parent levels

DEFAULT_ADMIN_KEY = "change-me"

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
    database_url: str = "postgresql+psycopg://rag:rag@localhost:5433/rag"
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
