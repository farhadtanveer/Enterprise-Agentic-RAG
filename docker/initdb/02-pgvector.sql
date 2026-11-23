-- P1 (docs/tasks/pgvector-p1.md): the pgvector extension, on a FRESH volume
-- only (docker-entrypoint-initdb.d runs at first init). Runs as the
-- bootstrap superuser — `vector` is NOT a trusted extension, so creation
-- requires it; later bootstrap for an EXISTING cluster is
-- scripts/sql/bootstrap-existing.sql (operator, docs/db-image-swap.md).
-- 02-* runs after 01-test-db.sql, so rag_test exists here.
-- Idempotent by construction (IF NOT EXISTS): a restart of a partially
-- initialized volume cannot fail on this file.
CREATE EXTENSION IF NOT EXISTS vector;
