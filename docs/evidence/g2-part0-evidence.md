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
