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
