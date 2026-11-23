# db image swap: `postgres:18-alpine` → `pgvector/pgvector:pg18` (P1)

Non-destructive runbook for the OPERATOR (amended 2026-11-23 after the
first real run: the dump now comes from a TEMPORARY container mounted on
the OLD volume — the old `db` container may already be replaced by the
time you run this — and every data file moves with `pg_dump -f` /
`pg_restore` / `psql -f` INSIDE a container plus `docker cp`, never
through shell redirection or pipes; PowerShell's `>` writes UTF-16 text
and corrupts binary dumps).

The repo ships the compose change (P1): the `db` service now uses
`pgvector/pgvector:pg18` (official Postgres 18 + the pgvector extension)
and points at a NEW named volume `pgdata_pgvector`. The old `pgdata`
volume is **never deleted** — it holds the pre-swap dev data and IS the
rollback. (A safety copy of the old volume, e.g. `pgdata_backup_20261123`,
is cheap insurance the operator may add; nothing in this runbook depends
on it.)

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

**Shell note:** every command has a bash form and, where they differ, a
PowerShell form. Data files NEVER pass through `>` / `<` / pipes — inside
containers via `-f` flags, across the boundary via `docker cp` (SQL files
are text and could pipe safely, but the same container+cp pattern is used
everywhere for one uniform procedure).

Set in `.env` BEFORE step 3 (the fresh init's `docker/initdb/03-roles.sh`
reads them; empty = roles created without a usable password — acceptable
until P4):

```
RAG_OWNER_PASSWORD=...   # python -c "import secrets; print(secrets.token_urlsafe(32))"
RAG_APP_PASSWORD=...
```

## Steps

1. **Record the BEFORE state and stop writes**:

   ```bash
   docker compose ps
   docker compose exec db psql -U rag -d rag -c \
     "SELECT datname, datcollate, datctype FROM pg_database WHERE datname IN ('rag','rag_test');"
   docker compose exec db psql -U rag -d rag -Atc \
     "SELECT 'documents=' || count(*) FROM documents; SELECT 'users=' || count(*) FROM users;"
   docker compose stop app
   ```

   Save both outputs — steps 5–6 compare against them. (If the old `db`
   container is ALREADY gone — i.e. compose has been switched to the
   pgvector image before the dump was taken — skip the compose-exec lines
   and take the before-collation/row-counts from the temporary container
   in step 2 instead.)

2. **Dump from the OLD volume via a TEMPORARY container.** The old volume
   name is `enterprise-agentic-rag_pgdata` (compose project prefix
   included); the OLD image is `postgres:18-alpine`. The dump is written
   INSIDE the temporary container (`pg_dump -f`) and copied out with
   `docker cp` — no `>` anywhere (identical in bash and PowerShell):

   ```bash
   OLDVOL=enterprise-agentic-rag_pgdata
   DUMP="$HOME/rag-pre-pgvector-$(date +%Y%m%d-%H%M%S).dump"

   docker run -d --name pg-old \
     -v "$OLDVOL":/var/lib/postgresql \
     -e POSTGRES_USER=rag -e POSTGRES_PASSWORD=old-volume-temp \
     -e POSTGRES_DB=rag \
     postgres:18-alpine
   # POSTGRES_PASSWORD only matters if the volume is EMPTY (it is not —
   # the cluster on it keeps its own password); set it anyway so a mistake
   # cannot silently initdb a blank volume over the old data.

   docker exec pg-old pg_isready -U rag -d rag     # wait: accepting connections
   docker exec pg-old pg_dump -U rag -Fc -f /tmp/rag.dump rag
   docker cp pg-old:/tmp/rag.dump "$DUMP"
   docker exec pg-old psql -U rag -d rag -Atc \
     "SELECT 'documents=' || count(*) FROM documents; SELECT 'users=' || count(*) FROM users;"
   docker rm -f pg-old        # the temporary container only — the OLD VOLUME STAYS
   ls -l "$DUMP"              # sanity: non-empty
   ```

   (PowerShell: the same commands work verbatim — `docker` takes no shell
   redirection here and `$HOME` exists in PowerShell as `$env:USERPROFILE`:

   ```powershell
   $OLDVOL = "enterprise-agentic-rag_pgdata"
   $DUMP   = "$env:USERPROFILE\rag-pre-pgvector-$(Get-Date -Format yyyyMMdd-HHmmss).dump"
   # … identical docker run / exec / cp / rm lines as above …
   ```

   )

   If the old `db` container still exists and is healthy, you MAY instead
   dump via `docker compose exec db pg_dump -U rag -Fc -f /tmp/rag.dump rag`
   + `docker cp` — the temporary-container path above is the one that
   works in EVERY state, including after compose already replaced the
   container.

3. **Start the new image on the new volume** (compose already points at
   `pgvector/pgvector:pg18` + `pgdata_pgvector` since P1). The fresh init
   runs `docker/initdb/`: `rag_test` created, `CREATE EXTENSION vector` in
   both databases, roles created:

   ```bash
   docker compose up -d db
   docker compose ps                      # healthy
   docker compose exec db psql -U rag -d rag -Atc "SELECT extversion FROM pg_extension WHERE extname='vector';"
   ```

4. **Restore the dump** (docker cp the file IN, `pg_restore` from the
   in-container path — no stdin piping, identical in both shells; the dump
   carries no extension/roles to conflict — the initdb created them;
   `--no-owner` keeps `rag` as the creating user):

   ```bash
   NEWDB=$(docker compose ps -q db | xargs docker inspect --format '{{.Name}}' | tr -d '/')
   docker cp "$DUMP" "$NEWDB":/tmp/rag.dump
   docker compose exec -T db pg_restore -U rag -d rag --no-owner /tmp/rag.dump
   ```

   (PowerShell: replace the first line with
   `$NEWDB = (docker compose ps -q db | ForEach-Object { (docker inspect --format '{{.Name}}' $_).TrimStart('/') })`
   — or simply hardcode the container name `enterprise-agentic-rag-db-1`
   for the `docker cp`.)

5. **Bootstrap roles on the restored cluster + the collation check** (the
   bootstrap file is idempotent — run it for BOTH databases; roles are
   cluster-wide, the extension is per-database and IF NOT EXISTS). The SQL
   file is copied in and run with `psql -f` (`<` does not exist in
   PowerShell):

   ```bash
   docker cp scripts/sql/bootstrap-existing.sql "$NEWDB":/tmp/bootstrap-existing.sql
   set -a; source <(grep '^RAG_.*PASSWORD=' .env | sed 's/^/export /'); set +a   # or set by hand
   for DBNAME in rag rag_test; do
     docker compose exec -T db psql -v ON_ERROR_STOP=1 -U rag -d "$DBNAME" \
       -v owner_pw="$RAG_OWNER_PASSWORD" -v app_pw="$RAG_APP_PASSWORD" \
       -f /tmp/bootstrap-existing.sql
   done
   docker compose exec db psql -U rag -d rag -c \
     "SELECT datname, datcollate, datctype FROM pg_database WHERE datname IN ('rag','rag_test');"
   ```

   (PowerShell:
   ```powershell
   foreach ($dbname in 'rag','rag_test') {
     docker compose exec -T db psql -v ON_ERROR_STOP=1 -U rag -d $dbname `
       -v owner_pw="$env:RAG_OWNER_PASSWORD" -v app_pw="$env:RAG_APP_PASSWORD" `
       -f /tmp/bootstrap-existing.sql
   }
   ```
   with `RAG_OWNER_PASSWORD`/`RAG_APP_PASSWORD` set in the session's
   environment.)

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
   cheap at this scale. Verified live 2026-11-23: dump → restore →
   `REINDEX DATABASE rag;` → `documents=4, users=0` matched, app health
   200.)

6. **Verify and switch over:**

   ```bash
   docker compose up -d app
   curl -s http://127.0.0.1:8081/api/health
   # log in / run a chat question; compare row counts to step 2's dump-side
   # counts for the evidence file
   docker compose exec db psql -U rag -d rag -Atc "SELECT count(*) FROM documents;"
   ```

   (PowerShell: `Invoke-WebRequest http://127.0.0.1:8081/api/health`.)

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

## Status (filled by the operator)

- 2026-11-23: swap EXECUTED and verified — old volume intact (safety
  volume copy `pgdata_backup_20261123` also taken by the operator),
  temp-container dump `rag-OLD-data-20261123.dump` (~27 kB, 68 TOC
  entries), restore + `bootstrap-existing.sql` on `rag` and `rag_test`,
  `REINDEX DATABASE rag;`, row counts matched (`documents=4, users=0`),
  `GET /api/health` → 200 `{"status":"ok"}`.

## CI (no operator action)

The CI `test` job swaps its service image to `pgvector/pgvector:pg18` and
runs `scripts/sql/bootstrap-existing.sql` against its throwaway Postgres
before the suite (ci.yml) — the same file the operator runs, so the two
paths cannot drift.
