"""B1: the Alembic chain upgrades and downgrades a real database."""
import os
from pathlib import Path

import pytest

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
    return cfg


def test_upgrade_to_head_stamps_baseline(alembic_cfg):
    from alembic import command
    from sqlalchemy import create_engine, text

    command.upgrade(alembic_cfg, "head")
    engine = create_engine(TEST_URL)
    try:
        with engine.connect() as conn:
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert ver == "0001"
    finally:
        engine.dispose()


def test_upgrade_is_idempotent(alembic_cfg):
    from alembic import command

    command.upgrade(alembic_cfg, "head")  # second run: no-op, must not raise


def test_downgrade_to_base_unstamps_the_database(alembic_cfg):
    from alembic import command
    from sqlalchemy import create_engine, text

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
