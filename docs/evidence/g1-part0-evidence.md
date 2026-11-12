# G1 PART 0 housekeeping — executed evidence

One section per step (H1–H6), command + expected + actual + PASS/FAIL,
appended by the step's own commit per CLAUDE.md.

## H1 — b5.sh namespace violation fixed (2026-11-12)

**Violation being fixed:** the committed b5.sh ran its throwaway container
with the DEFAULT namespace, so its legacy-ingest check wrote a real vector
into production-namespace `rag-v2` (documented in
docs/evidence/b5-evidence.md; the accumulated junk was purged by id the
same day). This step makes the script CLAUDE.md-compliant:
`PINECONE_NAMESPACE=multi-user-test` on the container + a cleanup trap that
deletes the script's vectors **by id** (this run's `document_id` from the
ingest response plus any prior crashed run's `b5-note.txt` leftovers,
selected via list → fetch → metadata match, never `delete_all`).

**Command:** `bash scripts/verify/b5.sh` (2026-11-12 07:31:14Z → 07:31:51Z)

| Check | Expected | Actual | Result |
|---|---|---|---|
| full script run | 12 PASS / 0 FAIL | 12 PASS / 0 FAIL / 0 SKIP | PASS |
| ingest returns document_id (trap input) | 36-char uuid | uuid | PASS |
| trap by-id cleanup | vectors deleted | `cleanup: deleted 1 vectors by id` | PASS |
| rag-v2 ids before run (external read-only probe) | 0 | 0 | PASS |
| rag-v2 ids after run | 0 (untouched) | 0 | PASS |
| multi-user-test ids after run | 0 (trap cleaned) | 0 | PASS |
| legacy `enterprise-agentic-rag` vector count | 27 (untouched) | 27 | PASS |
| leftover rag-verify-* containers | none | none | PASS |

The B5 subject checks all still hold on the re-run (writer unimportable,
AUDIT_DB_PATH setting gone, audit.db 4 rows before == after with mtime
untouched, chat marker absent from trace + logs, legacy ingest 200).
ONE real LLM call spent (the legacy chat); one embedding call for the
ingest (disclosed).

## H2 — B6 live gaps closed: private-document purge + self-delete (2026-11-12)

**Gap being closed:** b6.sh previously exercised admin purge and
`DELETE /api/me` only against users owning ZERO private documents, and
proved vector deletion with namespace STATS counters. Now: a REAL private
document per scenario via the `ingest-private` CLI (test namespace), and
deletion proved by EXACT vector ids (list → fetch → metadata.doc_id match
→ capture the concrete ids → after deletion `fetch(ids=...)` must return
none of them). Never stats, per CLAUDE.md. The b6 cleanup trap was also
upgraded from `delete_all` to by-id deletion of the script's tracked
document ids (CLAUDE.md: "clean up their own vectors by id in a trap").

**Command:** `bash scripts/verify/b6.sh` (final green run 2026-11-12 07:41:18Z)

| Proof | Expected | Actual | Result |
|---|---|---|---|
| full script run | 0 FAIL | 44 PASS / 0 FAIL / 0 SKIP | PASS |
| ingest-private CLI (purge target) | private doc indexed | `private document <uuid>` | PASS |
| purge-target doc has exact vector ids | ≥1 | ≥1 | PASS |
| admin bulk-purge → 200 | 200 | 200 | PASS |
| purged doc's exact ids absent after purge | 0 present | 0 | PASS |
| purged doc row gone | 0 rows | 0 | PASS |
| self-delete user owns a real private doc | indexed | `private document <uuid>` | PASS |
| `DELETE /api/me` no confirm → 422 / confirm → 200 | 422 / 200 | 422 / 200 | PASS |
| self-deleted user's exact ids absent | 0 present | 0 | PASS |
| self-deleted user's doc + user rows gone | 0 / 0 | 0 / 0 | PASS |
| CHECK 5 org delete-ANY now proved by exact ids | 0 present | 0 | PASS |
| legacy namespace untouched (read-only stats) | 27 = 27 | 27 = 27 | PASS |
| trap cleanup (by id) | namespace empty | `cleanup: namespace already empty` | PASS |
| rag-v2 ids after run | 0 (untouched) | 0 | PASS |

**What failed first:** the first run failed 8 checks with ONE root cause —
`MSYS_NO_PATHCONV=1 docker cp "$TMP/..."` also disabled path conversion for
the HOST argument, so docker.exe looked for `C:\tmp\...` and found nothing;
the CLI error text then flowed into the doc-id extraction and the SQL row
checks (the script's EMPTY/`>=1` guards failed loudly instead of false-
PASSING, as designed). Fix: host side passed as a native Windows path
(`cygpath -w`), conversion disabled only for the container side. No app
code involved. Real model usage: 3 small embedding calls (org + 2 private
notes), ZERO LLM calls.

## H3 — last-admin guard race: test first, then row locking (2026-11-12)

**Bug being closed:** `active_admin_count` was an unlocked aggregate
COUNT. Two admins demoting/disabling each other at the same moment both
observed count=2 inside the guard window (count → mutate → commit), both
proceeded, and the result was ZERO active admins — the exact state the
§A5 guard exists to prevent.

**What failed first (the test, before any fix):**
`tests/security/test_last_admin_race.py` — two barrier-synchronized
requests per attempt, each with its OWN DB session/transaction (the test
overrides get_db with a fresh-per-request session on the test engine; the
guard window is widened by a semantics-free sleep inside
active_admin_count so the race is deterministic, not luck-based),
alternating demote/disable, 20 attempts:

```
20 failed, 2 warnings in 15.92s
AssertionError: attempt 0: neither racing request was refused —
codes=[200, 200] bodies=('...role":"user"...race0b...', '...role":"user"...race0a...')
```

Both racing requests answered 200 — both admins demoted, zero active
admins left. 20/20 attempts red.

**The fix** (app/services/users.py): `active_admin_count` now selects the
active-admin ids `FOR UPDATE` and counts in Python (FOR UPDATE cannot
ride on an aggregate). The second racing guard blocks until the first
transaction commits, re-reads the post-update state, and answers 409.
Deadlock-free: both transactions lock the admin set in one statement
(consistent scan order).

| Check | Expected | Actual | Result |
|---|---|---|---|
| race test, UNFIXED guard | fails every attempt | 20 failed / 0 passed | RED (as designed) |
| race test, FOR UPDATE fix | 20/20 green | 20 passed | PASS |
| full offline suite | 0 failed | **253 passed / 0 failed** (67.5s) | PASS |

**How to reproduce the red state:** revert app/services/users.py to the
COUNT aggregate and run
`docker compose --profile test run --rm test python -m pytest tests/security/test_last_admin_race.py -q`.

## H4 — ENABLE_WEB_FALLBACK defaults to false, AND the switch now works (2026-11-12)

**What H4 found:** two problems, not one. (1) The CODE default was still
`true` with a test pinning it — the D6 AMENDMENT (§A14, decided
2026-11-09, and CLAUDE.md) says default `false`; the flip never landed.
(2) Worse, the kill switch was DECORATIVE: nothing read the setting —
`after_kb` routed KB-weak → search_web unconditionally, so even
`ENABLE_WEB_FALLBACK=false` let question text reach Tavily. A default
flip alone would have pinned a fake guarantee.

**Changes:** config default → False (comment cites the amended D6);
`after_kb` now honors the flag — KB-weak with fallback off routes
straight to `insufficient` (no rewrite loop; the conditional edge map
gains the existing "insufficient" target — topology otherwise unchanged);
README env-table row corrected; `.env` deliberately UNTOUCHED (it sets
`ENABLE_WEB_FALLBACK=true` — an explicit operator opt-in, which remains
valid under the new default).

**Explicit CONTRACT CHANGES to existing tests (not weakenings):**
- tests/test_settings.py `test_feature_flags_and_limits_defaults`: pinned
  True → now pins False (the amended decision; old comment said "kill
  switch defaults on").
- tests/test_graph_routing.py: the three web-path tests (weak-KB→web,
  retry-exhaustion, bounded-rewrite) now set `enable_web_fallback=True`
  explicitly — the same behavioral assertions, under the now-opt-in flag.

**New test:** `test_default_off_weak_kb_stops_at_insufficient_and_tavily_
is_never_called` — with fallback OFF, an EXPLODING Tavily fake (raises if
so much as invoked) proves the question text never reaches Tavily;
source_used=insufficient_evidence, retry_count=0, query never rewritten.

| Check | Expected | Actual | Result |
|---|---|---|---|
| settings + routing + workflow tests | 0 fail | 16 passed | PASS |
| full offline suite | 0 fail | **254 passed / 0 failed** (63.9s) | PASS |

## H5 — data/audit.db report for Farhad (2026-11-12) — REPORT ONLY, never deleted here

| Field | Value |
|---|---|
| Absolute path | `C:\Users\mtanveer\Desktop\Work\PKM RAG\Enterprise-Agentic-RAG\data\audit.db` |
| Size | 12,288 bytes |
| Git status | ignored (never tracked; commit 4302258 "Document setup and ignore runtime artifacts") |
| Rows in `query_audit` | **4** |
| Latest row | `created_at = 2026-11-09T08:42:11.331175+00:00` |

Provenance: the file is a relic of the legacy audit writer (removed in B5,
commit 797c494). Its 4 rows were written by verify-run app containers
during the 2026-11-09 b-runs, before the writer was removed. Since B5
nothing writes it — re-proven live on 2026-11-12 (b5.sh: 4 rows before ==
after, mtime untouched, across chat + ingest + a fresh startup).

**Farhad: the file is yours to delete when you choose.** Nothing in the
app, tests, or scripts reads or writes it anymore (the B5 pins prove it).

## H6 — sample KB re-ingested as ORG documents into rag-v2 (2026-11-12)

**Operator action** (allowed to write rag-v2; not a test/script write):
`docker compose exec app python -m app.cli ingest-org /app/data/sample_kb`

```
indexed company_it_handbook.md → org document 01a1cecd-6389-7aad-a0c2-60bae13f1358
indexed service_desk_runbook.md → org document 01a1cecd-7f02-769f-bc46-731e3bdf1f80
```

Pre-state (list-based, no stats): rag-v2 = 0 vector ids, legacy
`enterprise-agentic-rag` = 27 vector ids.

**Documents listing** (documents table, dev `rag` DB):

| display_name | id | scope | owner_id | chunks | status |
|---|---|---|---|---|---|
| company_it_handbook.md | 01a1cecd-6389-7aad-a0c2-60bae13f1358 | org | NULL | 3 | indexed |
| service_desk_runbook.md | 01a1cecd-7f02-769f-bc46-731e3bdf1f80 | org | NULL | 2 | indexed |

**Per-document EXACT vector ids** (list → fetch → metadata.doc_id match;
no stats counters):

- `company_it_handbook.md` — 3 vectors (== chunk_count):
  `5defeddf2fd4d5766a00698b16fc89543ee04890dab452ae6f43e7909886c8df`,
  `97d3f1e3ee0e011eeac8984e941f4d9da5440715c919d25202c84a1dc0203a00`,
  `b172d4ee048449912355dc043a39fb03512ff8fc9e97dc9fb49512fe25e94172`
- `service_desk_runbook.md` — 2 vectors (== chunk_count):
  `17e7922a3f2fa09c01510503eabb9b8a6c5e0df2cd92e22b04b7225aadcf2f42`,
  `a62345a197d765d7a36116b4e42041fd6c8619e723227766d568a27f4344b47d`

Every vector carries `scope=org`, `read_access=["org"]`, correct source;
**zero** vectors in rag-v2 belong to any other doc id. Legacy namespace
untouched: 27 ids after (list-based count).

**Retrieval smoke** (ONE real embedding call, no LLM): org-filtered
retriever, question "How do I connect to the company VPN?" → 4 hits, top
3 from company_it_handbook.md, 1 from service_desk_runbook.md — the app
can answer KB questions again. Embeddings spent: ~6 (5 ingest + 1 query);
LLM calls: 0.

**Orphan row reported (not mutated):** the dev DB documents table still
holds `bad%00name.txt` (id 01a1ac27-7635-71d3-ae3f-3c0ba280ab00, scope
org, status=indexed, chunks=1) from F3-era live testing. Its vector was
removed from rag-v2 during the 2026-11-12 junk cleanup, so the row is an
orphan (indexed with 0 vectors). Left for the operator: delete it via
`DELETE /api/admin/documents/{id}` (idempotent vectors-first) if unwanted.

## H4 follow-up — CI caught one more implicitly env-dependent test (2026-11-12)

**What failed first:** CI (`test` job) went red at `1d458fe` (H4) while
the compose run was green. Root cause: the compose test container reads
the bind-mounted `.env` (which sets `ENABLE_WEB_FALLBACK=true`), but CI
runs without `.env` — so the amended default (false) applied there and
`tests/test_prompt_injection.py::test_web_content_is_fenced_in_grade_
and_generate_prompts` (KB-weak → web path, no explicit flag) short-
circuited to insufficient. Reproduced locally with
`docker compose --profile test run --rm -e ENABLE_WEB_FALLBACK=false test …`
→ 1 failed / 230 passed (-x stop).

**Fix:** that M5 test now opts in explicitly
(`monkeypatch.setattr(s, "enable_web_fallback", True)` — same declared
contract change as the routing tests; the fencing assertions themselves
are unchanged). Process lesson recorded: after env-default changes, run
the FULL suite in CI-mode, not only the touched files.

| Check | Expected | Actual | Result |
|---|---|---|---|
| full suite, CI-mode (`ENABLE_WEB_FALLBACK=false`) | 0 fail | **254 passed / 0 failed** (66.0s) | PASS |
| full suite, compose-mode (`.env` flag true) | 0 fail | **254 passed / 0 failed** (67.5s) | PASS |
| CI on the fix commit | test green | (re-checked after push) | — |
