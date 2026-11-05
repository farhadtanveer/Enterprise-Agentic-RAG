"""B1: the app engine talks to real Postgres; tests never fall back to SQLite."""
import os

import pytest
from sqlalchemy import text

TEST_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture(scope="module")
def engine():
    if not TEST_URL:
        pytest.fail(
            "TEST_DATABASE_URL is not set. DB tests run inside the compose "
            "network (the dev DB has no host port by design): "
            "docker compose --profile test run --rm test  "
            "(see docs/dev-testing.md)"
        )
    from app.core.db import get_engine

    return get_engine(TEST_URL)


def test_connects_to_real_postgres(engine):
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version()")).scalar_one()
    assert "PostgreSQL" in version  # would fail on any SQLite fallback


def test_uuidv7_available_and_is_v7(engine):
    # Postgres 18 native generator; Python 3.13 stdlib has no uuid7, so the
    # DB is the single ID authority for every table added from B2 on.
    with engine.connect() as conn:
        u = conn.execute(text("SELECT uuidv7()")).scalar_one()
    assert str(u)[14] == "7"  # version nibble


def test_engine_is_cached_per_url(engine):
    from app.core.db import get_engine

    assert get_engine(TEST_URL) is engine


def test_session_roundtrip_and_close(engine):
    from app.core.db import get_sessionmaker

    session = get_sessionmaker(TEST_URL)()
    try:
        assert session.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        session.close()
        # close() releases the connection back to the pool; the session is
        # reset (no open transaction) and reusable.
        assert not session.in_transaction()


def test_engine_hides_bound_parameters_from_errors():
    """Privacy rule: question/answer text must never reach logs or exception
    messages. Bound DB values are the last place content could leak through a
    raised StatementError — hide_parameters=True renders placeholders only.
    The contrast engine (no flag) proves the assertion is meaningful."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import DBAPIError

    from app.core.db import get_engine

    marker = "SECRET-MARKER-DB-CONTENT"
    bad_sql = text("INSERT INTO definitely_missing_table VALUES (:content)")

    # contrast: a plain engine LEAKS the bound value into the error text —
    # this documents why hide_parameters matters (test-only engine).
    plain = create_engine("sqlite://")
    with pytest.raises(DBAPIError) as leaked:
        with plain.connect() as c:
            c.execute(bad_sql, [{"content": marker}])
    assert marker in str(leaked.value)

    # the app factory: same failure, value gone
    app_engine = get_engine("sqlite:///file:hideparams-test?mode=memory&uri=true")
    with pytest.raises(DBAPIError) as hidden:
        with app_engine.connect() as c:
            c.execute(bad_sql, [{"content": marker}])
    assert marker not in str(hidden.value)
