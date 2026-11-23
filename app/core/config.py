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
    # B4 namespace migration (§A7): new metadata scheme lives in `rag-v2`;
    # the old namespace is left 100% untouched until the operator clears it
    # AFTER verifying re-ingest (manual, namespace-scoped only).
    pinecone_namespace: str = "rag-v2"
    # --- pgvector migration (docs/pgvector-design.md §2.2/§3; DECISIONS
    # #4/#7) ---
    # Transitional backend selector (P3–P7): "pinecone" stays the default
    # until the golden-question gate passes; "pgvector" switches the store
    # (P7 cutover). The Pinecone adapter, this setting and the $in dict all
    # die at P10 — a migration device with a mandated removal commit, not a
    # permanent mode.
    vector_store: str = "pinecone"
    # The embedding dimension the document_chunks column was created with
    # (migration 0006 wrote the same literal; text-embedding-3-small,
    # DECISION #7). The PgVectorStore refuses to store or query a vector
    # whose dimension differs — a model change needs the §7 dual-column
    # procedure, never a silent dimension drift.
    embedding_dimensions: int = 1536
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
    upload_dir: str = str(BASE_DIR / "uploads")

    # --- Multi-user foundation (docs/backend-design.md, Phase B) ---
    # Dev default is the compose-network hostname (db:5432): the dev DB has
    # no host port by design (LAN exposure). CI and prod override via env.
    database_url: str = "postgresql+psycopg://rag:rag@db:5432/rag"
    # Secure cookie flag: keep true everywhere except local plain-http dev.
    cookie_secure: bool = True
    session_idle_hours: int = 168  # 7 days sliding
    session_absolute_days: int = 30  # hard cap from login
    # D6 kill switch, AMENDED by §A14 (decided 2026-11-09): FALSE by
    # default — weak KB evidence stops honestly at "insufficient" and the
    # question text never reaches Tavily. Opt in per deployment that
    # accepts external search.
    enable_web_fallback: bool = False
    max_upload_mb: int = 25  # 413 beyond this; enforced while streaming
    # --- G2 A2 personal upload quotas (§A14.6, DECISION #7) ---
    # Enforcement order: per-file (413) → total bytes (409 quota_total) →
    # document count (409 quota_docs) → hourly window (429 + Retry-After).
    # Admins are EXEMPT from all four (org uploads are their job); the
    # global MAX_UPLOAD_MB above still binds everyone.
    user_upload_max_mb: int = 25  # per file, ≤ max_upload_mb
    user_upload_total_mb: int = 250  # per user across their PRIVATE docs
    user_upload_max_docs: int = 50  # per user, private docs
    user_uploads_per_hour: int = 10  # per-user sliding window on uploads
    # A5.2: a 'pending' row older than this is orphaned (its process died
    # between TX1 and TX2) — the stale-pending sweep marks it failed so it
    # stops consuming quota. In-flight uploads are minutes old; 30 gives a
    # wide margin over the 60s parser timeout.
    user_upload_pending_stale_minutes: int = 30
    # --- G2 A3 parser subprocess limits (§A14.6, DECISION #8) ---
    # The parser runs in a spawned subprocess: a poisoned file can kill the
    # CHILD, never the app. Any child failure (timeout, kill, cap refusal)
    # maps to the generic 422 "File could not be parsed".
    parser_timeout_seconds: int = 60  # hard wall-clock deadline per parse
    # Child RLIMIT_AS (POSIX; skipped elsewhere). 1GB, not less: the limit
    # counts MAPPED address space (imported native libs included), and a
    # live PyPDFLoader run needs real headroom above that. Still bounds any
    # runaway decompression far below what a 25MB upload could expand to.
    parser_max_memory_mb: int = 1024
    parser_max_pages: int = 500  # parsed pages/sections kept per document
    parser_max_chars: int = 2000000  # extracted characters kept per document
    parser_max_docx_uncompressed_mb: int = 200  # zip-bomb guard, checked pre-parse
    # Global ingest concurrency (DECISION #7 addition): caps simultaneous
    # parse/embed runs so N parallel uploads cannot starve the process.
    ingest_concurrency: int = 2
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
    # --- B3 chat caps (review requirement e; all configurable) ---
    max_message_length: int = 3000  # chars per question (422 beyond)
    max_messages_per_conversation: int = 200  # 409 beyond
    max_conversations_per_user: int = 100  # 409 beyond

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
