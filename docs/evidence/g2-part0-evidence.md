# G2 PART 0 evidence (docs/tasks/g2.md, one section per step)

## H7 — Settings audit (2026-11-16)

Mandate: every field of `Settings` must be read by production code AND
proven by a test of its effect. `ENABLE_WEB_FALLBACK` was decorative until
H4 found it — this step makes that class of drift impossible.

### Test added (first, red before the fix)

`tests/test_settings_audit.py`:

- `test_every_setting_is_referenced_by_production_code` — walks every
  `Settings` field via `model_fields` and greps `app/**/*.py` for the field
  name (AST-exempting only the `Settings` class body in `config.py`, so the
  definition itself proves nothing; the `validate_*` functions in the same
  file count as real reads). `ALLOWLIST` exists for wiring-only fields WITH
  a written reason — it is EMPTY as of H7.
- Effect tests for the three fields that had NO effect test anywhere:
  `app_name` (health payload + FastAPI title),
  `chat_rate_limit_per_minute` (the legacy chat limiter is built FROM the
  setting — a hardcoded limit fails the equality),
  `tavily_api_key` (empty key refuses tool construction with RuntimeError;
  a set key constructs and caches the tool).
- Every other field already had an effect test (rate limiters, session
  lifetimes, upload caps, B3 chat caps, cookie flags, D6 flag, vectorstore
  index/dimension mapping, timeouts, static keys) — listed, not re-proven.

### RED state (before the fix)

```
command: docker compose --profile test run --rm test python -m pytest tests/test_settings_audit.py -q
expected: the reference test names the decorative fields
actual:   FAILED — "Settings fields not read anywhere under app/ — remove
          them or allowlist them WITH a reason: ['sample_kb_dir',
          'test_database_url', 'conversation_retention_days',
          'pinecone_test_namespace']" (1 failed, 4 passed)
          PASS/FAIL: FAIL (expected red)
```

### The four decorative settings — REMOVED, not allowlisted

| Removed field | Why it was decorative | What replaced it |
|---|---|---|
| `sample_kb_dir` | read ONLY by the root-level legacy script `ingest_sample_kb.py`, which cannot even run anymore (its `add_documents(chunks)` call predates the B4 keyword-only signature — it would crash today) | nothing — the dead script is deleted with the field |
| `pinecone_test_namespace` | referenced only by a decorative assertion in `tests/test_settings.py`; tests and verify scripts hardcode `multi-user-test` (mandated by CLAUDE.md) | nothing — the constant lives in the scripts/tests that use it |
| `conversation_retention_days` | D5 deferred the retention sweep; ZERO readers anywhere (app, tests, scripts). README already said "retention sweep deferred" | nothing — the field returns WITH the sweep if a policy ever lands |
| `test_database_url` | `tests/conftest.py` reads the `TEST_DATABASE_URL` env var directly (fail-fast when unset); the Settings field added a second, unused copy | the env var, as before |

### Declared contract changes (no test weakened)

- `tests/test_settings.py`: the three assertions pinning removed fields are
  GONE with their subjects (same rule B5 used: the test dies with the
  subject). The `enable_web_fallback`/`max_upload_mb`/cookie pins stay.
- `.env.example`: `CONVERSATION_RETENTION_DAYS` and `PINECONE_TEST_NAMESPACE`
  lines removed. `TEST_DATABASE_URL` stays (compose sets it; conftest reads it).
- README settings table: the two matching rows removed.

### GREEN state

```
command: docker compose --profile test run --rm test python -m pytest tests -q
expected: 296 prior + 5 new = 301 passed
actual:   301 passed, 3 warnings in 107.22s        PASS
command: docker compose --profile test run --rm -e ENABLE_WEB_FALLBACK=false test python -m pytest tests -q   (CI-mode, the H4 lesson)
actual:   301 passed, 3 warnings in 108.03s        PASS
```

## H8 — Hermetic tests (2026-11-16)

Mandate: no test may depend on the developer's `.env` or the compose
environment; conftest pins every setting a test relies on; prove it with an
empty environment AND with `.env` present.

### Pins added (tests/conftest.py, before any app import)

Env vars beat pydantic's env_file, so `os.environ` pins make the suite
immune to the developer's `.env` (the compose container bind-mounts it):
`OPENAI/TAVILY/PINECONE_API_KEY=""` (pre-existing), `COOKIE_SECURE=false`
(pre-existing), and NEW: `APP_ENV=development` (production validators must
never fire from a dev .env), `RUN_MIGRATIONS_ON_STARTUP=false` (importing
app.main must never migrate ANY db), `ENABLE_WEB_FALLBACK=false` (amended
D6 default; web-path tests opt IN explicitly), `ADMIN_API_KEY=change-me`,
`CHAT_API_KEY=""`, `LOGIN_THROTTLE_PEPPER=""`,
`DATABASE_URL=postgresql+psycopg://tests@hermetic.invalid/test` (any
accidental production-DB use fails loudly instead of silently hitting the
dev DB), `PINECONE_NAMESPACE=multi-user-test` (belt over the CLAUDE.md
namespace rule), and the four caps the chat/upload tests lean on
implicitly: `MAX_UPLOAD_MB=25`, `MAX_MESSAGE_LENGTH=3000`,
`MAX_MESSAGES_PER_CONVERSATION=200`, `MAX_CONVERSATIONS_PER_USER=100`.
`TEST_DATABASE_URL`/`ALEMBIC_DATABASE_URL` stay unpinned: they are the
runner's provision and fail fast when missing (pg_engine).

### What failed first (the poisoned run found 20 real .env dependencies)

```
command: docker compose --profile test run --rm -e APP_ENV=production -e ENABLE_WEB_FALLBACK=true \
           -e RUN_MIGRATIONS_ON_STARTUP=true -e ADMIN_API_KEY=poison-real-key -e CHAT_API_KEY=poison-chat \
           -e LOGIN_THROTTLE_PEPPER=poison-pepper -e MAX_UPLOAD_MB=1 -e MAX_MESSAGE_LENGTH=7 \
           -e MAX_CONVERSATIONS_PER_USER=1 -e DATABASE_URL=postgresql+psycopg://poison@nowhere/x \
           -e PINECONE_NAMESPACE=poison-ns test python -m pytest tests -q
expected: 302 passed (the pins neutralize every poisoned value)
actual 1st run: 20 failed / 282 passed — MAX_MESSAGE_LENGTH=7 broke every
          chat-path test (14), MAX_UPLOAD_MB=1 broke upload tests,
          MAX_CONVERSATIONS_PER_USER=1 broke pagination tests,
          test_feature_flags_and_limits_defaults saw the poisoned env.
          PASS/FAIL: FAIL (expected first — these ARE the implicit .env
          dependencies H8 exists to expose)
fix: pin the four caps to their code defaults in conftest (no test touched)
actual final: 302 passed, 3 warnings in 91.31s     PASS
```

### The three proof runs (final state)

```
1. POISONED env (the full hostile-.env simulation above):
   302 passed, 3 warnings in 91.31s                  PASS
2. EMPTY environment (only PATH + TEST_DATABASE_URL survive):
   docker compose --profile test run --rm test sh -c \
     'env -i PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/bin \
      TEST_DATABASE_URL="$TEST_DATABASE_URL" python -m pytest tests -q'
   302 passed, 3 warnings in 81.73s                  PASS
3. NORMAL compose run — the developer's .env IS present (bind-mounted) and
   pydantic reads it; the pins win:
   302 passed, 3 warnings in 85.78s                  PASS
```

### Permanent enforcement

`tests/test_settings_audit.py::test_suite_runs_on_the_hermetic_pins` asserts
the pinned values through `get_settings()` — removing/renaming a conftest
pin fails it, so the suite cannot silently start reading `.env` again.

## H9 — verify-script env pins + rag-v2 before/after (2026-11-16)

Mandate: pin `ENABLE_WEB_FALLBACK=false` and `PINECONE_NAMESPACE=multi-user-test`
in EVERY `scripts/verify/*.sh` container (b2..g1); scripts must COMPARE rag-v2
before/after (unchanged) rather than assume zero vectors.

### Changes (all six scripts)

- Pins added to every SERVING app container:
  - b2.sh: 3 containers (main :8082, spray :8083, __Host- variant :8084) —
    previously NONE pinned (the legacy chat queried rag-v2 by default!).
  - b3.sh: 4 containers (main + two restarts + caps :8085).
  - b4.sh/b5.sh/b6.sh: had `PINECONE_NAMESPACE=$NS`, gained
    `ENABLE_WEB_FALLBACK=false`.
  - g1.sh: already pinned both (the H4/G1 standard) — unchanged.
  - Deliberate exception: b2's `rag-prod-refusal` one-shot container is NOT
    pinned — its whole purpose is to prove the APP production validator
    refuses dev defaults; pinning it would defeat the check.
- rag-v2 before/after: every script now captures the SORTED EXACT VECTOR-ID
  LIST of rag-v2 right after the app is healthy (`ragv2_ids()`, index.list —
  never stats) and asserts BEFORE == AFTER near the RESULT line. The list
  prints `(empty)` when the namespace has no vectors so the ck empty-guard
  cannot false-PASS. Currently the baseline is the 5 sample-KB vector ids
  from H6 — the scripts no longer assume anything about the count.
- b2.sh fix found by the first live run: the legacy-chat guard read
  `OPENAI_API_KEY` from the HOST shell env (a stale assumption — keys live
  in .env), so the check skipped even though .env had both keys. Now guards
  on `.env` exactly like g1.sh: `grep -q '^OPENAI_API_KEY=..*' .env`.

### Executed evidence

```
bash -n scripts/verify/*.sh            → all six OK
bash scripts/verify/b5.sh              → PASS=13 FAIL=0 SKIP=0 (07:47:41Z)
                                         1 real LLM call; trap: "deleted 1
                                         vectors by id"; rag-v2 5 ids == 5 ids
bash scripts/verify/b2.sh (run 1)      → 72/0/1 — the skip EXPOSED the stale
                                         host-env OPENAI guard above; fixed
bash scripts/verify/b2.sh (run 2)      → PASS=73 FAIL=0 SKIP=0 (07:52:16Z)
                                         1 real LLM call; rag-v2 5 ids == 5 ids
b3/b4/b6/g1                            → edited identically (pins + ragv2_ids),
                                         bash -n clean; NOT re-run live this
                                         step (LLM budget) — their next
                                         scheduled runs exercise the pins.
```

Real-LLM spend this step: TWO calls (one b5 chat, one b2 chat), disclosed.

### Side observation (REPORT only)

`data/audit.db` reads NOFILE in this b5 run: the H5 report left its deletion
to Farhad; the file is gone from `data/` as of 2026-11-16 (his action — this
session never touches it). Nothing in the app can recreate it (CHECK 1+2
prove that again in the same run).
