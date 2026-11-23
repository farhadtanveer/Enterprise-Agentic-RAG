"""Test bootstrap: force offline, keyless settings before any app import."""
import os

# --- H8 hermetic pins (docs/tasks/g2.md) ---------------------------------------
# Env vars beat the project .env in pydantic-settings, so these make the
# suite immune to whatever the developer's .env or the CI runner carries.
# Empty keys mean any accidental real network call raises instead of
# reaching out. The two INFRA vars (TEST_DATABASE_URL / ALEMBIC_DATABASE_URL
# — where the database lives) are deliberately NOT pinned: they are the
# runner's provision and fail fast when missing (pg_engine below). Every
# other setting a test could lean on is pinned to its code default here.
os.environ["OPENAI_API_KEY"] = ""
os.environ["TAVILY_API_KEY"] = ""
os.environ["PINECONE_API_KEY"] = ""
# The test client speaks plain http to TestClient's http://testserver; the
# cookie jar would refuse to SEND Secure cookies back over http, breaking
# every session test. Cookie-flag VALUES are asserted from raw Set-Cookie
# headers, not from the jar (see tests/test_auth_api.py).
os.environ["COOKIE_SECURE"] = "false"
os.environ["APP_ENV"] = "development"  # production validators must never fire from a dev .env
os.environ["RUN_MIGRATIONS_ON_STARTUP"] = "false"  # importing app.main must never migrate ANY db
os.environ["ENABLE_WEB_FALLBACK"] = "false"  # amended D6 default; web-path tests opt IN explicitly
os.environ["ADMIN_API_KEY"] = "change-me"  # code default; key tests monkeypatch per scenario
os.environ["CHAT_API_KEY"] = ""  # code default; unauthenticated legacy chat in tests
os.environ["LOGIN_THROTTLE_PEPPER"] = ""  # deterministic dev-fallback hashing
os.environ["DATABASE_URL"] = (  # any accidental production-DB use fails loudly, never silently hits a real DB
    "postgresql+psycopg://tests@hermetic.invalid/test"
)
# Belt over the namespace rule (CLAUDE.md): an offline test can never write
# to rag-v2 or enterprise-agentic-rag even through a bug — only multi-user-test.
os.environ["PINECONE_NAMESPACE"] = "multi-user-test"
# The caps the chat/upload tests lean on implicitly (found by the H8
# poisoned-env run: MAX_MESSAGE_LENGTH=7 / MAX_UPLOAD_MB=1 /
# MAX_CONVERSATIONS_PER_USER=1 broke 20 tests). Pinned to their CODE
# defaults; tests that exercise a cap override it explicitly.
os.environ["MAX_UPLOAD_MB"] = "25"
os.environ["MAX_MESSAGE_LENGTH"] = "3000"
os.environ["MAX_MESSAGES_PER_CONVERSATION"] = "200"
os.environ["MAX_CONVERSATIONS_PER_USER"] = "100"
# pgvector P3 pins (code defaults): the transitional backend selector and
# the column dimension (migration 0006's literal) — the contract suite
# asserts BOTH, so a developer .env must never move them.
os.environ["VECTOR_STORE"] = "pinecone"
os.environ["EMBEDDING_DIMENSIONS"] = "1536"

import pytest
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import Base

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def _alembic_upgrade_head(url: str) -> None:
    """Idempotent: brings the test DB to the current migration head."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def pg_engine():
    """Schema-at-head engine for the whole test session (§A11 fixture plan)."""
    if not TEST_DATABASE_URL:
        pytest.fail(
            "TEST_DATABASE_URL is not set. DB tests run inside the compose "
            "network (the dev DB has no host port by design): "
            "docker compose --profile test run --rm test  "
            "(see docs/dev-testing.md)"
        )
    from app.core.db import get_engine

    _alembic_upgrade_head(TEST_DATABASE_URL)
    return get_engine(TEST_DATABASE_URL)


@pytest.fixture()
def db_session(pg_engine):
    """One test = one clean database (§A11): TRUNCATE all tables up front,
    then run against REAL commits — production session semantics.

    Why not the classic savepoint fixture: under SQLAlchemy 2.1.4 a
    session.commit() can end the session's ROOT transaction; the next
    autobegin then takes over the connection's real transaction, so a
    later ``db.rollback()`` (4xx handlers roll back!) wiped the whole
    outer transaction and everything committed before it. Found by the
    B6 admin tests (a revoked-sessions sequence went invisibly empty).
    Truncate-per-test is statement-type agnostic: Core ``update``/
    ``delete`` (throttle, revoke, conversation delete) behave exactly as
    in production. Isolation comes from the truncate, not from rollback."""
    import app.models  # noqa: F401 — register every table on Base.metadata

    connection = pg_engine.connect()
    tables = ", ".join(
        f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables)
    )
    connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    # commit the truncate: without this it stays an open connection-level
    # transaction that every session then JOINS — route-level rollback()
    # would wipe the whole test's data (exactly what the B6 tests caught)
    connection.commit()
    session = Session(bind=connection, expire_on_commit=False)

    yield session

    session.close()
    connection.rollback()  # discard any uncommitted leftovers
    connection.close()


@pytest.fixture()
def joined_session_factory(db_session):
    """Factory for code that builds its own sessions (CLIs).

    B6: plain sessions on the test connection — truncate-per-test makes the
    old savepoint-join tricks unnecessary; CLI commits are real and the
    next test's truncate wipes them."""
    from sqlalchemy.orm import Session as _Session

    connection = db_session.get_bind()

    def factory() -> Session:
        return _Session(bind=connection, expire_on_commit=False)

    return factory

