# Progress log

Rule (CLAUDE.md, added 2026-11-09): after every finished step, append date,
step, commit hash, proving tests/evidence, and the exact next step. A new
session must be able to resume from this file and `git log`.

---

## 2026-11-09 — security-review fixes F1–F8 (Part 1 of the fix mandate)

| Step | Commit | Proven by |
|---|---|---|
| F1 complete_delete symlink refusal + server-generated storage names | `a7d3201` | `tests/security/test_destructive_paths.py` green (acceptance: symlink test); suite 200 passed / 5 failed (remaining = F2–F6 acceptance) |
| F2 delete-during-send → clean 502 (delete wins; both orders tested) | `41b96f4` | `tests/security/test_chat_concurrency.py` 20/20 green incl. new second-order test; suite 202/4 |
| F3 422 on NUL/control/reserved/overlong filenames + titles | `92c9a69` | `tests/security/test_input_validation.py` + NUL acceptance green; suite 208/3 |
| F4 legacy chat/ingest log exception CLASS only | `c11fd53` | `test_legacy_chat_exception_text_must_not_reach_logs` green; suite 210/1 |
| F5 Tavily error class+status only | `ee1701c` | `test_tavily_error_text_must_be_reduced_to_class` green; suite 210/1 |
| F6 content-based upload type check | `45e1aac` | `tests/security/test_upload_pipeline.py` 8/8 green; **full suite 215 passed / 0 failed** |
| F7 verify-script guards (b2 ckc empty-guard, b4 tautology) | `3dc3a6b` | `bash -n` clean; live re-runs below |
| F8 review tests promoted to permanent `tests/security/` | `d4d0324` | suite 215/0 from new paths; route-inventory negative test now permanent |
| SAST: Bandit + Semgrep in CI (fail high only) | `1d12574` | bandit `--severity-level high` exit 0 (zero findings at ANY severity after deleting dead `PASSWORD_ROUTE`); semgrep `p/default --severity ERROR --error` exit 0 (2 JUSTIFIED suppressions in legacy audit writer); suite 215/0 |
| Fixes evidence (raw live records) | `8201cc5` | `docs/review/fixes-evidence.md`: **b2.sh 72 PASS / 0 FAIL / 0 SKIP** (the previously-skipped legacy-chat REAL call ran and passed), **b3.sh 53/0/0**, **b4.sh 26/0/0** (Pinecone test namespace; legacy namespace untouched) |

All pushed to `origin/backend/multi-user` through `8201cc5`.

## 2026-11-09 — Part 2: department/group design amendment (docs only)

- Commit: (this commit) — `docs/backend-design.md` gains §A14 (audiences
  org/group/private with `group:<id>`/`user:<id>` tokens, `group_editor`
  role, AccessResolver interface, M1–M3 Alembic plan from `0004_documents`,
  share flow with D11 preserved, quotas, permission matrix, 10
  backend-agnostic behavior tests, code-impact table, abuse cases, 10
  DECISIONS FOR FARHAD); D4/D6/D10/D11 rows amended; D6 web-fallback
  default flips to `false` (decided).

## 2026-11-09 — Part 1 corrections + S1/S2/S3 + B5 (phase-b5-b6 task)

- Part 1 design corrections: `f6b27cd` (§A14 v2 + A8/A12/A13 aligned).
- S1 delete-during-send 409 conversation_deleted + chat_failed audit: `dee4e09`
  (acceptance window test unchanged; two new exact-semantics tests).
- S2 corrupt upload 422 generic / infra 500 boundary: `ac18d9e`.
- S3 mutation re-test F1/F2/F6: `1aea641` + docs/review/s3-mutation-report.md
  (6 mutants: 4 killed outright, M-B equivalent mutant documented, M-E killed
  by the new pre-row 415 pin). Zero survivors.
- B5 legacy audit writer REMOVED (C8): app/services/audit.py deleted,
  write_audit/init_db/AUDIT_DB_PATH gone, tests/test_audit_log.py removed with
  its subject, Semgrep suppressions removed with the file (both SAST gates
  exit 0), README claims fixed. New pins: legacy chat/ingest create NO audit
  db; mandated log-content test (DBAPIError with marker → absent from logs +
  body). NOTE: data/audit.db still EXISTS on disk (4 rows, latest
  2026-11-09T08:42Z — recreated by the verify-run app containers) — preserved
  per instructions, never deleted.
- What failed first during B5: the mandated DB-error marker test exposed that
  the S1 handlers caught the PARENT DBAPIError and misreported any DB failure
  as conversation_deleted — split into IntegrityError/StaleDataError (409)
  vs DBAPIError (502, status db_error).

## 2026-11-09 — PART 4: B6 admin surface (continued)

- B6 commit (this one): app/api/admin.py (users create/list/patch/
  reset-password/delete + purge-private-documents C4; documents org-only
  metadata listing C4, org upload, delete-ANY C3), DELETE /api/me (auth),
  require_admin dependency (403/401), last-admin guard on EVERY path incl.
  self (409), forced-change gate probed over the LIVE route table (route
  inventory extended consciously with the 9 admin routes), audit events
  without content, §A9.1 vectors-first on every delete, C2 (org docs
  survive user deletion; self-deletion audit row anonymized — the FK
  cannot reference the deleted actor on INSERT).
- TEST INFRASTRUCTURE FIX (found by B6, documented in conftest): the
  savepoint-rollback fixture was unsafe under SQLAlchemy 2.1.4 — after
  session.commit() the session's root transaction could end and the next
  autobegin took over the connection's real transaction, so any later
  db.rollback() (every 4xx handler!) wiped the whole outer transaction
  (revoked-sessions went invisibly empty; B5's audit.db rows came from
  the same class of surprise). db_session now TRUNCATEs all tables per
  test and runs REAL commits — production semantics, statement-type
  agnostic. joined_session_factory simplified accordingly.
- TWO REAL BUGS the B6 tests caught in EXISTING code:
  (1) revoke_user_sessions: `id != keep_id` with keep_id=None is
  `id != NULL` in SQL — password resets/disables revoked ZERO sessions;
  (2) login_throttle reset/clear used Core DELETE (same fixture poison);
  both rewritten ORM-style, semantics preserved (tests pass).
- What failed first: create/list 403 (shared cookie jar — per-identity
  clients), FK violation on self-delete audit (actor must be NULL),
  login limiter exhaustion (autouse fresh-limiter fixture), then the two
  real bugs above.

## EXACT NEXT STEP

Decisions #1–#10 and corrections C1–C8 have LANDED (f6b27cd,
docs/tasks/phase-b5-b6.md). Part 1 + S1/S2/S3 + B5 + B6 are done (see the
2026-11-09 entries). **EXACT NEXT STEP: scripts/verify/b5.sh + b6.sh live
runs with raw evidence (≤3 real LLM calls total), then STOP — do NOT
start G1.**
