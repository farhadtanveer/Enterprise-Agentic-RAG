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
