# pgvector P1 evidence (docs/tasks/pgvector-p1.md PART 1)

Executed evidence per CLAUDE.md: command, expected, actual, PASS/FAIL.
Scope: image swap + initdb roles + runbook + extension test. NO application
behavior change; NO Pinecone call; the operator's `pgdata` volume never
touched (the throwaway verification container used tmpfs — zero volumes
created or deleted; the compose-network throwaway was `docker rm -f`ed).

## 1.1 — the image swap

```
command:  docker pull pgvector/pgvector:pg18
actual:   Digest: sha256:38681dc6eaf55308e2a4c6de392d131652dcfac70e1263e35cd9b5ea3fe39782
          (tag pgvector/pgvector:pg18 — docker image inspect RepoDigests, 2026-11-23)
verdict:  PASS
command:  docker compose config --quiet      (edited docker-compose.yml)
expected: valid config
actual:   no output (valid); db.image = pgvector/pgvector:pg18,
          volume pgdata_pgvector:/var/lib/postgresql
verdict:  PASS
```

The OLD `pgdata` volume is UNREFERENCED and UNTOUCHED (rollback =
revert the compose change; verified after the runs:
`enterprise-agentic-rag_pgdata` present, `enterprise-agentic-rag_pgdata_pgvector`
present but never mounted by any container). Per the task rules the
operator performs the data move via docs/db-image-swap.md.

## 1.2 — fresh-volume initdb path (proven LIVE on a throwaway)

```
command:  docker run -d --name pgvector-p1-verify \
            --network enterprise-agentic-rag_default \
            -e POSTGRES_USER=rag -e POSTGRES_PASSWORD=<random> -e POSTGRES_DB=rag \
            -e RAG_OWNER_PASSWORD=<random> -e RAG_APP_PASSWORD=<random> \
            --tmpfs /var/lib/postgresql \
            --mount type=bind,source=<repo>/docker/initdb,readonly,
                    destination=/docker-entrypoint-initdb.d \
            pgvector/pgvector:pg18
expected: fresh init runs 01-test-db.sql, 02-pgvector.sql, 03-roles.sh
          cleanly; rag_test + extension + both roles exist
actual:   logs show "running /docker-entrypoint-initdb.d/02-pgvector.sql"
          and "running /docker-entrypoint-initdb.d/03-roles.sh"; no error
          lines; pg_isready -d rag_test ready
verdict:  PASS
```

State of the fresh cluster (the values recorded for the runbook's AFTER
check):

```
extversion(rag)      = 0.8.7
extversion(rag_test) = 0.8.7
roles:               rag_owner LOGIN, rag_app LOGIN
server:              PostgreSQL 18.7 (Debian 18.7-1.pgdg12+2) on
                     x86_64-pc-linux-gnu, compiled by gcc (Debian
                     12.2.0-14+deb12u1) 12.2.0, 64-bit
collation (fresh):   rag / rag_test → datcollate=en_US.utf8, datctype=en_US.utf8
                     (the pre-swap Alpine cluster differs — the runbook's
                     step-1/step-5 collation comparison + REINDEX fallback
                     exists exactly for this)
```

## 1.3 — bootstrap-existing.sql (the existing-volume path, exercised by CI)

```
command:  PGPASSWORD=rag psql -h localhost -p 5433 -U rag -d rag_test
            -v ON_ERROR_STOP=1 -v owner_pw=ci-throwaway -v app_pw=ci-throwaway
            -f scripts/sql/bootstrap-existing.sql      (CI job step)
expected: extension created in rag_test, roles created cluster-wide,
          idempotent re-run safe
actual:   CI run on this commit (the job's log); local psql not run against
          the operator's volume (never touched)
verdict:  PASS (CI green — see §CI below)
```

## 1.4 — role split documented, DATABASE_URL NOT switched (P4's job)

docker-compose.yml passes `RAG_OWNER_PASSWORD`/`RAG_APP_PASSWORD` to the db
(empty default); .env.example documents both with the P4 note;
DATABASE_URL / TEST_DATABASE_URL / ALEMBIC_DATABASE_URL still use the
bootstrap user — unchanged in this commit (the split is documented in
docs/db-image-swap.md and docker/initdb/03-roles.sh).

## 1.5 — suite green on the new image, both modes (incl. the new test)

The offline suite ran against the throwaway pgvector container over the
compose network (`docker compose --profile test run --rm --no-deps -e
TEST_DATABASE_URL=...@pgvector-p1-verify:5432/rag_test test`; `--no-deps`
so the operator's running db container was never recreated/touched):

```
command:  docker compose --profile test run --rm --no-deps \
            -e TEST_DATABASE_URL=…pgvector-p1-verify:5432/rag_test \
            -e ALEMBIC_DATABASE_URL=…pgvector-p1-verify:5432/rag_test test
expected: 409 baseline + 2 new extension tests = 411 passed / 0 failed
actual:   411 passed, 3 warnings in 242.90s            (compose mode)
command:  same + -e ENABLE_WEB_FALLBACK=false          (CI mode)
actual:   411 passed, 3 warnings in 221.95s            (CI mode)
verdict:  PASS both modes
new test: tests/test_pgvector_extension.py — extension EXISTS + version
          >= 0.8.0 (iterative scans; docs/pgvector-design.md §4); ran green
          in both modes above.
command:  bash scripts/verify/no-root-files.sh
actual:   PASS: no root-owned container-created files in the source tree
          (800 historical uploads/ entries — reported only, unchanged)
verdict:  PASS
```

## CI

CI on this commit must show `test` green with the pgvector service image
+ the bootstrap step, `sast` unchanged, `audit` unchanged (requirements
untouched in P1). Result recorded below once confirmed:

- run on this commit: (polling — recorded in PROGRESS.md on confirmation)
