# Independent security review — B2–B4 (branch `backend/multi-user`)

Reviewer: independent security review (as mandated), 2026-11-05 → 2026-11-09.
Scope: `backend/multi-user` vs `main` (B2 auth, B3 conversations, B4 documents/ACL),
`docs/backend-design.md`, `scripts/verify/`, plus the working tree at `afe2f81`.
Method: read-only code review, new adversarial tests under `tests/review/`, live
re-run of the whole suite against real Postgres (compose `test` profile), verifier
audit + mutation testing in a throwaway scratch copy, history/hygiene scans.
Real LLM calls: zero. Real `.env`, dev DB, real Pinecone namespace: untouched.

**Verdict on the claim "B2–B4 are secure and correct": NOT REFUTED at the
severity levels that matter — no Critical/High findings. Eight real defects
found (2 Medium, 6 Low), all with failing repro tests; nothing found that
crosses the trust boundary between users (no IDOR, no ACL leak, no session
or CSRF bypass). The suite killed all six kill-mutations (no vacuity found
in the security-critical paths).**

## Findings

| # | Severity | Location | Issue | Repro | Suggested fix | Confidence |
|---|----------|----------|-------|-------|---------------|------------|
| 1 | Medium | `app/services/documents.py:138-141` | `complete_delete` resolves a symlinked `storage_path` and unlinks the target. The guard (`target.is_file() and allowed_root in target.parents`) accepts any destination **under upload_dir**, including another document's file — deleting doc A removes doc B's file. `storage_path` is server-written today and `complete_delete` has no API surface until B6 (sweep CLI uses it), so this is a latent integrity hole, not a live one. | `tests/review/test_review_destructive_paths.py::test_complete_delete_must_not_follow_symlink_out_of_doc_dir` (fails) | Refuse symlinked paths: check `target.is_symlink()` (or open with `O_NOFOLLOW` semantics) before unlink; keep the resolved-path containment check. | High (behavior proven; reachability gated on B6) |
| 2 | Medium | `app/api/conversations.py:213-249` (TX2) vs `delete_conversation` | Delete while a send's graph is in flight: delete takes no row lock and removes the conversation (messages cascade); TX2 then INSERTs the assistant row → FK violation → unhandled raw framework 500. The user's question is cascade-deleted, the answer lost, and no `chat_failed` error-marker row is written. | `tests/review/test_review_concurrency.py::test_delete_during_pending_send_must_not_500` (fails) | Serialize delete against TX2 (e.g. refuse delete while pending, or take `FOR UPDATE` on the conversation row in the delete path so it waits out TX2), or catch the FK violation in TX2 and translate to a clean 502 + error-status marker. | High |
| 3 | Low | `app/api/routes.py:91-92`, `app/services/ingestion.py:14-23` | Upload type gate is filename-suffix only. Non-PDF bytes named `.pdf` reach PyPDFLoader and die late in a handled 500 with the doc row marked `failed` (no corruption, but the type contract rests on a client-controlled string). Admin-key gated; files are never served back. | `tests/review/test_review_upload_pipeline.py::test_ingest_content_type_must_be_checked` (fails) | Sniff magic bytes (`%PDF-`, `PK\x03\x04`) and reject with 4xx before creating any row or writing any file. | High |
| 4 | Low | `app/api/routes.py` ingest path (no parser bounds) | No parser timeout and no decompressed-size bound: the 25 MB cap bounds compressed bytes only. A docx (zip) whose XML decompresses >10 MB from <1 MB uploaded passes every gate and reaches the parser (proven with a 1000:1 deflate payload). DoS exposure for an admin-key holder or a compromised admin key; never a data-exposure issue. | `tests/review/test_review_upload_pipeline.py::test_oversized_docx_decompression_is_bounded_by_upload_cap` (documents acceptance) | Bound decompressed size during parse (stream-cap per document) and/or wrap `load_file` in a hard timeout. | Medium (exposure proven; exploitation impact estimated) |
| 5 | Low | `app/api/routes.py:71` | Legacy `/api/chat` uses `logger.exception`, so an upstream exception whose text echoes the question (OpenAI/psycopg-style errors do) lands verbatim in shared logs. The multi-user path already logs a static line (`app/api/conversations.py:210`) — same treatment needed. Also applies to `logger.exception("legacy ingest failed")` at `app/api/routes.py:130`. | `tests/review/test_review_privacy.py::test_legacy_chat_exception_text_must_not_reach_logs` (fails) | Static log line + exception class only, mirroring `conversations.py`. | High |
| 6 | Low | `app/rag/workflow.py:107` | `search_web` logs the raw Tavily error payload (`Tavily search failed: %s`); upstream HTTP errors can embed the query. Design §A6 already schedules this line's reduction to an error class in B5 — this review pins it as a real, reproducible leak today (trace JSON retained for the caller is fine). | `tests/review/test_review_privacy.py::test_tavily_error_text_must_be_reduced_to_class` (fails) | Reduce to `type(exc).__name__` (B5, §A6). | High |
| 7 | Low | `app/core/db.py:35` (residual gap) | `hide_parameters=True` redacts the `[SQL]`/`[parameters]` sections, but the DRIVER's own message (`e.orig`) can still embed server-echoed bind values for cast/constraint errors (`invalid input syntax for type uuid: "<value>"`). Reachable content (message text) doesn't hit value-echoing error classes in practice; the commit message "bound values never render in errors" is broader than the mechanism delivers. | `tests/review/test_review_privacy.py::test_postgres_engine_hides_bind_parameters_in_statement_rendering` (passes, documents the gap + control) | Scrub `e.orig` in a DBAPIError handler if driver-echoed values are judged reachable; otherwise document the boundary. | High (mechanism), Low (exploitability) |
| 8 | Low | `app/api/routes.py:91` + DB insert | Multipart filename with a raw NUL byte passes the suffix check and crashes the documents INSERT (psycopg `DataError: text fields cannot contain NUL`) → unhandled 500. Client-controlled string must map to 4xx, never an unhandled 500. Admin-key gated. | `tests/review/test_review_destructive_paths.py::test_ingest_raw_nul_filename_is_client_error_not_500` (fails) | Reject control characters in filenames (422) before the DB insert and the `open()`. | High |
| 9 | Info | `scripts/verify/b2.sh:38-40` (`ckc`) | b2.sh's `ckc` lacks the empty-needle/haystack guard b3/b4 have; an empty needle makes `grep -qi ""` vacuously pass. All current needles are string literals, so no check is vacuous today — robustness note for future edits. | code review | Port the b3/b4 empty-guard into b2.sh. | High |
| 10 | Info | `scripts/verify/b4.sh:293` | First flag of the A-GONE probe, `a_ids <= (ids - a_ids)`, is tautologically true (a set minus itself is disjoint). Harmless — the adjacent `len(a_ids & ids) == 0` check (line 297) is the real assertion — but the dead flag should go. | code review | Drop the tautological flag; keep the count check. | High |
| 11 | Info | `app/services/users.py:52-57` | `would_leave_zero_admins()` (last-admin guard for B6 demote/disable/delete) has no caller yet and no test coverage; it will be dead-code-untested until B6 lands. Bootstrap refusal (`bootstrap_first_admin`), the guard that DOES exist, is tested and mutation-verified. | code review | When B6 wires it in, require tests that kill mutations of it (the review's M5 pattern). | High |
| 12 | Info | legacy SQLite audit db (§A10 accepted) | Legacy `/api/chat` still writes plaintext question + trace into the legacy SQLite audit db until B5 runs the confirmation-gated drop. Accepted transitional state, pinned so nobody forgets why the drop is needed. | `tests/review/test_review_privacy.py::test_legacy_audit_still_carries_question_until_b5` (passes) | B5: confirmation-gated drop script (already planned). | High |
| 13 | Info | `POST /api/auth/login` | Login has no CSRF token (standard for credential endpoints; design is silent). Login CSRF attacks (force a victim into the attacker's session) are low-value here; noting for completeness. | — | None needed now; revisit if login starts side effects. | Medium |

Severity calibration: nothing found that exposes one user's data to another,
nothing that bypasses auth/CSRF/session controls, nothing destructive reachable
from the current API surface. The two Medium findings are integrity/robustness
defects that B6/B5 must close before the surface grows.

## Mutation testing (task 8 — "verify the verifier")

Verifier audit first: `git log -p scripts/verify/` shows each script committed
once (b2 `b9123eb`, b3 `f5133d1`, b4 `98eaf6a`) and never modified — no
loosened assertions across history. Content review found the two Info findings
above (#9, #10) and no empty-grep/count-only vacuity in the security checks
(absence checks are count-based against explicit `0`; b3/b4 helpers FAIL on
empty needle/haystack).

Mutation testing in a scratch copy (`%TEMP%\rag-mutation`, outside the repo;
compose `test` image, real `rag_test` Postgres). Baseline first: 196 passed /
7 failed (the six intentional hole-tests + `test_dockerignore.py`, which fails
in the scratch copy because `.dockerignore` isn't part of the copy — artifact,
not a regression). Each mutation then ran the full `tests/` suite:

| Mutation (security-critical line broken) | Result | Killed by (new failures beyond baseline) |
|---|---|---|
| M1: ownership filter dropped from `get_own_conversation` | **killed** | `test_idor_other_users_conversation_is_404_everywhere`, `test_admin_role_confers_no_conversation_access` (+2) |
| M2: CSRF check short-circuited to `False` | **killed** | 3× CSRF tests in `test_auth_api.py` + `test_conversation_requires_csrf_on_mutations` (+4) |
| M3: `retrieval_filter_for` returns org-only (owner id lost) | **killed** | 3× `test_documents_b4.py` + 2× review ACL tests (+5) |
| M4: `revoked_at` check dropped from `lookup_session` | **killed** | `test_lookup_rejects_revoked_session`, `test_revoke_user_sessions_keeps_current`, +2 auth-API tests (+4) |
| M5: `bootstrap_first_admin` refusal removed | **killed** | `test_bootstrap_refuses_when_any_admin_exists`, `test_bootstrap_refuses_even_when_admin_disabled` (+2) |
| M6: `hide_parameters=True` removed from engine | **killed** | `test_engine_hides_bound_parameters_from_errors` + review privacy pin (+2) |

**Surviving mutations: none.** All six mutated files were restored
byte-identical afterwards (verified by diff).

The TX1→graph→TX2 live evidence was correctly identified in the mandate as
proving only the reject branch. `tests/review/test_review_concurrency.py`
closes the gap: a slow fake graph, two genuinely simultaneous sends on a FRESH
conversation via production-style sessions (real commits, real `FOR UPDATE`),
20/20 rounds produced exactly one 200 and one 409, exactly 2×20 messages in
role order, and the 409'd sender never reached the graph (`len(calls) == 20`).
The test is not vacuous: with the pending check disabled it deterministically
fails (interleaved writes).

## Checked and fine

- **Route inventory** — 19 routes total; every route not on the reviewed
  unauthenticated allowlist carries `get_current_user`. Negative test
  (`test_every_non_allowlisted_route_requires_session_auth`) fails on any
  future unauthed route; full snapshot test pins the exact inventory.
- **No query-by-id without owner** — every conversation access resolves through
  `user_id == owner` (`app/services/conversations.py:30-45`); admin gets 404 on
  other users' conversations like anyone else (§A8 404-not-403), verified live
  against the API.
- **Cookies** — `sid`: HttpOnly, SameSite=Lax, Path=/, Secure when
  `COOKIE_SECURE=true`; `csrf`: JS-readable by design; `__Host-` prefixes under
  secure mode (live-verified, b2.sh CHECK 1B-d); `Cache-Control: no-store` on
  auth responses (live-verified, CHECK 2).
- **CSRF** — session-bound HMAC(token_hash, session id), constant-time
  comparison of BOTH header and cookie (review fix 4); cross-session token
  rejected (live + offline; mutation M2 killed). Standard no-CSRF-on-login
  noted as Info #13.
- **Sessions** — 256-bit `token_urlsafe`, DB stores SHA-256 only (64-hex
  verified live, raw token absent from any column, CHECK 12); absolute + idle
  expiry; fresh token every login; revocation on logout/password
  change/disable (mutation M4 killed); `populate_existing` prevents stale
  revoked state in a long-lived identity map.
- **Argon2** — argon2id m=64 MiB, t=3, p=4 (OWASP floor 19 MiB/2/1 — pinned by
  test); `needs_rehash` upgrade on login proven with a legacy-parameter hash.
- **Throttle** — (peppered-username-hash, IP) exponential 15 min→1 h keyed on
  the SOCKET peer; spoofed `X-Forwarded-For`/`X-Real-IP` cannot reset either
  the pair lockout or the per-IP sliding window (offline tests); 8 usernames/1
  IP spray → 429 (live, CHECK 1B-c); unknown usernames throttled identically
  (no enumeration via lockout behavior); throttled-attempt audit carries no
  username, not even hashed; operator `unlock` CLI audited. No username-wide
  lock → lockout cannot DoS a victim account (shared-IP collateral bounded to
  the pair).
- **Enumeration side channels** — unknown-user / wrong-password / disabled
  responses byte-identical incl. headers (live CHECK 3 + offline test);
  20×20 timing parity <1.5× (live CHECK 4); argon2 verify runs for both cases.
- **Forced-change gate** — covers all conversation routes added after B2
  (offline test) and was live-verified (CHECK 10); exemptions exactly
  {GET /api/me, POST /api/auth/password, POST /api/auth/logout}.
- **Retrieval ACL** — filter built ONLY from the session user
  (`retrieval_filter_for`); `get_retriever` refuses a missing filter (fail
  closed at the API boundary); vectors with no `read_access` match nothing
  (live against the real legacy Pinecone namespace: 0 hits, b4.sh);
  admin's filter is `["org", admin-id]` — cannot retrieve a user's private doc
  (offline test); user B asking A's exact question through the FULL chat path
  (API → graph → filtered retriever) gets no chunk, no citation, no mention of
  A's document; legacy org-only filter for the static-key surface (D8);
  chunk ids are doc-scoped — delete-by-doc_id cannot touch another document's
  chunks even after same-content re-ingest (live + fake-index tests).
- **Concurrency** — TX1 (lock + caps + pending + user message, committed) →
  graph (zero DB resources held) → TX2; real double-send race 20/20 exact
  (see above); delete-during-send defect found and filed (#2).
- **Uploads** — size cap enforced DURING streaming (413, pending row rolled
  back, only the partial single FILE removed — never a directory, per the
  2026-11-05 working-tree safety rule); `Path(...).name` strips traversal
  components (verified with `../../evil.txt`); same display_name from two
  owners → distinct doc ids, distinct storage dirs, no vector collisions;
  suffix/magic-byte finding filed (#3, #8).
- **Privacy** — `audit_events` carry counts/status/duration only — no
  question/answer/trace/title (marker-scanned live, CHECK 11); multi-user chat
  failure path logs a static line; response bodies never carry exception text;
  engine `hide_parameters=True` (mutation M6 killed) with the `e.orig`
  residual documented (#7); `LANGSMITH_TRACING=false` documented in
  `.env.example` with a staging-only warning — neither `LANGSMITH_*` nor
  legacy `LANGCHAIN_*` tracing vars are set by app code; throttled attempts
  leak no identifier; legacy SQLite audit plaintext pinned as accepted (#12).
- **Verifier hygiene** — no loosened assertions across git history; helpers
  fail loudly on empty inputs (b3/b4); b2 evidence includes the honest
  3-run history; every PASS line prints expected+actual.
- **Hygiene (task 9)** — zero secret patterns (`sk-`/`tvly-`/`pcsk_`/password
  literals) across ALL git history and in `docs/evidence/` (values redacted in
  evidence); `.gitignore` covers `.env`, `venv/`, `data/*.db`, `uploads/`;
  `.dockerignore` excludes `.env`, `.git`, `tests`, `uploads/`, `data/*.db`;
  Dockerfile multi-stage — runtime stage is non-root (`appuser`), contains no
  pytest/dev tools, declares HEALTHCHECK on `/api/health` (live CHECK 14);
  `pip-audit` on requirements+dev requirements: **no known vulnerabilities**
  (post langgraph-sdk 0.4.6 / pypdf 6.21.4 bumps); CI green on the pushed
  branch tip `afe2f81` (GitHub Actions run 2026-11-05, conclusion success).

## Could not verify

- **Live LLM/Pinecone behavior** — `PINECONE_API_KEY` has been empty since the
  2026-11-05 incident (mandate forbids the real namespace anyway). Retrieval
  ACL conclusions rest on (a) the committed live evidence (b4.sh, 26 PASS/0
  FAIL, including the real-namespace fail-closed 0-hit proof and per-vector-id
  delete proof) and (b) Pinecone-faithful fake-index tests run in this review.
  The b2.sh SKIP (legacy chat end-to-end real call) remains skipped.
- **CI on `tests/review/`** — the review tests are uncommitted at review time;
  CI ran the committed suite only. CI status above is for the branch tip's
  committed code.
- **Parser-failure magnitudes** — no parser timeout exists (#4), so worst-case
  wall-clock/CPU under hostile PDFs/XMLs could only be estimated, not measured.
- **Production deployment posture** (TLS termination, proxy trusted-hop
  configuration for real client IPs, secrets management) — repo-only review;
  the socket-peer throttle keying assumes the app sees the real client IP
  (direct or via a trusted proxy that overwrites XFF — current compose does).
- **Future B5/B6 surfaces** — reviewed design docs only; endpoints that don't
  exist yet (admin user management, complete_delete API) were assessed as
  latent-risk findings (#1, #11), not live ones.

## Artifacts and containment

- New tests: `tests/review/` (7 files, 31 tests: 25 pins passing, 6 failing
  hole repros — the failures ARE the findings' evidence).
- Everything ran offline/keyless via `conftest.py` env forcing; the only
  Postgres touched was the disposable `rag_test` database on the compose
  network; mutation testing ran in a throwaway scratch copy outside the repo,
  all mutated files restored byte-identical; no application code changed;
  nothing pushed.
