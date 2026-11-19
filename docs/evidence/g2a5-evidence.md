# G2 PART A5 evidence (docs/tasks/g2.md, "PART A5 runs BEFORE Part B")

Executed evidence per CLAUDE.md: command, expected, actual, PASS/FAIL, per
step. One commit per step; tests written first.

## A5.1 — Upload connection discipline (2026-11-19)

Mandate: the upload path must not hold a DB connection or transaction while
parsing or embedding. TX1 = quota check + insert the document row (status
pending) + commit; ingest with no DB held; TX2 = mark indexed or failed.
Prove with a pool test (``checkedout()==0`` during a slow fake ingest).

**What the audit found:** the USER route (POST /api/documents, A2/A3 code)
already complied — TX1 commits (the locked quota TX) before the
``ingest_gate`` block, and ``expire_on_commit=False`` keeps attribute reads
from re-checking out a connection. The ADMIN org route (POST
/api/admin/documents) did NOT: it inserted the row, streamed, ``flush()``ed
and held that open transaction across ``load_file``/``add_documents``.

**Tests first** (tests/test_upload_pool_discipline.py, same pool-probe
idiom as the chat TX-discipline test — production-style session lifecycle,
no truncate fixture, committed rows cleaned up at the end):

```
command:  docker compose --profile test run --rm test \
            python -m pytest tests/test_upload_pool_discipline.py -q
RED run (before any product change):
  expected: private-route test green (code already complies);
            admin-org test RED (connection held across ingest)
  actual:   1 failed, 1 passed —
            FAILED test_admin_org_upload_releases_the_pool_during_ingest
            AssertionError: "the admin org upload holds a DB connection
            across parse/embed — assert 1 == 0"
  verdict:  PASS (RED as mandated — the probe caught the real violation)
GREEN run (after the admin route restructure):
  expected: 2 passed
  actual:   22 passed (new module + tests/test_admin_api.py +
            tests/test_admin_documents_listing.py — the B6-era admin
            upload contract is unchanged)
  verdict:  PASS
```

**The product change** (app/api/admin.py, ``upload_org_document``): stream
to a server-generated staging file in ``uploads/.incoming`` first (no row
exists yet, so the 413/422 rejections need no rollback), then TX1 = insert
the pending row + rename staging → ``uploads/<doc_id>/<doc_id><suffix>`` +
``commit()`` (releases the connection), then ``ingest_gate`` parse/embed
with no DB held, then TX2 = attach_ingest_result / mark_failed + audit +
commit. Response contract unchanged (201 + document_id + chunks) — all
existing admin tests pass unmodified. Rejection-path semantics unchanged
(no row, no file) — already pinned by the A1/A4 tests.

**Full suite after the change:**

```
command:  docker compose --profile test run --rm test python -m pytest tests -q
            (compose mode)   → 397 passed / 0 failed  (was 395 + 2 new tests)
command:  docker compose --profile test run --rm -e ENABLE_WEB_FALLBACK=false \
            test python -m pytest tests -q   (CI-mode flag forced)
            → 397 passed / 0 failed
verdict:  PASS
```

**Disclosed scope boundary (not changed, deliberately):** two other code
paths still hold a transaction across parse/embed — the LEGACY
``POST /api/ingest`` (admin-key only, scheduled for deletion in the B9
removal commit; restructuring it would be work on its deathbed) and the
operator CLI ``_ingest_one`` (single process, one session per CLI run — not
a pool-sharing surface). The A5.1 mandate targets the upload ROUTES over
HTTP; both deviations are recorded here and re-visited only if Farhad
wants them closed before B9.

## A5.2 — Quota accounting (2026-11-19)

Mandate: failed and deleted documents must not keep consuming quota; stale
pending rows are swept.

**Tests first** (tests/test_quota_accounting.py):

```
command:  docker compose --profile test run --rm test \
            python -m pytest tests/test_quota_accounting.py -q
RED run (before any product change):
  expected: the accounting violations RED; already-correct behavior green
  actual:   7 failed, 2 passed —
            FAILED failed_documents_do_not_consume_total_quota
                   (409 quota_total — the 0.2MB failed row consumed quota)
            FAILED failed_documents_do_not_consume_doc_count
            FAILED near_quota_view_ignores_failed_rows
                   (failed bytes listed the user as near-quota)
            FAILED sweep_marks_only_stale_pending_failed        (no sweep)
            FAILED sweep_frees_quota_and_is_audited             (no sweep)
            FAILED sweep_threshold_is_a_real_setting            (no setting)
            FAILED uploads_sweep_stale_cli                      (no CLI cmd)
            passed: deleted_documents_free_the_total_quota (row gone = bytes
                    freed — pinned as-is; count axis was already pinned in
                    test_upload_quotas.py::deleting_documents_frees...)
                    inflight_pending_rows_still_count... (pending MUST keep
                    counting — the A2 race proof depends on it)
  verdict:  PASS (RED as mandated)
GREEN run (after the changes below): 35 passed — the new module + the
  existing quota/race/CLI/settings-audit/admin-listing suites.
```

**What failed first during the GREEN run:** my own test arithmetic — the
first scenario had the failing upload itself breach the quota, so the gate
(rightly) 422'd it with quota_total BEFORE parsing; no failed row could
exist. Fixed the scenario (0.2MB failing upload fits; 0.35MB third upload
distinguishes the accountings). No product change for this.

**Product changes:**
- ``app/services/documents.py``: ``QUOTA_COUNTED_STATUSES = ("pending",
  "indexed")`` — ``enforce_private_quota`` sums only these (failed rows
  stop consuming quota; deleted rows never had a row). The admin
  ``near_quota_users`` enforcement view (app/api/admin.py) applies the SAME
  filter so the enforcement view cannot disagree with enforcement.
- ``user_upload_pending_stale_minutes: int = 30`` (config) — read by the
  new ``sweep_stale_pending`` service (documents.py): pending rows older
  than the threshold are marked 'failed' (error class ``stale_pending``),
  audited as ``stale_pending_swept`` (ids/counters only, CLI actor None).
  In-flight uploads are minutes old — never swept.
- ``app/cli.py``: ``uploads-sweep-stale`` operator command (mirrors
  ``documents-retry-deleting``).

**Full suite after the change:** 406 passed / 0 failed in compose AND
CI-mode (was 397 + 9 new tests). The settings-audit reference test passes
(the new setting is read in app/services/documents.py) and its effect is
proven by ``test_sweep_threshold_is_a_real_setting`` (threshold 0 sweeps
even a fresh row).

```
verdict: PASS
```

## A5.3 — Mutation harness working-tree safety (2026-11-19)

Mandate: never discard uncommitted work — refuse to run on a dirty working
tree; back up each target file to a temp directory and restore from that
copy, never from git; assert a byte-identical restore.

**What the audit found:** run-g2a-mutations.sh refused only tracked
changes (`git diff` + `git diff --cached` — untracked files slipped
through) and restored every mutant with `git checkout --` (a discarding
restore); run-g1-mutations.sh had NO dirty-tree check at all and the same
git-restore.

**Tests first** (scripts/mutation/selftest.sh — a 25-check offline
self-test against a throwaway sandbox git repo; sourcing-safety is checked
BEFORE any source, so the RED run cannot start a real mutant run):

```
command:  bash scripts/mutation/selftest.sh
RED run (before any harness change):
  expected: the safety mechanics RED
  actual:   PASS=10 FAIL=15 — no shared lib, harnesses not source-safe
            (sourcing the old g2a harness STARTED A REAL MUTANT RUN —
            harmlessly: the sandbox has no docker compose file and no
            target files, and this demonstrated exactly why the guard is
            mandated), g1 refused nothing, untracked files refused by
            nobody, restores came from git, no byte-identical assertion
  verdict:  PASS (RED as mandated)
GREEN run (after the changes below):
  actual:   PASS=25 FAIL=0, exit 0 —
            S1 bash -n on all four scripts; S2 lib exists + both harnesses
            source it; S3 BASH_SOURCE main guard (sourcing never runs
            mutants); S10 zero git checkout/restore/stash in any harness;
            S4/S5/S6 modified, staged AND untracked trees refused by BOTH
            harnesses (target left byte-untouched); S7 backup→restore
            round-trip returns the PRISTINE COPY's bytes while git HEAD
            deliberately holds different bytes (restore-from-git would be
            detectably wrong); S8 missing backup → loud RESTORE-FAILED,
            nonzero exit, file left for manual recovery (no silent git
            fallback); S9 an interrupted run (exit 3 mid-mutant) is
            repaired by the EXIT trap and the temp dir is removed
  verdict:  PASS
```

**What failed first during the self-test GREEN run:** two SELFTEST bugs
(not harness bugs) — the sandbox initially copied only the g2a harness
(the g1 refusal checks tested a nonexistent file) and the function tests
ran in subshells that never inherited the sourced functions. Fixed in the
self-test; the harness changes below were untouched by both failures.

**Changes:**
- NEW ``scripts/mutation/_lib.sh``: ``mutation_preflight`` (refuse on ANY
  ``git status --porcelain`` output — modified, staged, untracked; create
  the per-run ``mktemp -d`` backup dir; install the EXIT trap),
  ``mutation_backup`` (pristine copy, kept once per file),
  ``mutation_restore`` (copy back + ``cmp`` byte-identical assert; loud
  exit on any failure — never a git fallback), ``mutation_restore_all``
  (crash-safe trap; removes the run's own mktemp dir — the one recursive
  delete allowed here), and the shared ``patch_file``.
- run-g2a-mutations.sh / run-g1-mutations.sh: source the lib; preflight
  replaces the old diff-based check (g1 gains a check it never had);
  ``mutate()`` backs up before patching and restores from the copy with
  the cmp assertion; the main run is guarded by ``BASH_SOURCE`` so
  sourcing can never start mutants. Mutant list and selections UNCHANGED.
- NEW ``scripts/mutation/selftest.sh`` (the tests above; offline, no
  docker, no pytest).

**Live integration proof** (run AFTER this commit so the preflight sees a
clean tree): full g2a harness against the real repo — see the A5.3
addendum section below for the recorded run.

## A5.3 addendum — the live integration runs (2026-11-19, offline, no LLM)

```
command:  bash scripts/mutation/run-g2a-mutations.sh      (clean tree; the
          new preflight itself was exercised and passed)
RUN 1:    12/13 KILLED, M-P1 PATCH-FAILED — a REAL anchor drift the live
          run caught: the pre-A3 anchor proc.join(timeout=timeout_s) died
          with the pipe-deadlock fix (da0731b). P1 retargeted to the
          current deadline (timeout_s * 1000; finite, so the hang child's
          60s self-exit still terminates the selection). Commit 8d1996b.
RUN 2:    13/13 KILLED —
            Q1..Q4 KILLED, C1/C2 KILLED, G1/G2 KILLED, P1 KILLED (104.89s:
            the hang child self-exits at 60s, then the deadline assertions
            fail — the designed termination shape), P2..P5 KILLED.
          Baselines green (6/15/19 passed). ZERO RESTORE-FAILED lines —
          every backup→restore was byte-identical through 14+ real
          patch/run cycles on the real tree.
verdict:  PASS
```

Note: the end-of-run `git status` in RUN 2 listed the A5.4 verify-script
edits being made concurrently — the harness only ever backs up / restores
its TARGET files (app/*.py), so concurrent edits to scripts/verify/ cannot
interfere with a run (and this run proves it).

## A5.4 — Verify scripts: per-run namespace; rejection-before-embedding;
## the 55,598-vector investigation (2026-11-19)

### 1. The investigation — which script produced the leftovers, and why

```
command:  docker run (read-only mount) python -c "...describe_index_stats()"
expected: multi-user-test empty (the 2026-11-16 evidence says 55,598 were
          "deleted BY ID in batches of 100")
actual:   {'enterprise-agentic-rag': 27, 'multi-user-test': 55597,
          'rag-v2': 5}   — the cleanup NEVER completed
verdict:  FAIL (the documented cleanup did not stick)
```

**Attribution (converging evidence):** the producer was g2a.sh itself —
docs/evidence/g2a-evidence.md already attributes the accumulation to "the
one run killed by a harness timeout — its trap never fired"; the bash EXIT
trap does not survive a SIGKILLed/timeout-killed process, and g2a.sh ran
18 times that day. Each killed run left its vectors in the SHARED
multi-user-test namespace. My purge census found **61 distinct doc_ids**
(all with test-artifact metadata; zero refused by the doc_id guard) —
several runs' worth, consistent with multiple killed/incomplete runs.
~10-11k vectors per full run × several killed runs ≈ 55k.

**Why the cleanup could not recover, mechanically — reproduced live:**

```
command:  idx.fetch(ids=<500 ids>, namespace='multi-user-test')
actual:   PineconeApiException: (414) Request-URI Too Large
verdict:  Pinecone fetch is GET-based; a 500-id URI already 414s. The
          2026-11-16 "batches of 100" cleanup either was interrupted or
          silently failed part-way; list-based probes against 55k vectors
          then 414'd too (recorded in g2a-evidence.md), making every later
          cleanup attempt harder. CHUNK=100 works — the purge below used it.
```

### 2. The purge (hygiene; by id, guarded)

scripts/verify/purge-multi-user-test.sh (committed with this step):
namespace allowlist (`multi-user-test` only), CHUNK=100 (414-proof),
fail-closed per chunk (delete only after a fetch confirms EVERY id carries
a metadata doc_id), census aggregated, final re-count.

```
command:  bash scripts/verify/purge-multi-user-test.sh
expected: 55,597 listed -> deleted by id in 100-chunks -> final count 0
actual:   listed 55597; purged 55597/55597 (last chunk 97);
          census: 61 distinct doc_ids, all with doc_id metadata, 0 refused;
          final count 55597 -> 0. External re-probe: rag-v2 = 5 (UNTOUCHED),
          enterprise-agentic-rag = 27 (UNTOUCHED), multi-user-test = 0.
verdict:  PASS
```

### 3. The downstream discovery: the Pinecone egress limit is EXHAUSTED

The first re-run of g2a.sh failed 44 checks; the visible-stderr probe shows
why — EVERY Pinecone READ now fails account-wide:

```
PineconeApiException: (429)
{"message": "Request failed. You've reached your egress limit for the
current month (1000000000 bytes). To continue reading data, upgrade your
plan."}
```

Writes (upsert/delete) and describe_index_stats still work (the probe
upsert+delete returned ids without error). The cap tripped between the
purge (finished ~09:05Z) and the first g2a attempt (09:33Z). **Honest
disclosure of contributors:** weeks of verify-script probes that listed +
fetched 55k leftover vectors whole (the accumulation problem A5.4 exists
to fix), the 2026-11-16 cleanup fetches, and — my own contribution,
disclosed — today's purge fetched all 55,597 metadata records (~hundreds
of MB). Reads recover when the monthly egress resets (2026-12-01) or if
the plan is upgraded — **Farhad's call** (cost decision).

### 4. The script changes (the fix)

- **Unique per-run namespaces** (b4, b5, b6, g1, g2a):
  `NS="multi-user-test-$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"` + a case guard
  that REFUSES any name not matching `multi-user-test-*`. Cleanup deletes
  the run's namespace WHOLE (`delete_all`, guarded again inside cleanup) —
  safe because the namespace was created empty for this run and only this
  run writes to it. A killed run can no longer poison a shared namespace;
  it leaves an isolated, self-named (and datable) namespace instead. No
  namespace-wide listing anywhere in cleanup (the 414 lesson). The
  per-document delete proofs (by EXACT recomputed ids) are unchanged.
  Read-only scripts (b2, b3) keep the shared namespace pin — they never
  write.
- **Rejection-before-embedding proofs (g2a.sh):** the corrupt-docx and
  zip-bomb checks moved to CHECK 0, run FIRST against the empty namespace,
  each followed by `ns_count()==0`; the oversize 413 in CHECK 1 re-asserts
  the namespace is still empty. Nothing can embed before a rejection.
- **MSYS_NO_PATHCONV guard (all seven scripts):** the first live re-run
  collapsed into a 401 cascade because MY OWN invocation exported
  MSYS_NO_PATHCONV=1 globally around the script — that flag kills MSYS
  path conversion for EVERY native binary inside (curl's /tmp cookie jars
  landed nowhere → 401s; `-o /dev/null` → curl exit 23; the host python
  got raw /tmp paths). The scripts rely on conversion being ON; dc()
  scopes the docker exclusion itself. Every script now REFUSES when the
  flag is globally set, with the reason inline. (Lesson recorded: the
  CLAUDE.md export rule is for individual docker commands, never around
  whole scripts.)

### 5. Live run of the changed g2a.sh (correct invocation; per-run namespace)

```
command:  bash scripts/verify/g2a.sh        (no global MSYS_NO_PATHCONV)
run:      2026-11-19 09:44:40Z — ns multi-user-test-20261119T094440Z-6718
result:   PASS=42 FAIL=5 SKIP=0 — ALL FIVE failures are the egress cap:
            private doc has exact vector ids        (fetch -> 429, empty)
            private vector tokens are exactly [user:bob]  (upstream empty)
            bob's answer cites his PRIVATE doc       (retrieval query 429)
            carol's answer cites the ORG doc         (retrieval query 429)
            rag-v2 ids unchanged                     (list -> 429, empty)
          The A5.4-SPECIFIC checks ALL PASSED:
            rejections happened BEFORE any embedding (namespace empty) 0==0
            oversize rejected BEFORE any embedding (namespace empty)   0==0
            the whole quota ladder (413/409x2/429+Retry-After, admin exempt)
            parallel race 201 201 409 409, committed sum bounded
            per-run namespace visible in the header; trap deleted it whole
          2 chat conversations (budget 4). rag-v2 comparison impossible
          while reads are capped — the stats endpoint confirms rag-v2 = 5
          before AND after the run (same 5 KB ids as H6 left them).
verdict:  PASS with 5 environment-blocked checks (recorded, not waved
          through — they re-run green on the first post-reset/upgrade run).
```

bash -n clean on all seven scripts. The remaining live proofs (b4/b5/b6/g1
re-runs + g2a's five blocked checks) are recorded as PENDING the egress
reset — their script changes are the same three-line mechanical pattern,
guarded, syntax-checked, and g2a.sh (the most complex change) ran live.

## A5.5 — No root-owned files in the repo (2026-11-19)

**The check** (scripts/verify/no-root-files.sh): a throwaway READ-ONLY
appuser container scans the repo. Detection on Docker Desktop/Windows: host
files map to root:root WORLD-WRITABLE inside containers, while files CREATED
by a root container carry the creator's umask (root:root, NOT world-writable)
— so ``find /app -user root ! -perm -002`` isolates container-created
artifacts (verified live: .env maps rwxrwxrwx, .pytest_cache was drwxr-xr-x).
Scope: the SOURCE TREE; uploads/ and data/ are runtime-data directories
(CLAUDE.md: never touched) — root-owned entries there are listed
INFORMATIONALLY, never deleted by a script.

**RED run (before any change):**

```
command:  bash scripts/verify/no-root-files.sh
expected: FAIL — historical root-owned artifacts exist
actual:   FAIL, 873 entries: .pytest_cache + __pycache__/*.pyc under
          app/, migrations/, tests/ — all created by the compose TEST
          container while it still ran as root (also the count grew from
          565 to 873 across today's pre-fix suite runs, proving the
          writer was live)
verdict:  PASS (RED as mandated)
```

**The writer fix** (docker-compose.yml, test service): ``user: "1000:1000"``
+ ``-p no:cacheprovider`` in the command (PYTHONDONTWRITEBYTECODE=1 already
comes from the image). The test container is now repo-write-FREE: bytecode
disabled, pytest cache disabled, tests write only to tmp_path in the
container's /tmp. The dev app service and every verify throwaway container
run the runtime image = appuser (image verified: built 2026-10-29 with
USER appuser) — they write only uploads/<doc_id>/ as appuser.

**Proof the writer is closed:**

```
command:  docker compose --profile test run --rm test python -m pytest tests -q
            (compose mode)   → 406 passed / 0 failed
command:  ... -e ENABLE_WEB_FALLBACK=false ...  (CI-mode flag)
            → 406 passed / 0 failed
command:  container scan after BOTH suites
expected: root-owned count unchanged (nothing new appeared)
actual:   873 -> 873 (both scans identical; every entry pre-existing)
verdict:  PASS
```

**The historical purge (Farhad approved via explicit ask, per the
working-tree safety rule):** the 873 entries are gitignored, regenerable
caches — no source, no data. First attempt (appuser container) hit
Permission denied on entries inside root-owned 755 dirs; completed with
the one-shot ``--user root`` container (the documented .incoming
precedent): pass 1 delete root-owned *.pyc files, pass 2 delete empty
__pycache__ dirs, pass 3 ``rm -rf /app/.pytest_cache``.

```
command:  docker run --rm --user root ... sh -c "<three passes>"
actual:   source-tree root count 873 (post-suite, pre-purge) -> 0
          git status after purge: only the A5.5 edits — no tracked file harmed
command:  bash scripts/verify/no-root-files.sh
actual:   PASS: no root-owned container-created files in the source tree
verdict:  PASS
```

**Reported, NOT touched (CLAUDE.md: report, never delete):** 800 root-owned
entries under ``uploads/`` — historical stored document files/dirs created
by pre-appuser-era containers (operator CLIs / early verify runs). They
either back dev-DB document rows or were orphaned when scratch DBs were
dropped (the bad%00name orphan class). Only the operator can decide their
fate (e.g. cross-check storage_path against the dev DB, then remove
genuinely orphaned dirs manually). Current writers cannot add to this set:
every container that bind-mounts the repo now runs non-root and writes
only to uploads/<server-generated-id>/ as appuser.

## A5.6 — RLIMIT_AS degrades EXPLICITLY off Linux (2026-11-19)

**Tests first** (tests/test_parser_platform.py — the Linux test container
cannot BE the unsupported platform, so the tests STUB the ``resource``
module; that stub is the unsupported platform):

```
command:  docker compose --profile test run --rm test \
            python -m pytest tests/test_parser_platform.py -q
RED run:
  expected: the two degradation tests RED (silent pass / uncaught
            AttributeError); the import-safety pin already green
  actual:   2 failed, 1 passed —
            FAILED missing_resource_module_degrades_explicitly
                   (ImportError path degraded SILENTLY — no message)
            FAILED resource_without_rlimit_as_degrades_explicitly
                   (AttributeError path NOT CAUGHT — would crash the parse
                   child and surface as a misleading ParseFailed('crashed'))
  verdict:  PASS (RED as mandated)
GREEN run: 3 passed; parser suite (tests/test_parser_subprocess.py)
           14 passed unchanged — the POSIX memory cap still works.
```

**The fix** (app/services/ingestion.py, ``_apply_memory_limit``): the
silent ``pass`` became a WARNING naming the platform, the reason, and what
still bounds the parse ("subprocess + hard timeout"); a second handler
catches AttributeError (``resource`` present without ``RLIMIT_AS`` on some
non-Linux POSIX). Import safety is pinned by a REAL subprocess test
(``sys.modules['resource'] = None; import app.services.ingestion`` →
exit 0) — the module never touches ``resource`` at import time.

**Full suite after the change:** 409 passed / 0 failed in compose AND
CI-mode (was 406 + 3 new tests).

```
verdict: PASS
```

## A5.7 — The four decorative settings removed in PART 0 (H7), confirmed
## (2026-11-19)

The four fields the H7 audit (commit `9e2d0c9`) found decorative and
REMOVED — not allowlisted (tests/test_settings_audit.py ALLOWLIST is
deliberately empty):

1. **`sample_kb_dir`** — read by nothing; the dead `ingest_sample_kb.py`
   script went with it (sample-KB ingestion is the operator CLI
   `ingest-org`, proven live in H6).
2. **`pinecone_test_namespace`** — a Settings field nothing read; the
   test namespace is a CONVENTION (conftest pin, CLAUDE.md rule), not a
   setting — and since A5.4, verify scripts use unique per-run namespaces.
3. **`conversation_retention_days`** — retention was never implemented;
   keeping the field implied a feature that does not exist (D19 stays
   open).
4. **`test_database_url`** — not a production setting; where the tests
   run is the RUNNER's provision (compose interpolation / CI env), read
   directly by conftest, never by app code.

**Confirmation sweep (this step):**

```
command:  grep for all four names + ingest_sample_kb across app/, tests/
          conftest, README.md, .env.example, docs/dev-testing.md,
          docker-compose.yml
expected: no live reference (historical/removal notes excepted)
actual:   only the intentional removal notes added THIS step; the
          backend-design.md code-impact rows are design HISTORY (their
          disposition already says "superseded by ingest-org CLI")
verdict:  PASS
command:  docker compose --profile test run --rm test \
            python -m pytest tests/test_settings_audit.py -q
actual:   8 passed — the reference audit still holds; nothing crept back
verdict:  PASS
```

**Documentation fixes made this step:**
- README.md: the deleted `python ingest_sample_kb.py` instruction (×2 —
  setup block and the tree listing) replaced with the operator CLI form;
- README.md: `PINECONE_NAMESPACE` default corrected to `rag-v2` (stale
  pre-B4 row said `enterprise-agentic-rag`);
- .env.example: `ENABLE_WEB_FALLBACK=true` template → `false` with the
  amended-D6 explanation (true = explicit operator opt-in; H4 fixed the
  code default and README but missed the template);
- docs/dev-testing.md: the "opt-in live Pinecone check" section described
  machinery that no longer exists (`RUN_LIVE_PINECONE` gone from the
  repo, `PINECONE_TEST_NAMESPACE` removed in H7) — rewritten to the
  current reality: the offline suite is keyless; live Pinecone happens
  only in scripts/verify/*.sh under per-run namespaces.
- .env.example TEST_DATABASE_URL rows are CORRECT as-is: the Settings
  field was removed, but the env var itself is real (compose
  interpolates it; conftest reads it) — infra var, not a code setting.

```
verdict: PASS
```
