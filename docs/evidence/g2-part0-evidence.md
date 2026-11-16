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

## H10 — operator CLI `delete-document <id>` (2026-11-16)

- `app/cli.py` gains the command: parse UUID (garbage → ClickException),
  unknown id → "nothing to do" exit 0 (IDEMPOTENT), otherwise the §A9.1
  sequence: TX1 `begin_delete` (row visible 'deleting') → COMMIT →
  idempotent `delete_document_vectors` (outside TX) → TX2 `complete_delete`
  (row + `document_deleted` audit with actor NULL + stored-file removal via
  the F1-hardened `remove_stored_file`). A Pinecone outage leaves the row
  'deleting' for `documents-retry-deleting` — the same recoverable shape as
  the admin route.
- Tests FIRST: `tests/test_cli_delete_document.py` was RED (7 errors —
  command absent) before the implementation. Now 7 green:
  vectors-then-row ordering (spied delete asserts the row EXISTS in status
  'deleting' at vector-delete time), row+file removal (tmp_path
  upload_dir only), idempotency (second run "nothing to do", vectors
  touched once), unknown id noop, non-UUID rejection, audit row (actor
  NULL, no filename in details), stuck-in-'deleting' completion.
- Test-side lessons (reported, no product impact): the caller session's
  identity map raises ObjectDeletedError on expired-instance attribute
  access after another session deletes the row — the assertions use fresh
  SELECTs with pre-captured ids.
- NOT RUN against real data: the mandate reserves the bad%00name.txt
  orphan row for Farhad. The only executions were the tests (tmp_path).
- Full suite: 309 passed / 0 failed (302 + 7).

## H11 — last-admin race extended to the DELETE paths + production-hook grep (2026-11-16)

Mandate: extend the race test to admin delete-user and DELETE /api/me;
grep production code for artificial sleeps / test hooks.

### New tests (tests/security/test_last_admin_race.py, +40)

- `test_racing_admin_user_deletes_never_leave_zero_active` — two barrier-
  synced admins DELETE EACH OTHER via `/api/admin/users/{id}?confirm=true`,
  20 attempts.
- `test_racing_admin_self_deletes_never_leave_zero_active` — two admins
  DELETE `/api/me?confirm=true` (themselves) at the same moment, 20
  attempts.
- Same scaffolding as the H3 PATCH race (widened `active_admin_count`
  window + fresh session per request + barrier); same invariants: at least
  one 409 `last_admin`, exactly one 200, ≥1 active admin remains.

### RED-first proof (the mutant, not luck)

The guard is already locked (H3), so the new tests had to prove they bite:
with the M-C1-style mutant applied (`with_for_update()` removed from
`active_admin_count` in app/services/users.py):

```
command: docker compose --profile test run --rm test python -m pytest \
           "...test_racing_admin_user_deletes..." "...test_racing_admin_self_deletes..." -q
expected: both new tests fail (the unlocked window lets both deletes through)
actual:   40 failed (40/40 attempts red)          PASS/FAIL: FAIL as expected
restore:  git checkout -- app/services/users.py (working tree then clean
          except the test file)
green:    docker compose ... -m pytest tests/security/test_last_admin_race.py -q
          60 passed (20 PATCH + 20 admin-delete + 20 self-delete)
full suite: 349 passed / 0 failed (309 + 40)
```

### Production-code grep (mandated report)

```
grep -rn "sleep(" app/
  → exactly ONE hit: app/rag/vectorstore.py:102 time.sleep(1) — the
    ensure_index readiness poll between describe_index calls, bounded by
    INDEX_READY_TIMEOUT_SECONDS, on the Pinecone-index-provisioning path.
    NOT in any auth/guard path; production-legitimate.
grep -rniE "test_hook|testing|monkeypatch|fake_|_test_|allow_test|skip_auth|backdoor" app/
  → (none) — zero test hooks in production code.
```

## H12 — members surface carries added_by + added_at (2026-11-16)

- FOUND: `GET /api/groups/{id}/members` returned `added_at` but NOT
  `added_by` — the G1.1 schema stores `group_memberships.added_by` and the
  service populates it, but the G1.4 route dropped it. DECISION #4 says an
  admin self-join must be visible to the group's editors — "visible" means
  WHO did it, not just WHEN.
- Test FIRST (red): tests/test_groups_api.py::
  test_group_members_list_carries_added_by_and_added_at — admin creates a
  group, promotes an editor, SELF-JOINS as member; the editor lists members
  and asserts added_by/added_at on both rows (editor: added_by == admin;
  self-join row: added_by == admin == themselves).
- Fix: the route now emits `"added_by": str(m.added_by) | None` — ids only,
  no content (privacy rule intact).
- Green: groups file 12 passed; full suite **350 passed / 0 failed**
  (349 + 1).

## H13 — archived-group semantics pinned in the design doc (2026-11-16)

Docs-only commit. §A14.3 gains the pinned paragraph: archiving
(`groups.is_active = false`) puts the group in NOBODY's audience — its
documents are retrievable by nobody (members, editors, admins — DECISION #4
leaves no implicit path) until re-activation; memberships are KEPT while
archived (re-activation restores the same members); the delete-RESTRICT
still applies while documents reference the group. Notes the fail-closed
implementation (DbAccessResolver: archived excluded from `audiences_for`,
`can_read` False on inactive) and its proving test
(tests/security/test_access_resolver.py, G1.2) — the behavior ALREADY
exists in code; H13 only makes the contract explicit in the design.

## G2 PART A — A1: user-facing documents API (2026-11-16)

- NEW `app/api/documents.py` (5 routes, registered in main.py, route
  inventory consciously extended +5 in the same commit): GET/POST
  /api/documents, GET/PATCH/DELETE /api/documents/{id}.
- Rules proven by `tests/test_documents_api.py` (12 tests, RED first —
  ModuleNotFoundError before the router existed):
  - anonymous 401 on all five; POST private → owned by caller (C2),
    uploaded_by set, F1 server-generated stored name, strict
    `user:<id>` vector token; POST group → editor-only (member 403,
    outsider 404, ADMIN-NON-MEMBER 404 — no implicit access,
    DECISION #4), ownerless row + group_id + uploaded_by; unknown
    group 404; archived group 404.
  - Listing is a SERVER-SIDE filter (org + own private + ACTIVE groups');
    detail outside the audience 404; archived-group docs vanish from
    listing AND detail.
  - PATCH: display_name renames; scope/group keys → 422 (pydantic
    extra=forbid — DECISION #3, no moves); member-non-editor 403;
    admin-member-of-group 403 (C3), admin-non-member 404; org PATCH
    admin-only (plain user 403).
  - DELETE §A9.1 (spy asserts the row exists in 'deleting' at
    vector-delete time); authz matrix: outsider 404, member 403, editor
    200, owner 200, admin ANY (C3) audited.
  - F3/F6 rejections (reserved-char 422 / wrong-type 400 / magic-mismatch
    415 / NUL-text 422) leave NO document rows; audits carry scope+chunk
    counts only — asserted no filename ever reaches audit_events.
- What failed first (2 real catches by the tests):
  1. the first listing filter used Python `and`/`|` on SQLAlchemy
     column expressions — another user's PRIVATE document leaked into
     the listing. Fixed with `sa_or`/`sa_and` (the test exists precisely
     to catch this class of bug).
  2. the route-level NUL-filename probe passed a NUL that multipart
     transport delivers as a literal "%00" — the F3 validator (control
     chars) can only be exercised directly; the route-level probe now
     uses a reserved char (`bad|name.txt`) that survives transport.
- Suite: **362 passed / 0 failed in compose mode AND CI-mode** (350 + 12).

## G2 PART A — A2: race-safe quotas (2026-11-16, commit 458b19f)

- Settings (§A14.6, DECISION #7, all read + wiring-proven):
  USER_UPLOAD_MAX_MB=25, USER_UPLOAD_TOTAL_MB=250, USER_UPLOAD_MAX_DOCS=50,
  USER_UPLOADS_PER_HOUR=10 (limiter built at import from the setting,
  3600 s window — pinned in test_settings_audit.py). Admins exempt from
  all four; global MAX_UPLOAD_MB binds everyone.
- Race safety: enforce_private_quota locks the caller's USER ROW
  (SELECT ... FOR UPDATE) INSIDE the transaction that inserts the doc row —
  parallel uploads serialize; the loser re-reads the winner's COMMITTED
  sizes and answers 409. POST /api/documents streams to a server-generated
  `.incoming/<hex>.part` staging file FIRST (a rejected upload leaves no
  row), then the locked TX checks quota + inserts the row + renames the
  file into place, then parse/index run outside the lock.
- Codes: per-file 413 (USER_UPLOAD_MAX_MB / MAX_UPLOAD_MB for admins),
  409 {"code":"quota_total"}, 409 {"code":"quota_docs"},
  429 {"code":"rate_limited"} + Retry-After.
- RED-first: quota tests errored on the absent settings/functions; then
  two real catches — (1) stale `dest` NameError after the staging
  restructure (caught by every upload test), (2) the race test initially
  exercised REAL add_documents (offline fakes made explicit).
- MUTATION PROOF of the race test: with `with_for_update()` stripped from
  enforce_private_quota the race test FAILS (oversubscription); green with
  the lock. NOTE (disclosed): the first mutant run used
  `git checkout -- app/services/documents.py` to restore, which also wiped
  the then-UNCOMMITTED A2 service changes — restored by re-applying the
  identical edits, suite re-verified green before committing. Process
  lesson recorded: commit BEFORE mutation runs (the g1 harness does this;
  the ad-hoc run here did not).
- Suite: 369 passed / 0 failed.
