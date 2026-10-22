# Running the tests locally

## The rules

- **The multi-user schema is tested against real Postgres 18 — never
  SQLite.** JSONB columns, `inet` columns, native enums and `uuidv7()` must
  behave exactly as deployed. CI runs a `postgres:18` service container
  (`.github/workflows/ci.yml`); locally you run the compose `db` service.
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
docker compose up -d db          # Postgres 18 on localhost:5433
                                 # (host 5433 so a locally installed
                                 #  PostgreSQL on 5432 never conflicts)
export TEST_DATABASE_URL=postgresql+psycopg://rag:rag@localhost:5433/rag_test
pytest tests -q
```

- `rag_test` is created automatically by `docker/initdb/01-test-db.sql` on
  first volume init, so test fixtures can never touch the dev `rag` database.
- Tests that need the DB **fail fast with these instructions** when
  `TEST_DATABASE_URL` is unset — that is deliberate, not an accident.
- Pure-unit tests (no DB) keep passing without Docker.
- Alembic state: migration tests upgrade to head and downgrade back to base
  against `rag_test`; nothing is left migrated after a green run.

## Opt-in live Pinecone check (added in B4)

One test module will talk to the real Pinecone **only** when
`RUN_LIVE_PINECONE=1` **and** `PINECONE_API_KEY` are set, and only inside
`PINECONE_TEST_NAMESPACE` (default `multi-user-test`). CI never sets these.

## Migration workflow (for B2+ development)

```bash
export ALEMBIC_DATABASE_URL=$TEST_DATABASE_URL   # or your dev DATABASE_URL
venv/Scripts/python.exe -m alembic revision -m "users and sessions"
venv/Scripts/python.exe -m alembic upgrade head
```

URL resolution order lives in `migrations/env.py`: programmatic config →
`ALEMBIC_DATABASE_URL` → app `DATABASE_URL`. No credentials in
`alembic.ini`.
