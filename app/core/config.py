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
    audit_db_path: str = str(BASE_DIR / "data" / "audit.db")
    upload_dir: str = str(BASE_DIR / "uploads")
    sample_kb_dir: str = str(BASE_DIR / "data" / "sample_kb")

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
