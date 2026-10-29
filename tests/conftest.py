"""Test bootstrap: force offline, keyless settings before any app import."""
import os
import tempfile

# Env vars beat the project .env in pydantic-settings. Empty keys mean any
# accidental real network call raises instead of reaching out.
os.environ["OPENAI_API_KEY"] = ""
os.environ["TAVILY_API_KEY"] = ""
os.environ["PINECONE_API_KEY"] = ""
# Keep the real data/audit.db untouched by tests.
os.environ["AUDIT_DB_PATH"] = os.path.join(tempfile.gettempdir(), "audit-test.db")
# The test client speaks plain http to TestClient's http://testserver; the
# cookie jar would refuse to SEND Secure cookies back over http, breaking
# every session test. Cookie-flag VALUES are asserted from raw Set-Cookie
# headers, not from the jar (see tests/test_auth_api.py).
os.environ["COOKIE_SECURE"] = "false"

import pytest
from pathlib import Path
from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransactionOrigin

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
    """One test = one transaction, rolled back at the end (§A11).

    App code commits explicitly (§A8), so the session joins the external
    transaction via a SAVEPOINT that is restarted after every commit —
    commits appear real to the code but the outer transaction is never
    ended, and the final rollback wipes everything the test wrote.
    """
    connection = pg_engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(sess, transaction):  # pragma: no cover - plumbing
        # SQLAlchemy 2.1: origin is public (the old _parent_nil attr is gone).
        # Restart only real savepoints of a still-active session — never
        # during close() teardown.
        if (
            transaction.origin is SessionTransactionOrigin.BEGIN_NESTED
            and transaction.parent is not None
            and sess.is_active
        ):
            sess.begin_nested()

    yield session

    session.close()
    outer.rollback()
    connection.close()


@pytest.fixture()
def joined_session_factory(db_session):
    """Factory for code that builds its own sessions (CLIs).

    Returns sessions bound to the test connection so CLI runs land inside
    the same rolled-back transaction; the savepoint listener is copied onto
    each new session so its commit() doesn't end the outer transaction.
    """
    from sqlalchemy.orm import Session as _Session

    connection = db_session.get_bind()

    def factory() -> Session:
        s = _Session(bind=connection, expire_on_commit=False)
        s.begin_nested()

        @event.listens_for(s, "after_transaction_end")
        def restart(sess, transaction):  # pragma: no cover - plumbing
            if (
                transaction.origin is SessionTransactionOrigin.BEGIN_NESTED
                and transaction.parent is not None
                and sess.is_active
            ):
                sess.begin_nested()

        return s

    return factory

