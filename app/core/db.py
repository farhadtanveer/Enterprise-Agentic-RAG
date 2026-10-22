"""SQLAlchemy engine/session factory for the multi-user backend (B1).

Engines and sessionmakers are lazily built, cached singletons so importing
app modules never opens a connection — the schema is owned by Alembic, not
create_all, and nothing should touch the DB at import time.

Transaction discipline (docs/backend-design.md §A8): sessions are
short-lived and callers commit explicitly. The FastAPI dependency below
deliberately does NOT auto-commit at request end — the chat flow needs
short TX1/TX2 boundaries with no connection held across the graph run.
"""
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all multi-user models (tables land in B2+)."""


@lru_cache(maxsize=None)
def get_engine(url: str | None = None):
    """Engine for the app database, or an explicit URL (tests, Alembic).

    pool_pre_ping replaces connections killed by idle timeouts instead of
    raising on first use after a gap.
    """
    return create_engine(url or get_settings().database_url, pool_pre_ping=True)


@lru_cache(maxsize=None)
def get_sessionmaker(url: str | None = None):
    # expire_on_commit=False: attributes stay usable after commit without a
    # second query — the short-transaction pattern commits often.
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


def get_db():
    """FastAPI dependency: one session per request, explicit commits."""
    db = get_sessionmaker()()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
