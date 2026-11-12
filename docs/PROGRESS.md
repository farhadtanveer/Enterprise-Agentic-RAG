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

## 2026-11-12 — B5 + B6 LIVE VERIFICATION (the mandated final step of phase-b5-b6)

- Scripts first committed: `scripts/verify/b5.sh` (B5, disposable app
  container + scratch db `rag_verify_b5`, loopback :8086) and
  `scripts/verify/b6.sh` (B6, `rag_verify_b6`, :8087, Pinecone namespace
  pinned to `multi-user-test`).
- **b5.sh: PASS=11 FAIL=0 SKIP=0** (2026-11-12 06:53:41Z). Legacy writer
  unimportable, AUDIT_DB_PATH setting gone, startup+chat+ingest write
  nothing to data/audit.db (host-side sqlite check: 4 rows before AND
  after, mtime unchanged — file preserved per C8), real legacy-chat marker
  absent from trace AND logs, legacy ingest still 200. ONE real LLM call.
- **b6.sh: PASS=33 FAIL=0 SKIP=0** (final run 06:57:57Z). ZERO real LLM
  calls. All six CHECK groups green: forced-change gate, 401/403 authz
  matrix, last-admin guard on every path incl. /api/me, disable/reset-
  password, admin documents (org upload, metadata listing, delete-ANY,
  vectors-first §A9.1, legacy namespace untouched 27=27), purge-private
  403/200, /api/me 422/200 + login dead + anonymized audit row.
- Script fix found by the first b6 run (30/1): "temp password logs in"
  expected 200 but the app rightly refuses login on the still-DISABLED
  account — reset-password (§A5) does not reactivate; enable is a separate
  explicit PATCH. Script strengthened to pin 401-while-disabled → PATCH
  is_active:true → 200 + must_change_password (a reset can never silently
  un-suspend). No app change; permanent test already pins the right thing.
- Pinecone hygiene: rag-v2 (default namespace) was holding 4 test-artifact
  vectors (3× b5-note.txt from two 2026-11-09 b5.sh attempts + today, 1×
  bad%00name.txt from F3 live testing — scratch DBs died, vectors
  persisted). Deleted BY ID on 2026-11-12; rag-v2 now 0 (pristine for the
  §A7 operator re-ingest), legacy namespace untouched (27).
- Post-run cleanup verified: multi-user-test 0 vectors, no rag-verify-*
  containers, no rag_verify_* databases.
- Evidence (raw live records): docs/evidence/b5-evidence.md,
  docs/evidence/b6-evidence.md (commit: this one).

## 2026-11-12 — G1 PART 0 started (task file committed; H1–H6 follow)

- Farhad reviewed the B5/B6 summary and issued docs/tasks/g1.md (committed
  in this commit). PART 0 = housekeeping H1–H6, one commit each, tests
  first. PART 1 = G1 access control — NOT started until PART 0 is done and
  reported.

## 2026-11-12 — G1 PART 0: H1 DONE (b5.sh namespace violation fixed)

- Commit (this one): scripts/verify/b5.sh now pins
  `PINECONE_NAMESPACE=multi-user-test` on the throwaway container and its
  cleanup trap deletes the script's vectors BY ID (this run's document_id
  + prior crashed runs' b5-note.txt leftovers; list → fetch → metadata
  match → delete(ids=…), never delete_all) BEFORE removing the container.
  Ingest response now parsed for document_id (new PASS pin).
- Live re-run (07:31Z): **PASS=12 FAIL=0 SKIP=0**; trap printed
  `cleanup: deleted 1 vectors by id`. External read-only probe:
  rag-v2 ids 0 before → 0 after (UNTOUCHED), multi-user-test 0 after
  (trap cleaned), legacy namespace 27 unchanged. 1 real LLM call.
- Evidence: docs/evidence/g1-part0-evidence.md (H1 section).

## 2026-11-12 — G1 PART 0: H2 DONE (B6 private-doc live gaps closed)

- Commit (this one): b6.sh now ingests a REAL private document per
  scenario via the ingest-private CLI (test namespace; docker cp with a
  native-Windows host path), proves deletion by EXACT vector ids
  (list → fetch → doc_id match → fetch(ids=…) after → absent; never
  stats), pins doc/user row removal, and its trap deletes tracked docs'
  vectors BY ID (delete_all removed). CHECK 5's org delete-ANY proof
  upgraded to exact ids too.
- First run failed 8 checks, ONE root cause (script-side, reported by the
  EMPTY guards): MSYS_NO_PATHCONV=1 docker cp broke the HOST path
  (C:\tmp\... not found). Fix: cygpath -w for the host side. No app code.
- Final run (07:41Z): **PASS=44 FAIL=0 SKIP=0** — admin bulk-purge of a
  real private doc → exact ids absent + row gone; DELETE /api/me for a
  user owning a real private doc → exact ids absent + doc/user rows gone
  + anonymized audit. rag-v2 untouched (0), multi-user-test empty after
  trap, legacy 27=27. ZERO LLM calls, 3 small embeddings.
- Evidence: docs/evidence/g1-part0-evidence.md (H2 section).

## 2026-11-12 — G1 PART 0: H3 DONE (last-admin guard race fixed with row locking)

- Commit (this one): tests/security/test_last_admin_race.py (20 attempts:
  two barrier-synced admins demote or disable EACH OTHER, fresh per-
  request DB sessions on the test engine, semantics-free widened guard
  window, alternating demote/disable) + the fix: active_admin_count now
  selects active-admin ids FOR UPDATE and counts in Python (an aggregate
  can't take row locks).
- What failed first (test before fix, as mandated): **20/20 attempts RED
  against the unlocked COUNT** — both racing requests answered 200 and
  the result was ZERO active admins (codes=[200, 200], both role=user).
  After the fix: 20/20 green — the second guard blocks until the first
  commit, re-reads, answers 409.
- Full offline suite: **253 passed / 0 failed** (67.5s, compose test
  profile; was 233 before H3 — grew exactly by the 20 race tests).
- Evidence: docs/evidence/g1-part0-evidence.md (H3 section incl. the
  exact red assertion and the reproduce-red recipe).

## 2026-11-12 — G1 PART 0: H4 DONE (web-fallback default flipped AND the switch made functional)

- Commit (this one): config default `enable_web_fallback` True → **False**
  (D6 amended §A14/CLAUDE.md — the flip had never landed), AND the
  previously DECORATIVE kill switch now actually gates: after_kb routes
  KB-weak → insufficient when off (no rewrite, Tavily never invoked);
  conditional-edge map gains the existing insufficient target. README row
  corrected; .env untouched (explicit operator opt-in stays valid).
- Explicit CONTRACT CHANGES to existing tests (declared, not weakened):
  settings pin True → False; the 3 web-path graph tests now opt in with
  enable_web_fallback=True (same assertions under the opt-in flag).
- New test: exploding-Tavily fake proves the question text NEVER reaches
  Tavily under the default (retry_count 0, query never rewritten).
- Full offline suite: **254 passed / 0 failed** (63.9s; 253 + 1).
- Evidence: docs/evidence/g1-part0-evidence.md (H4 section).

## 2026-11-12 — G1 PART 0: H5 DONE (data/audit.db reported, NOT deleted)

- Report (also in docs/evidence/g1-part0-evidence.md): path
  `C:\Users\mtanveer\Desktop\Work\PKM RAG\Enterprise-Agentic-RAG\data\audit.db`,
  12,288 bytes, git-ignored, **4 rows** in query_audit, latest
  2026-11-09T08:42Z (written by verify-run containers during the 2026-11-09
  b-runs, i.e. BEFORE the B5 writer removal). Nothing reads or writes it
  since B5 (re-proven live 2026-11-12). **Farhad deletes it when he
  chooses — not deleted here.**

## 2026-11-12 — G1 PART 0: H6 DONE (sample KB re-ingested into rag-v2) — **PART 0 COMPLETE**

- Commit (this one): `docker compose exec app python -m app.cli ingest-org
  /app/data/sample_kb` (operator action). company_it_handbook.md →
  01a1cecd-6389… (3 chunks), service_desk_runbook.md → 01a1cecd-7f02…
  (2 chunks). Verified by document listing + per-document EXACT vector
  ids (list → fetch → doc_id match; NO stats): 3 + 2 ids enumerated in
  docs/evidence/g1-part0-evidence.md, all scope=org / read_access=["org"],
  zero foreign vectors in rag-v2, legacy namespace 27 ids (untouched).
  Retrieval smoke with the org filter (1 real embedding, 0 LLM): VPN
  question → 4 hits from both KB docs. ~6 embeddings spent, 0 LLM calls.
- Orphan REPORTED (not mutated): dev-DB documents row bad%00name.txt
  (01a1ac27-7635…, status=indexed, 0 vectors since the junk cleanup) —
  operator may delete via the admin API.
- rag-v2 pre-state 0 ids → now exactly the 5 KB vectors. Real-LLM spend
  across PART 0: ONE call (H1 legacy chat); embeddings ~10 (all small,
  disclosed per step).

## 2026-11-12 — H4 follow-up: CI-caused fix (one more env-dependent test)

- CI `test` job went red at 1d458fe while compose was green: CI has no
  `.env`, so the amended default (fallback OFF) applied and
  test_prompt_injection.py (web-path, no explicit flag) short-circuited.
  Reproduced locally with `-e ENABLE_WEB_FALLBACK=false`. Fix: the M5
  test opts in explicitly (declared contract change; fencing assertions
  unchanged). Commit (this one).
- Full suite now green in BOTH modes: CI-mode (-e ENABLE_WEB_FALLBACK=
  false) **254 passed / 0 failed** AND compose-mode (.env opt-in true)
  **254 passed / 0 failed**. Lesson recorded in evidence: after
  env-default changes, run the FULL suite in CI-mode.

## 2026-11-12 — G1 PART 1 started (Farhad's go received): G1.1 DONE

- Commit (this one): Alembic 0005 — groups (name unique among ACTIVE via
  partial unique index, kind CHECK department|project_team, contact,
  is_active) + group_memberships (composite PK, both FKs CASCADE, role
  CHECK member|editor per C1, added_by SET NULL, added_at) + documents
  (scope CHECK org|group|private; group_id FK ON DELETE RESTRICT;
  uploaded_by FK ON DELETE SET NULL backfilled from owner_id;
  ck_documents_scope_owner_group enforcing D16/C2 shapes; dedupe unique
  extended with group_id). users.role untouched (DB rejects group_editor).
- Tests FIRST: tests/test_groups_schema.py — red before (module absent),
  then green: CHECK combos (3 valid + 8 rejected), kind CHECK, name
  unique-among-active-only, membership role/unique, users.role pin, group
  delete RESTRICTed + memberships CASCADE, user delete C2 (private dies,
  group/org survive, uploaded_by → NULL).
- Full offline suite: **272 passed / 0 failed**. Evidence:
  docs/evidence/g1-evidence.md (G1.1 section, incl. the two test-quality
  failures encountered: varchar(8) vs 'department' probe; stale identity
  map vs DB-side cascades).

## 2026-11-12 — G1.2 DONE (AccessResolver + prefixed tokens; §A8 regression caught + fixed)

- Commit (this one): app/services/access.py — AccessResolver Protocol
  (§A14.3) + DbAccessResolver (audiences_for org + ACTIVE groups +
  user:<self>, recomputed EVERY call; filter_for single $in; can_read
  mirrors + fail closed; is_editor per-group C1). conversations chat wired
  to it. add_documents emits strictly prefixed tokens (org / group:<id> /
  user:<id>), takes group_id, rejects group-without-group / private-without-
  owner. Legacy chat keeps ORG_ONLY_FILTER.
- DECISION #9 precondition verified live first: rag-v2 = 5 vectors, ZERO
  bare-UUID tokens → no migration.
- What failed first (the mandate working): my first wiring built the filter
  AFTER TX1's commit — the B3 pool test caught a §A8 violation (connection
  held across the graph run). Fix: resolver SELECT inside TX1, before the
  commit. Also: a "dangling group" row cannot even be STORED (FK RESTRICT =
  schema-level fail-closed); can_read probes for it go in-memory.
- DECLARED CONTRACT CHANGES: test_retrieval_acl.py + test_documents_b4.py
  bare-UUID pins → prefixed grammar (negative proofs unchanged in strength).
- New tests: audiences 0/1/2 groups + pinned order; revoke-on-next-call
  (same live user); role flip ≠ audience change (C1); archived group in NO
  audience; FULL-chat-path G1-vs-G2 isolation with positive controls;
  admin no-implicit-access until self-join.
- Full offline suite: **279 passed / 0 failed**. Evidence:
  docs/evidence/g1-evidence.md (G1.2 section).

## 2026-11-12 — G1.3 DONE (operator group CLIs + groups service)

- Commit (this one): app/services/groups.py (create/update/delete_group —
  delete refuses while documents reference the group; add/remove_member;
  set_member_role; groups_for_user ACTIVE-only mirroring the resolver;
  members_of; every change audited, ids/roles only, actor None for CLI)
  + app/cli.py create-group / add-member --role / remove-member /
  ingest-group <group> <file> (ownerless group doc per C2, vectors carry
  the prefixed group:<id> token). create_document grew group_id/uploaded_by.
- Tests FIRST: tests/test_groups_cli.py red (6 failed, no module) → green;
  full offline suite **285 passed / 0 failed**. Evidence:
  docs/evidence/g1-evidence.md (G1.3 section).

## 2026-11-12 — G1.4 DONE (admin group endpoints + member-facing routes)

- Commit (this one): 9 routes — admin: GET/POST/PATCH/DELETE
  /api/admin/groups (+members add/patch/delete; archive = is_active
  false; delete confirm-gated, 409 while documents exist, FK RESTRICT
  backstop; missing membership → 404; self-join audited actor==target
  per DECISION #4); member-facing: GET /api/me/groups (ACTIVE only) and
  GET /api/groups/{id}/members (editor 200 / member 403 / outsider-or-
  admin-non-member 404 per DECISION #4). Route inventory +9 consciously;
  the forced-change gate covered all 9 automatically (walks the live
  table). Groups router registered in main.py.
- Tests FIRST: tests/test_groups_api.py (11 red before routes) → green.
  What failed first: removed-membership probes mapped to 422 instead of
  404 (endpoints now check the membership row first); two test-side
  issues (shared cookie jar; identity-map + UUID-vs-string compare).
- Full suite: **296 passed / 0 failed in BOTH compose mode and CI-mode**
  (-e ENABLE_WEB_FALLBACK=false). Evidence: docs/evidence/g1-evidence.md
  (G1.4 section).

## 2026-11-12 — G1 LIVE VERIFICATION DONE (g1.sh 30/0/0 on the final run)

- Commit (this one): scripts/verify/g1.sh — throwaway container + scratch
  db rag_verify_g1 (:8088), namespace pinned multi-user-test AND
  ENABLE_WEB_FALLBACK=false (the amended default; a verify script must not
  ride the operator's .env opt-in). All scenario setup via the operator
  CLIs (ingest-org, create-group ×2, ingest-group ×2, add-member ×2,
  remove-member). Every doc verified by exact vector ids; the alpha
  vectors' read_access probed to be EXACTLY ['group:<alpha-id>'].
- Final green run (10:22:15Z): **PASS=30 FAIL=0 SKIP=0** — A cites
  [g1-note, org-note]; B [org-note, g2-note] with ZERO alpha; admin
  [org-note] only (no implicit access); remove-member alpha alice →
  A's SAME-cookie next question cites [org-note] with retrieval 2→1
  chunks; membership_removed audited; legacy namespace 27=27 (read-only);
  trap deleted 3 vectors by id; rag-v2 externally verified untouched (5).
- Honest disclosure in evidence: THREE runs to green (docker cp dir
  path-class bug → Tavily-detour via the operator's web-fallback opt-in →
  grader ambiguity from the alpha note reading like a competing answer);
  NO app code changed — scenario/script hardening only. Model budget:
  the four mandated conversations (A, B, admin, A-after-revoke) exceed
  the "≤3 real LLM calls" line; deviation DISCLOSED, not silently
  skipped (16 conversations total across all runs, ~3 LLM API requests
  each). Evidence: docs/evidence/g1-evidence.md.

## 2026-11-12 — G1 MUTATION RE-TEST DONE (10/10 killed, zero survivors) — **G1 COMPLETE**

- Commit (this one): scripts/mutation/run-g1-mutations.sh (harness; final
  run below) + docs/review/g1-mutation-report.md. Ten hand-written mutants
  over the four mandated targets, all KILLED: audience (M-A1 archived-in-
  audience, M-A2 self-token-drop, M-A3 org-token-drop), membership
  (M-B1 can_read inverted, M-B2 member-as-editor, M-B3 any-member-matches),
  last-admin lock (M-C1 FOR UPDATE removed → the exact 20/20 H3 red state;
  M-C2 disabled-admins-count → B6 pins), group filter (M-D1 static
  org-only, M-D2 chat-wiring regression). Baseline selection 76/0.
- What failed first: the first harness execution called M-C1/M-C2
  "survived" — a HARNESS bug (empty extra-test argument → the race suite
  never ran for the lock mutants; the 42-test count exposed it). Fixed;
  zero survivors in the final run.
- Full offline suite stands at **296 passed / 0 failed** (both compose and
  CI-mode); live verification 30/0/0 (see the previous entry).

## EXACT NEXT STEP

**G1 (docs/tasks/g1.md PART 1) COMPLETE — STOP and report to Farhad.**
Wait for his review of G1 before ANY further work. Do NOT start G2
(user-facing uploads, quotas, share flow, citations — his explicit rule).
