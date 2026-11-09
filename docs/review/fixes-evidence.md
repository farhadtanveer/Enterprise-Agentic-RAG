# Fixes evidence — security-review findings F1–F8 (raw records)

Verification of the review-fix commits `a7d3201..1d12574` (branch
`backend/multi-user`), 2026-11-09. Everything below is the RAW, unedited
output of the repeatable live verifiers run against the FIXED code, plus
the SAST gates and the offline suite. Setup per run: disposable app
container + scratch database (`rag_verify_b2`/`rag_verify_b4`, loopback
ports), the dedicated Pinecone test namespace (`multi-user-test`, deleted
afterwards), the legacy namespace only read via index stats. The dev DB and
the real namespace were never touched.

## Summary

| Run | Result | Notes |
|---|---|---|
| Full offline suite (compose `test` profile) | **215 passed / 0 failed** | includes the 6 former acceptance tests, now green, promoted to `tests/security/` (F8) |
| `scripts/verify/b2.sh` | **PASS=72 FAIL=0 SKIP=0** | the previously skipped legacy-chat end-to-end REAL call (1 LLM call) ran and passed — Pinecone/OpenAI keys refilled |
| `scripts/verify/b3.sh` | **PASS=53 FAIL=0 SKIP=0** | conversations/chat ownership, TX discipline, limits |
| `scripts/verify/b4.sh` | **PASS=26 FAIL=0 SKIP=0** | Pinecone ACL/re-ingest/delete; includes the F7-corrected A-GONE check |
| Bandit 1.9.4 (`--severity-level high --confidence-level medium`) | **exit 0 — zero findings at any severity** | dead `PASSWORD_ROUTE` constant deleted (B105 false positive) |
| Semgrep 1.183.0 (`p/default --severity ERROR --error`) | **exit 0 — zero findings** | two JUSTIFIED suppressions in the legacy SQLite audit writer (fully parameterized INSERT; constant-whitelist ALTER TABLE) |
| CI | branch pushed through `1d12574`; the `sast` job runs both gates on GitHub Actions | |

## What failed first, per fix (TDD record)

- **F1**: started red on
  `test_complete_delete_must_not_follow_symlink_out_of_doc_dir` (symlink to
  another doc's file was deleted). First intermediate failure during the
  fix: the new `test_save_upload_stream_refuses_path_storage_names` test
  exposed that `Path(storage_name).name != storage_name` does NOT reject
  `C:\evil.txt` on POSIX (backslash is not a separator there) — fixed with
  an explicit platform-independent separator/`..`/NUL check before green.
- **F2**: started red on `test_delete_during_pending_send_must_not_500`.
  First fix attempt caught `IntegrityError` only and STILL returned 500 —
  a temporary probe (not committed) showed the flush raises
  `sqlalchemy.orm.exc.StaleDataError` on the `conversations` UPDATE (0 rows
  matched) BEFORE the message INSERT ever fires. Both exception shapes are
  now caught; both interleavings tested.
- **F3**: started red on `test_ingest_raw_nul_filename_is_client_error_not_500`.
  Intermediate failure: httpx percent-encodes NUL in multipart filenames, so
  the API-level zoo test initially asserted an unsendable case — raw NUL is
  covered by the manual-body acceptance test; the zoo test now covers the
  other rejections.
- **F4**: started red on `test_legacy_chat_exception_text_must_not_reach_logs`.
  Green on first run after the change.
- **F5**: started red on `test_tavily_error_text_must_be_reduced_to_class`.
  Green on first run after the change.
- **F6**: started red on `test_ingest_content_type_must_be_checked`. Green
  after the magic-byte gate + streaming text validator; the bogus-docx pin
  (PK-magic garbage → handled 500) intentionally preserved.

## b2.sh — raw output (2026-11-09 08:41:03Z, rag_verify_b2)

```
==============================================================
B2 LIVE VERIFICATION — 2026-11-09 08:41:03Z — disposable db rag_verify_b2
==============================================================
PASS  disposable app healthy on http://127.0.0.1:8082  (expected=200 actual=200)
---- CHECK 1: bootstrap-admin / reset-password / unlock (password via stdin, never argv)
PASS  bootstrap-admin first run creates the admin  (found: created (id)
PASS  bootstrap-admin second run refuses  (found: already exists)
---- CHECK 2: login Set-Cookie flags + no-store
PASS  sid cookie HttpOnly  (found: HttpOnly)
PASS  sid cookie SameSite=lax  (found: SameSite=lax)
PASS  sid cookie Path=/  (found: Path=/)
PASS  csrf cookie present  (found: csrf=)
PASS  csrf cookie NOT HttpOnly (JS-readable)  (expected=no-httponly actual=no-httponly)
PASS  sid Secure ABSENT on plain-http dev  (expected=absent actual=absent)
PASS  login Cache-Control: no-store  (expected=cache-control: no-store actual=cache-control: no-store)
PASS  /api/me returns the user  (found: "username":"verifyboss")
PASS  /api/me Cache-Control: no-store  (expected=cache-control: no-store actual=cache-control: no-store)
---- CHECK 3: unknown user vs known user/wrong password — identical status+body+headers
PASS  responses byte-identical (headers+body, Date excluded)  (expected=identical actual=identical)
PASS  generic 401 status  (expected=401 actual=401)
---- CHECK 4: timing parity (20 unknown vs 20 known-wrong)
      unknown user:      median=0.028s p95=0.145s
      known-user wrong:  median=0.022s p95=0.143s
PASS  timing difference is small (<1.5x)  (expected=SMALL actual=SMALL)
---- CHECK 5: (username, IP) lockout + unknown-username parity + unlock
PASS  correct password after 5 failures -> 429  (found: 429)
PASS  Retry-After present  (found: Retry-After:)
401 401 401 401 401 PASS  unknown username 6th attempt -> 429 (parity)  (expected=429 actual=429)
PASS  unlock clears throttle records  (found: Cleared)
PASS  unlock restores access (200 on correct login)  (expected=200 actual=200)
---- CHECK 6: CSRF double-submit + session binding
PASS  no header -> 403 csrf_failed  (found: "code":"csrf_failed")
PASS  no header status 403  (expected=403 actual=403)
PASS  wrong token 403  (expected=403 actual=403)
PASS  token from another session -> 403  (expected=403 actual=403)
PASS  correct token -> 200 (and revokes server-side)  (expected=200 actual=200)
---- CHECK 7: logout revokes server-side
PASS  replayed cookie after logout -> 401  (expected=401 actual=401)
---- CHECK 8: password change semantics + policy
PASS  password change succeeds  (found: password_changed)
PASS  other session -> 401 after change  (expected=401 actual=401)
PASS  current session stays valid  (expected=200 actual=200)
PASS  old password rejected  (expected=401 actual=401)
PASS  11-char password rejected (422)  (expected=422 actual=422)
PASS  129-char password rejected (422)  (expected=422 actual=422)
PASS  username as password rejected (422)  (expected=422 actual=422)
PASS  username as password, case variant rejected  (expected=422 actual=422)
PASS  username as password, whitespace variant rejected  (expected=422 actual=422)
---- CHECK 9: disabled user — session + login
PASS  victim user created (check_ prefix)  (found: created checkvictim)
PASS  victim session works before disable  (expected=200 actual=200)
PASS  disabled user session dies on next request  (expected=401 actual=401)
PASS  disabled user login -> generic 401 (no disabled hint)  (found: "detail":"Invalid username or password")
---- CHECK 10: forced-change gate on a temporary password
PASS  reset-password issues temp password + revokes sessions  (found: revoked)
PASS  temp login flags must_change_password  (expected=true actual=true)
PASS  non-exempt route blocked while temp  (expected=403 actual=403)
PASS  gate body is password_change_required  (found: "code":"password_change_required")
PASS  exempt: GET /api/me reachable  (expected=200 actual=200)
PASS  exempt: password change reachable  (expected=200 actual=200)
PASS  exempt: logout reachable  (expected=200 actual=200)
---- CHECK 11: marker strings never reach logs / audit_events / throttle rows
PASS  marker absent from app logs  (expected=0 actual=0)
PASS  marker absent from audit_events  (expected=0 actual=0)
PASS  marker absent from login_throttle rows  (expected=0 actual=0)
PASS  marker absent from users table  (expected=0 actual=0)
---- CHECK 12: sessions store only sha256 token hashes
PASS  every token_hash is 64-hex  (expected=0 actual=0)
PASS  raw cookie value nowhere in sessions  (expected=0 actual=0)
PASS  raw cookie value nowhere in sessions (any column)  (expected=0 actual=0)
---- CHECK 13: legacy /api/chat + /api/ingest unchanged in shape
PASS  legacy chat validates body (422)  (expected=422 actual=422)
PASS  legacy ingest wrong admin key -> 401  (expected=401 actual=401)
PASS  legacy ingest unsupported type -> 400  (expected=400 actual=400)
PASS  legacy chat answers end-to-end (1 real LLM call)  (expected=200 actual=200)
---- CHECK 14: container posture
PASS  container runs as appuser  (expected=appuser actual=appuser)
PASS  default-build image has no pytest  (expected=0 actual=0)
PASS  image declares HEALTHCHECK on /api/health  (expected=1 actual=1)
PASS  container reports healthy  (expected=healthy actual=healthy)
---- CHECK 1B-a: APP_ENV=production with dev defaults refuses to start
PASS  refusal message lists the dev settings  (found: Refusing to start)
PASS  refusal names COOKIE_SECURE  (found: COOKIE_SECURE=false)
PASS  refusal names the throttle pepper  (found: LOGIN_THROTTLE_PEPPER)
PASS  refusal names dev DB credentials  (found: dev default credentials)
PASS  process exits non-zero  (expected=NONZERO actual=NONZERO)
---- CHECK 1B-c: per-IP login attempt limit (spray many usernames, one IP)
      spray sequence (8 distinct usernames, 1 IP): 401 401 401 401 401 429 429 429
PASS  8th distinct username from one IP -> 429  (expected=429 actual=429)
---- CHECK 1B-d: __Host- cookie names when COOKIE_SECURE=true
PASS  __Host-sid cookie used  (found: __Host-sid=)
PASS  __Host-sid carries Secure  (found: Secure)
PASS  __Host-csrf cookie used  (found: __Host-csrf=)
---- CHECK 1B-e: no CLI command reads a password from argv
PASS  no argv password parameters in app/cli.py  (expected=0 actual=0)
PASS  passwords read via hidden prompts (getpass-style)  (expected=YES actual=YES)
==============================================================
RESULT: PASS=72 FAIL=0 SKIP=0  (2026-11-09 08:42:55Z)
==============================================================
```

## b3.sh — raw output (2026-11-09 09:08:11Z)

```
==============================================================
B3 LIVE VERIFICATION — 2026-11-09 09:06:55Z — disposable db rag_verify_b3
==============================================================
PASS  disposable app healthy  (expected=200 actual=200)
PASS  admin login ok  (expected=200 actual=200)
PASS  regular user login ok  (expected=200 actual=200)
---- CHECK CRUD: create/list/get/rename + send (REAL graph call #1, direct route)
PASS  create conversation returns id  (found: "title":"B3 live conv")
PASS  list shows the conversation  (found: 01a1bf8f-e367-711a-ab73-1c0598b05365)
PASS  list carries pagination envelope  (found: "total":1)
PASS  get returns metadata  (found: "B3 live conv")
PASS  rename works  (found: "B3 renamed")
PASS  send message (real graph, direct route) -> ok  (found: "status":"ok")
PASS  assistant message carries model  (found: "model")
PASS  assistant message carries trace  (found: "trace")
PASS  assistant message carries duration_ms  (found: "duration_ms")
PASS  assistant message carries token counts  (found: "prompt_tokens")
PASS  messages list has both rows  (found: "role":"user")
PASS  assistant row present  (found: "role":"assistant")
PASS  citations persisted on assistant row  (found: "citations")
PASS  chat_asked audit row exists with route  (found: chat_asked)
PASS  audit carries no question text  (absent: helpful)
PASS  audit carries no answer text  (absent: final answer)
---- CHECK authz: 401 unauth, 403 missing CSRF, 404 IDOR on every verb, admin 404
PASS  unauthenticated list -> 401  (expected=401 actual=401)
PASS  unauthenticated send -> 401  (expected=401 actual=401)
PASS  missing CSRF on create -> 403  (expected=403 actual=403)
PASS  IDOR get -> 404  (expected=404 actual=404)
PASS  IDOR list messages -> 404  (expected=404 actual=404)
PASS  IDOR send -> 404  (expected=404 actual=404)
PASS  IDOR rename -> 404  (expected=404 actual=404)
PASS  IDOR delete -> 404  (expected=404 actual=404)
PASS  admin reading user conv -> 404  (expected=404 actual=404)
PASS  admin listing sees 0 user convs  (expected=0 actual=0)
PASS  nonexistent id -> 404  (expected=404 actual=404)
---- CHECK failure path: empty OPENAI key -> 502, rows, marker absent
PASS  graph failure -> 502  (expected=502 actual=502)
PASS  user message STAYS + error marker row  (found: user:ok)
PASS  assistant error row present  (found: assistant:error)
PASS  error row content empty (no raw exception)  (expected=1 actual=1)
PASS  chat_failed audit row exists  (found: chat_failed)
PASS  chat_failed audit has no marker  (absent: SECRET-MARKER-B3-LIVE)
PASS  marker absent from app logs on failure  (expected=0 actual=0)
PASS  retry after recovery -> 200 (REAL graph call #2)  (expected=200 actual=200)
---- CHECK double-submit: two concurrent sends serialize on the row lock
      concurrent send codes: 409 409
PASS  concurrent send one -> 409  (expected=409 actual=409)
PASS  concurrent send two -> 409  (expected=409 actual=409)
PASS  409 bodies name message_pending  (expected=2 actual=2)
---- CHECK delete conversation
PASS  delete returns status deleted  (found: "status":"deleted")
PASS  get after delete -> 404  (expected=404 actual=404)
---- CHECK caps + per-user chat rate limit (override container :8085)
PASS  title > 200 chars -> 422  (expected=422 actual=422)
PASS  question > MAX_MESSAGE_LENGTH(50) -> 422  (expected=422 actual=422)
PASS  conversation cap: 3rd create -> 409  (expected=409 actual=409)
PASS  message cap: 3rd message -> 409  (expected=409 actual=409)
PASS  rate limit: first send -> 200 (REAL graph call #3; allowance 2 — the earlier cap-409 send consumed one)  (expected=200 actual=200)
PASS  rate limit: immediate second send -> 429  (expected=429 actual=429)
PASS  429 carries Retry-After  (found: retry-after)
---- CHECK D19 + legacy
PASS  ask() has no follow-up context parameter (D19)  (expected=question,retrieval_filter actual=question,retrieval_filter)
PASS  legacy /api/chat still validates body (422)  (expected=422 actual=422)
PASS  legacy /api/ingest wrong key still 401  (expected=401 actual=401)
==============================================================
RESULT: PASS=53 FAIL=0 SKIP=0  (2026-11-09 09:08:11Z)
==============================================================
```

## b4.sh — raw output (2026-11-09 09:11:28Z, rag_verify_b4, namespace multi-user-test)

```
==============================================================
B4 LIVE VERIFICATION — 2026-11-09 09:08:34Z — disposable db rag_verify_b4 — namespace multi-user-test
==============================================================
PASS  disposable app healthy  (expected=200 actual=200)
      old-namespace baseline: {"enterprise-agentic-rag": 27, "rag-v2": 1}
      bootstrap: Admin 'verifyboss4' created (id 01a1bf91-512a-70e0-b0a2-d6e5e1a1ab6c).
      users: users ok
---- CHECK ingest: org doc + private doc for A (real embeddings, test namespace)
      cp-check: total 12
      cp-check: drwxr-xr-x 2 root root 4096 Nov  9 09:08 .
      cp-check: drwxrwxrwt 1 root root 4096 Nov  9 09:08 ..
      org ingest raw: indexed vpn-handbook.txt → org document 01a1bf91-7533-7378-894f-c09b0a1a8460
PASS  org doc indexed  (found: org document)
      A ingest raw: indexed policy-notes.txt → private document 01a1bf91-b445-7155-aed8-e775eb6f5194 (owner checkb4usera)
PASS  private doc A indexed  (found: private document)
PASS  test namespace populated (org + A private vectors)  (expected=YES actual=YES)
      vector ids in test namespace after org+A ingest: 2
---- CHECK same-filename upload by two owners
      B ingest raw: indexed policy-notes.txt → private document 01a1bf91-f7f6-71a9-94ee-5e2b61ed6836 (owner checkb4userb)
PASS  two docs share display_name, distinct ids  (expected=2 actual=2)
PASS  distinct document ids for same filename  (expected=2 actual=2)
PASS  same filename two owners: vector count grew by exactly B's chunks  (expected=3  actual=3 )
---- CHECK retrieval ACL: user A full chat path (REAL graph call)
      app cookie_secure setting: False
      login Set-Cookie: set-cookie: sid=<redacted>; HttpOnly; Max-Age=2592000; Path=/; SameSite=lax;set-cookie: csrf=<redacted>; Max-Age=2592000; Path=/; SameSite=lax;
      A login check: 200; login body probe: {"user":{"id":"01a1bf91-5cef-7792-a7ec-d7cb4bd7d070","username":"checkb4usera","display_name":null,"role":"user","must_c
      A chat raw (first 200 chars): {"id":"01a1bf92-415f-714b-91a1-32ac80d0a303","conversation_id":"01a1bf92-28a8-7958-9443-8301ee0381a9","role":"assistant","status":"ok","content":"To get access to the finance policy system, follow the
      A conv id: 01a1bf92-28a8-7958-9443-8301ee0381a9; A login check: 200
PASS  A's chat succeeded through the graph  (found: "status":"ok")
PASS  A's answer cites THEIR private doc  (found: policy-notes)
      A's chat source_used: "source_used":"private_kb"
PASS  A's chat used the private KB  (found: private_kb)
---- CHECK retrieval ACL: user B + legacy org-only + fail-closed (retriever-level)
      B/A retrieval raw: B-DOCS ['01a1bf91-7533-7378-894f-c09b0a1a8460', '01a1bf91-f7f6-71a9-94ee-5e2b61ed6836'];A-DOCS ['01a1bf91-7533-7378-894f-c09b0a1a8460', '01a1bf91-b445-7155-aed8-e775eb6f5194'];
      B/A retrieval stderr tail: 
PASS  B's retrieval includes the org doc  (found: 01a1bf91-7533-7378-894f-c09b0a1a8460)
PASS  B's retrieval EXCLUDES A's private doc  (absent: 01a1bf91-b445-7155-aed8-e775eb6f5194)
PASS  A's retrieval includes their private doc  (found: 01a1bf91-b445-7155-aed8-e775eb6f5194)
PASS  A's retrieval excludes B's private doc  (absent: 01a1bf91-f7f6-71a9-94ee-5e2b61ed6836)
PASS  legacy org-only filter sees the org doc  (found: 01a1bf91-7533-7378-894f-c09b0a1a8460)
PASS  legacy org-only filter NEVER sees private docs  (absent: 01a1bf91-b445-7155-aed8-e775eb6f5194)
PASS  fail-closed: legacy namespace vectors (no read_access) match NOTHING under the filter  (expected=0 actual=0)
---- CHECK re-ingest idempotency
      idempotency raw: IDEM 1 1 4 4
      idempotency (same-doc upsert | stable id set | counts): 1 1 4 4
PASS  re-ingest writes land under the same doc_id  (expected=1 actual=1)
PASS  re-ingest is idempotent (id set unchanged after double write)  (expected=1 actual=1)
---- CHECK delete: vectors-first protocol removes ONLY A's vectors
PASS  A's private doc deleted via §A9.1 protocol  (found: deleted A doc)
      A-gone raw: A-GONE 0
PASS  none of A's recomputed vector ids remain  (expected=0 actual=0)
PASS  only A's vectors removed (count dropped by exactly A's chunk_count)  (expected=3 actual=3)
PASS  B's same-name doc still fully visible after A's delete  (expected=True actual=True)
---- CHECK oversize upload -> 413
PASS  legacy ingest 26MB (> MAX_UPLOAD_MB=25) -> 413  (expected=413 actual=413)
PASS  no document row left behind  (expected=0 actual=0)
---- CHECK old namespace untouched
      old-namespace after: {"enterprise-agentic-rag": 27, "multi-user-test": 3, "rag-v2": 1}
PASS  legacy namespace vector count unchanged (test namespace may legitimately appear)  (expected="enterprise-agentic-rag": 27 actual="enterprise-agentic-rag": 27)
==============================================================
RESULT: PASS=26 FAIL=0 SKIP=0  (2026-11-09 09:11:28Z)
==============================================================
```

## SAST gates — raw

```
$ python -m bandit -r app --severity-level high --confidence-level medium
(exit 0 — no output; full-scan metrics: Low=0 Medium=0 High=0 after removing the dead PASSWORD_ROUTE constant)

$ docker run --rm -v "$PWD:/src" semgrep/semgrep:1.183.0 semgrep scan --config p/default --severity ERROR --error --quiet /src/app
(exit 0 — no output; the only ERROR-severity candidate, the legacy sqlite audit writer, is
 suppressed with inline JUSTIFIED comments: parameterized INSERT, constant-whitelist ALTER TABLE)
```
