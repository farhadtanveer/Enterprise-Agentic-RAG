#!/bin/bash
# P1 (docs/tasks/pgvector-p1.md): the rag_owner / rag_app roles, on a FRESH
# volume only (docker-entrypoint-initdb.d runs at first init; 03-* runs
# after 02-pgvector.sql). Idempotent (DO blocks). Passwords come from the
# ENVIRONMENT — never from this repo. The roles sit UNUSED until P4, when
# DATABASE_URL / ALEMBIC_DATABASE_URL switch to them (docs/db-image-swap.md
# and docs/pgvector-design.md §5.1); until then an empty password value is
# acceptable (the roles cannot log in without one).
set -euo pipefail

# The extension in the TEST database too (01-test-db.sql created rag_test;
# 02-pgvector.sql only reached the default database). The offline suite's
# extension test (tests/test_pgvector_extension.py) runs against rag_test.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname rag_test <<-'EOSQL'
	CREATE EXTENSION IF NOT EXISTS vector;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-'EOSQL'
	DO $roles$
	BEGIN
	  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_owner') THEN
	    CREATE ROLE rag_owner LOGIN;
	  END IF;
	  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
	    CREATE ROLE rag_app LOGIN;
	  END IF;
	END
	$roles$;
	-- Baseline grants ONLY (PG15+: CREATE on schema public is revoked from
	-- PUBLIC by default). rag_owner will own/migrate the schema; rag_app is
	-- the future runtime DML role (P4 adds its real grants and the URL
	-- switch — nothing else is granted here, on purpose).
	GRANT USAGE, CREATE ON SCHEMA public TO rag_owner;
	GRANT USAGE ON SCHEMA public TO rag_app;
EOSQL

set_password() { # $1 = role, $2 = password env var name
	local role="$1" var="$2" pw="${!2:-}"
	if [ -n "$pw" ]; then
		psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
			-v pg_role="$role" -v pg_pw="$pw" <<-'EOSQL'
			ALTER ROLE :"pg_role" PASSWORD :'pg_pw';
EOSQL
	else
		echo "03-roles.sh: $var is empty — role '$role' created WITHOUT a password (set it in .env before the P4 switch)" >&2
	fi
}

set_password rag_owner RAG_OWNER_PASSWORD
set_password rag_app RAG_APP_PASSWORD
