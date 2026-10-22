"""Alembic environment: URLs from app settings/env, metadata from app Base.

Resolution order for the target database:
1. ``sqlalchemy.url`` set programmatically on the Config (tests do this)
2. ``ALEMBIC_DATABASE_URL`` env var
3. the app's ``DATABASE_URL`` setting

Never a credential in alembic.ini — this file is tracked.
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.core.config import get_settings
from app.core.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return (
        config.get_main_option("sqlalchemy.url")
        or os.environ.get("ALEMBIC_DATABASE_URL")
        or get_settings().database_url
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_url(), pool_pre_ping=True)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
