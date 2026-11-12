# B6 live verification evidence (admin surface + DELETE /api/me)

- **Commits:** B6 feature `830c4c6`; verify script + this evidence in the
  final B5/B6 evidence commit
- **Timestamp:** 2026-11-12 06:57:57Z (UTC, final green run)
- **Command:** `bash scripts/verify/b6.sh`
- **Environment:** disposable Postgres DB `rag_verify_b6` (dropped after) +
  throwaway app container (compose app image + repo bind mount) on
  `127.0.0.1:8087`, `RUN_MIGRATIONS_ON_STARTUP=true`, `COOKIE_SECURE=false`,
  dev login limit raised, **`PINECONE_NAMESPACE=multi-user-test`** — every
  Pinecone write in the script targets the dedicated test namespace. The
  legacy namespace (`enterprise-agentic-rag`) was only ever READ via
  `describe_index_stats` (27 vectors before == 27 after).
- **Real model usage:** ZERO real LLM calls (B6 has no chat path); the two
  small document uploads embed into the test namespace only.
- **Result (final): PASS=33 FAIL=0 SKIP=0** — full raw output below.

## One wrong script assertion found by the first run (fixed, not silenced)

First run: 30 PASS / 1 FAIL — "temp password logs in" expected 200 but the
app answered **401**. The script was wrong, not the app: within CHECK 4 the
admin had just DISABLED the user, and per design (§A5) admin reset-password
issues a temp password + revokes sessions — it does NOT reactivate a
suspended account (enable is a separate explicit `PATCH is_active:true`).
`app/services/users.py::admin_reset_password` deliberately never touches
`is_active`; the permanent test `test_reset_password_issues_temp_and_
revokes_sessions` pins reset-on-ACTIVE-user → login 200, so no app change
was warranted. The script was strengthened to pin the intended semantics:
reset on a disabled user → login still **401** → admin re-enables → login
**200** with `must_change_password:true`. This turns a wrong expectation
into an explicit guarantee that a password reset can never silently
un-suspend an account.

## Coverage map (CHECK → what it proves live)

1. Create user → temp password + `must_change_password:true`; gated user
   blocked from admin routes (403) and conversations
   (`password_change_required`); after the forced password change the user
   can use the app.
2. Anonymous → 401 / plain user → 403 on admin routes; no
   admin chat-reading route exists (404/405).
3. Last-admin guard on EVERY path incl. self: demote-self 409,
   disable-self 409, delete-self 409 (admin route), self-delete via
   `/api/me` 409.
4. Disable → victim session dies immediately (401); reset-password → temp
   flag; disabled account refuses the temp password (401) until an admin
   explicitly re-enables → 200 + forced-change flag (fixed assertion).
5. Documents: org upload over an admin session, metadata listing shows the
   org doc, delete-ANY → 200 (C3), unknown id → 404, and the
   test-namespace vector count returned to 0 after the delete (vectors-first
   §A9.1) while the legacy namespace stayed at 27.
6. Purge-private-documents: plain user on self → 403 (admin-only route),
   admin → 200; `/api/me` without confirm → 422, with confirm → 200;
   deleted user login → 401; the `user_deleted` audit event is anonymized
   (`actor_user_id IS NULL`, `details.self=true` — the FK cannot reference
   the deleted actor).

## Post-run cleanup verified (2026-11-12)

`multi-user-test` = **0** vectors (script cleanup ran), legacy namespace =
**27** (untouched), no `rag-verify-*` containers, no `rag_verify_*`
scratch databases.

## Raw output (final green run)

```
==============================================================
B6 LIVE VERIFICATION — 2026-11-12 06:57:19Z — disposable db rag_verify_b6 — namespace multi-user-test
==============================================================
PASS  disposable app healthy  (expected=200 actual=200)
PASS  bootstrap-admin  (found: created)
PASS  admin login works  (expected=200 actual=200)
---- CHECK 1: create user — temp password + forced-change gate
PASS  user created with must_change_password  (found: "must_change_password":true)
PASS  gated user blocked from admin route (403)  (expected=403 actual=403)
PASS  gated user blocked from conversations  (found: password_change_required)
PASS  after password change the user can use the app  (expected=200 actual=200)
---- CHECK 2: 401 anonymous / 403 plain user on admin routes
PASS  anonymous -> 401  (expected=401 actual=401)
PASS  plain user -> 403  (expected=403 actual=403)
PASS  admin chat-reading route does not exist (404/405)  (expected=NOTEXIST actual=NOTEXIST)
---- CHECK 3: last-admin guard
PASS  sole admin demotes self -> 409  (expected=409 actual=409)
PASS  sole admin disables self -> 409  (expected=409 actual=409)
PASS  sole admin deletes self -> 409  (expected=409 actual=409)
PASS  sole admin self-deletes via /api/me -> 409  (expected=409 actual=409)
---- CHECK 4: disable + reset-password
PASS  disable user -> 200  (expected=200 actual=200)
PASS  disabled user session dies immediately -> 401  (expected=401 actual=401)
PASS  reset-password -> temp flag  (found: must_change_password)
PASS  temp password does NOT log in while account still disabled -> 401  (expected=401 actual=401)
PASS  admin re-enables the user -> 200  (expected=200 actual=200)
PASS  temp password logs in after explicit re-enable  (found: "must_change_password":true)
---- CHECK 5: admin documents
PASS  org upload over admin session  (found: "document_id")
PASS  org doc appears in the listing  (found: b6-org-note.txt)
      namespace stats before: {"enterprise-agentic-rag": 27, "multi-user-test": 1, "rag-v2": 4}
PASS  admin deletes ANY document (org) -> 200  (expected=200 actual=200)
PASS  unknown document id -> 404  (expected=404 actual=404)
PASS  test-namespace vector count dropped to 0 after delete  (expected=0 actual=0)
PASS  legacy namespace untouched  (expected="enterprise-agentic-rag": 27 actual="enterprise-agentic-rag": 27)
---- CHECK 6: purge + self-delete (a fresh user — CHECK 4's is disabled by design)
PASS  self-delete user created  (found: "must_change_password":true)
PASS  purge-private-documents by plain user on self -> 403  (expected=403 actual=403)
PASS  purge-private-documents as admin (0 docs) -> 200  (expected=200 actual=200)
PASS  self-delete without confirm -> 422  (expected=422 actual=422)
PASS  self-delete with confirm -> 200  (expected=200 actual=200)
PASS  deleted user login -> 401  (expected=401 actual=401)
PASS  user_deleted audit event anonymized (actor NULL)  (expected=1 actual=1)
==============================================================
RESULT: PASS=33 FAIL=0 SKIP=0  (2026-11-12 06:57:57Z)
==============================================================
```
