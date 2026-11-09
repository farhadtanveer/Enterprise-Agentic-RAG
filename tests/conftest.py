"""Test bootstrap: force offline, keyless settings before any app import."""
import os

# Env vars beat the project .env in pydantic-settings. Empty keys mean any
# accidental real network call raises instead of reaching out.
os.environ["OPENAI_API_KEY"] = ""
os.environ["TAVILY_API_KEY"] = ""
os.environ["PINECONE_API_KEY"] = ""
# (B5/C8: the legacy audit writer is gone — nothing writes audit.db anymore.)
# The test client speaks plain http to TestClient's http://testserver; the
# cookie jar would refuse to SEND Secure cookies back over http, breaking
# every session test. Cookie-flag VALUES are asserted from raw Set-Cookie
# headers, not from the jar (see tests/test_auth_api.py).
os.environ["COOKIE_SECURE"] = "false"

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

