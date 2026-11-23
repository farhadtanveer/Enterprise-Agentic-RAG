-- P1 (docs/tasks/pgvector-p1.md): idempotent bootstrap for an EXISTING
-- Postgres volume/cluster — the fresh-volume path is docker/initdb/ (runs
-- only at first init). The operator runs THIS file with psql per
-- docs/db-image-swap.md; CI runs it against its throwaway service so the
-- extension test (tests/test_pgvector_extension.py) can pass.
--
-- Run as the bootstrap SUPERUSER (the extension is not trusted) with the
-- password variables — `psql variables are substituted in the ALTER ROLE
-- lines only, never inside the $do$ block`:
--
--   psql -v ON_ERROR_STOP=1 \
--        -v owner_pw='…' -v app_pw='…' \
--        -f scripts/sql/bootstrap-existing.sql
--
-- Connect to the database that needs the extension (CI: rag_test; the
-- runbook: `rag` then `rag_test`). Roles are cluster-wide; CREATE
-- EXTENSION is per-database and IF-NOT-EXISTS here, so re-running this
-- file is always safe.
DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_owner') THEN
    CREATE ROLE rag_owner LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
    CREATE ROLE rag_app LOGIN;
  END IF;
END
$do$;

ALTER ROLE rag_owner PASSWORD :'owner_pw';
ALTER ROLE rag_app PASSWORD :'app_pw';

-- Baseline grants (same policy as docker/initdb/03-roles.sh; P4 adds the
-- rest). PG15+: CREATE on schema public is revoked from PUBLIC.
GRANT USAGE, CREATE ON SCHEMA public TO rag_owner;
GRANT USAGE ON SCHEMA public TO rag_app;

CREATE EXTENSION IF NOT EXISTS vector;
