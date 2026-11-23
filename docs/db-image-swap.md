# db image swap: `postgres:18-alpine` → `pgvector/pgvector:pg18` (P1)

Non-destructive runbook for the OPERATOR. The repo ships the compose change
(P1): the `db` service now uses `pgvector/pgvector:pg18` (official Postgres
18 + the pgvector extension) and points at a NEW named volume
`pgdata_pgvector`. The old `pgdata` volume is **never deleted** — it holds
the pre-swap dev data and IS the rollback.

**Why a new volume:** Alpine (musl) and Debian (glibc) build Postgres with
different collation libraries. A cluster initdb'd under musl and then run
under glibc sorts text with different collation weights — never reuse the
old data directory across the swap. `pg_dump` → new volume → restore keeps
the data; the checks below verify the collation story honestly.

**Why roles/bootstrap:** passwords and roles are cluster-level state — a
single-database `pg_dump` does NOT carry the `vector` extension or the
`rag_owner`/`rag_app` roles. The fresh volume's initdb scripts create them
(`docker/initdb/`); for a restored cluster run
`scripts/sql/bootstrap-existing.sql` (idempotent, safe to re-run).

Set in `.env` BEFORE step 2 (`docker/initdb/03-roles.sh` reads them; empty
= roles created without a usable password — acceptable until P4):

```
RAG_OWNER_PASSWORD=...
RAG_APP_PASSWORD=...
```

## Steps

1. **Record the BEFORE state and stop writes** (app container down; the db
   stays up for the dump):

   ```bash
   docker compose ps
   docker compose exec db psql -U rag -d rag -c \
     "SELECT datname, datcollate, datctype FROM pg_database WHERE datname IN ('rag','rag_test');"
   # save this output — step 5 compares it
   docker compose stop app
   ```

2. **Dump** (custom format, piped to a host file OUTSIDE git; `data/` is
   git-ignored and operator-owned — an external disk is better still):

   ```bash
   DUMP="$HOME/rag-pre-pgvector-$(date +%Y%m%d-%H%M%S).dump"
   docker compose exec -T db pg_dump -U rag -Fc rag > "$DUMP"
   ls -l "$DUMP"   # sanity: non-empty
   ```

   `rag_test` is NOT dumped — test data is disposable; the fresh init
   recreates the database (see step 5).

3. **Start the new image on the new volume** (compose already points at
   `pgvector/pgvector:pg18` + `pgdata_pgvector` since P1). The fresh init
   runs `docker/initdb/`: `rag_test` created, `CREATE EXTENSION vector` in
   both databases, roles created:

   ```bash
   docker compose up -d db
   docker compose ps                      # healthy
   docker compose exec db psql -U rag -d rag -Atc "SELECT extversion FROM pg_extension WHERE extname='vector';"
   ```

4. **Restore the dump** (dump has no extension/roles to conflict — the
   initdb created them; `--no-owner` keeps `rag` as the creating user):

   ```bash
   cat "$DUMP" | docker compose exec -T db pg_restore -U rag -d rag --no-owner
   ```

5. **Bootstrap roles on the restored cluster + the collation check** (the
   bootstrap file is idempotent — run it for BOTH databases; roles are
   cluster-wide, the extension is per-database and IF NOT EXISTS):

   ```bash
   . <(grep '^RAG_.*PASSWORD=' .env | sed 's/^/export /')   # or set by hand
   for DBNAME in rag rag_test; do
     docker compose exec -T db psql -v ON_ERROR_STOP=1 -U rag -d "$DBNAME" \
       -v owner_pw="$RAG_OWNER_PASSWORD" -v app_pw="$RAG_APP_PASSWORD" \
       < scripts/sql/bootstrap-existing.sql
   done
   docker compose exec db psql -U rag -d rag -c \
     "SELECT datname, datcollate, datctype FROM pg_database WHERE datname IN ('rag','rag_test');"
   ```

   **If `datcollate`/`datctype` changed vs step 1** (they will — musl →
   glibc), textual indexes built under the old libc may order differently:

   ```bash
   # which libc collations changed version:
   docker compose exec db psql -U rag -d rag -c \
     "SELECT collname, collversion, pg_collation_actual_version(oid)
        FROM pg_collation WHERE collprovider='c'
          AND collversion IS DISTINCT FROM pg_collation_actual_version(oid);"
   # refresh each listed collation, then rebuild the indexes:
   docker compose exec db psql -U rag -d rag -c \
     'ALTER COLLATION pg_catalog."en_US.utf8" REFRESH VERSION;'   # per listed collation
   docker compose exec db psql -U rag -d rag -c 'REINDEX DATABASE rag;'
   ```

   (REINDEX is the fallback and the honest default after a libc swap —
   cheap at this scale.)

6. **Verify and switch over:**

   ```bash
   docker compose up -d app
   curl -s http://127.0.0.1:8081/api/health
   # log in / run a chat question; document row counts (documents, users)
   # against the dump size for the evidence file
   docker compose exec db psql -U rag -d rag -Atc "SELECT count(*) FROM documents;"
   ```

7. **Record the AFTER state** in `docs/evidence/pgvector-p1-evidence.md`:
   image tag + digest (`docker image inspect pgvector/pgvector:pg18
   --format '{{index .RepoDigests 0}}'`), `extversion`, `SELECT version()`,
   the before/after collation table, and the row counts.

## Rollback

```bash
docker compose stop app db
git revert <the P1 commit>   # compose points back at postgres:18-alpine + pgdata
docker compose up -d db      # the OLD volume — untouched since step 1
docker compose up -d app
```

The old volume was never deleted and the dump stays as a second copy.
Nothing in the new volume is consulted by the rollback path.

## CI (no operator action)

The CI `test` job swaps its service image to `pgvector/pgvector:pg18` and
runs `scripts/sql/bootstrap-existing.sql` against its throwaway Postgres
before the suite (ci.yml) — the same file the operator runs, so the two
paths cannot drift.
