# Running the tests locally

## The rules

- **The multi-user schema is tested against real Postgres 18 — never
  SQLite.** JSONB columns, `inet` columns, native enums and `uuidv7()` must
  behave exactly as deployed. CI runs a `postgres:18` service container
  (`.github/workflows/ci.yml`); locally you run the compose `db` service.
- **The dev DB has no host port, on purpose.** This Docker Desktop build
  stalls loopback port publishes (~130 s per query even after a restart),
  and a bare `0.0.0.0` binding would expose the dev DB to the LAN. So
  everything that needs the DB runs **inside the compose network**; the only
  way in from the host is `docker compose exec db psql …`.
- **LLM, Tavily and Pinecone are mocked offline** (`tests/conftest.py` forces
  empty API keys; fake vectorstore pattern as in `tests/test_ingest_ids.py`).
  No test ever needs an API key.
- The legacy audit-log tests (`tests/test_audit_log.py`) still use a temp
  **SQLite** file on purpose: they test the legacy `query_audit` component
  that Phase B5 retires. The no-SQLite rule applies to everything new.

## Setup (once)

```bash
python -m venv venv
venv\Scripts\activate            # Windows / Git Bash
pip install -r requirements-dev.txt
```

## Every session

```bash
docker compose --profile test run --rm test
```

That builds the `test` stage of the Dockerfile (runtime deps + pinned dev
tools — `requirements-dev.txt`), starts the `db` service (health-gated via
`depends_on`), bind-mounts this repo into the container, and runs
`pytest tests -q` against `db:5432` with `TEST_DATABASE_URL` already set.
`rag_test` is created automatically by `docker/initdb/01-test-db.sql` on
first volume init, so test fixtures can never touch the dev `rag` database.

- First run builds the image (a couple of minutes); afterwards it's cached
  and fast. Source changes need **no rebuild** — the repo is bind-mounted.
- Tests that need the DB **fail fast** with these instructions when
  `TEST_DATABASE_URL` is unset — that is deliberate, not an accident. On the
  host (outside compose) they cannot reach the DB, so use the compose
  command above.
- Pure-unit tests (no DB) still run from the host venv:
  `pytest tests -q --ignore=tests/test_db.py --ignore=tests/test_migrations.py`
  (extend the ignore list as DB-backed modules grow).
- Alembic state: migration tests upgrade to head and downgrade back to base
  against `rag_test`; nothing is left migrated after a green run.

## Migrations (B2+ development)

```bash
docker compose --profile test run --rm test \
  alembic revision --autogenerate -m "users and sessions"
docker compose --profile test run --rm test alembic upgrade head
```

URL resolution order lives in `migrations/env.py`: programmatic config →
`ALEMBIC_DATABASE_URL` (set in the compose `test` service) → app
`DATABASE_URL`. No credentials in `alembic.ini`.

## Temporary host port (only when a host tool really needs it)

```yaml
# in docker-compose.yml, db service — REMOVE AFTER USE:
    ports:
      - "127.0.0.1:5433:5432"
```

Loopback-only, never bare `5433:5432` (LAN exposure). Expect slowness on
this Docker Desktop build (the relay stall described above); `docker compose
down && up` afterwards to restore the no-publish posture.

## Opt-in live Pinecone check (added in B4)

One test module will talk to the real Pinecone **only** when
`RUN_LIVE_PINECONE=1` **and** `PINECONE_API_KEY` are set, and only inside
`PINECONE_TEST_NAMESPACE` (default `multi-user-test`). CI never sets these.
