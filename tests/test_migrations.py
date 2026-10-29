"""B1: the Alembic chain upgrades and downgrades a real database.

B2 additions: dynamic head assertion, schema-sanity tests for the auth
tables (uuidv7 defaults, CHECKs, inet), and a finalizer that restores head
after the downgrade test — the conftest pg fixtures rely on the test DB
being at head, so this module must leave it that way (docs/dev-testing.md).
"""
import os
from pathlib import Path

import pytest
from sqlalchemy import text

TEST_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture(scope="module")
def alembic_cfg():
    if not TEST_URL:
        pytest.fail(
            "TEST_DATABASE_URL is not set. DB tests run inside the compose "
            "network (the dev DB has no host port by design): "
            "docker compose --profile test run --rm test  "
            "(see docs/dev-testing.md)"
        )
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_URL)
    # Whatever the downgrade test below does, leave the DB at head — the
    # conftest pg_engine fixture and every DB-backed module assume it.
    yield cfg
    from alembic import command

    command.upgrade(cfg, "head")


def test_upgrade_to_head_stamps_current_head(alembic_cfg):
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine

    command.upgrade(alembic_cfg, "head")
    expected_head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
    engine = create_engine(TEST_URL)
    try:
        with engine.connect() as conn:
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert ver == expected_head
    finally:
        engine.dispose()


def test_upgrade_is_idempotent(alembic_cfg):
    from alembic import command

    command.upgrade(alembic_cfg, "head")  # second run: no-op, must not raise


# --- B2 schema sanity (§A11 test 16) — run while the DB is at head --------
# Pattern: every probe runs in a single transaction that is always rolled
# back, so these tests write nothing and never leave an aborted TX behind.


def _engine():
    from app.core.db import get_engine

    return get_engine(TEST_URL)


def test_uuidv7_default_produces_v7_ids():
    from sqlalchemy import insert

    from app.models.user import User

    conn = _engine().connect()
    try:
        user_id = conn.execute(
            insert(User).values(username="b2-schema-probe", password_hash="x", role="user")
        ).inserted_primary_key[0]
        assert str(user_id)[14] == "7"  # version nibble
    finally:
        conn.rollback()
        conn.close()


def test_role_check_rejects_bad_value():
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError

    from app.models.user import User

    conn = _engine().connect()
    try:
        conn.execute(
            insert(User).values(username="b2-bad-role", password_hash="x", role="superadmin")
        )
        pytest.fail("role CHECK did not reject 'superadmin'")
    except IntegrityError:
        pass
    finally:
        conn.rollback()
        conn.close()


def test_username_check_rejects_uppercase():
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError

    from app.models.user import User

    conn = _engine().connect()
    try:
        conn.execute(
            insert(User).values(username="Not-Lowercase", password_hash="x", role="user")
        )
        pytest.fail("username CHECK did not reject uppercase")
    except IntegrityError:
        pass
    finally:
        conn.rollback()
        conn.close()


def test_token_hash_check_rejects_non_sha256():
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError

    from app.models.session import Session as SessionRow
    from app.models.user import User

    soon = datetime.now(timezone.utc) + timedelta(days=1)
    conn = _engine().connect()
    try:
        user_id = conn.execute(
            insert(User).values(username="b2-hash-probe", password_hash="x", role="user")
        ).inserted_primary_key[0]
        conn.execute(
            insert(SessionRow).values(
                user_id=user_id,
                token_hash="short-not-hash",
                expires_at=soon,
                idle_expires_at=soon,
            )
        )
        pytest.fail("token_hash CHECK did not reject a non-sha256 value")
    except IntegrityError:
        pass
    finally:
        conn.rollback()
        conn.close()


def test_inet_column_rejects_garbage():
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import insert
    from sqlalchemy.exc import DataError

    from app.models.login_throttle import LoginThrottle

    conn = _engine().connect()
    try:
        conn.execute(
            insert(LoginThrottle).values(
                username_hash="a" * 64,
                ip="not-an-ip",
                fail_count=1,
                last_failure_at=datetime.now(timezone.utc),
                throttle_until=datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        )
        pytest.fail("inet column did not reject garbage")
    except DataError:
        pass
    finally:
        conn.rollback()
        conn.close()


def test_audit_event_defaults_and_outcome_check():
    from sqlalchemy import insert, select
    from sqlalchemy.exc import IntegrityError

    from app.models.audit_event import AuditEvent

    conn = _engine().connect()
    try:
        details = conn.execute(
            insert(AuditEvent)
            .values(event_type="b2_probe_event", outcome="ok")
            .returning(AuditEvent.details)
        ).scalar_one()
        assert details == {}  # server_default '{}'::jsonb
        row_id = conn.execute(
            select(AuditEvent.id).where(AuditEvent.event_type == "b2_probe_event")
        ).scalar_one()
        assert row_id > 0
    finally:
        conn.rollback()
        conn.close()

    conn = _engine().connect()
    try:
        conn.execute(
            insert(AuditEvent).values(event_type="b2_probe_event", outcome="maybe")
        )
        pytest.fail("outcome CHECK did not reject 'maybe'")
    except IntegrityError:
        pass
    finally:
        conn.rollback()
        conn.close()


def test_downgrade_to_base_unstamps_the_database(alembic_cfg):
    from alembic import command
    from sqlalchemy import create_engine

    command.downgrade(alembic_cfg, "base")
    engine = create_engine(TEST_URL)
    try:
        with engine.connect() as conn:
            table = conn.execute(
                text("SELECT to_regclass('public.alembic_version')")
            ).scalar_one()
            # alembic 1.20 keeps an EMPTY version table on downgrade-to-base;
            # the invariant is "no stamped revision", not "no table".
            rows = (
                0
                if table is None
                else conn.execute(text("SELECT count(*) FROM alembic_version")).scalar_one()
            )
        assert rows == 0
    finally:
        engine.dispose()
        # the module-scoped alembic_cfg finalizer restores head after this
