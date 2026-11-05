# B2 live verification evidence (Part 1)

- **Commit:** `3098a32` (fix(b4): harden complete_delete …) — B2 stack itself spans `0c4231d`→`9420c6a`
- **Timestamp:** 2026-11-05 10:18:08Z → 10:19:42Z (UTC, from the run banner/result line)
- **Command:** `bash scripts/verify/b2.sh`
- **Environment:** disposable Postgres DB `rag_verify_b2` + throwaway app container
  (`enterprise-agentic-rag-app:latest` + repo bind mount) on `127.0.0.1:8082`
  (`LOGIN_RATE_LIMIT_PER_MINUTE=1000` so the (username, IP) throttle can be
  tested in isolation). Two more throwaway containers for 1B-c (spray limit=5,
  :8083) and 1B-d (`COOKIE_SECURE=true`, :8084). Everything torn down
  afterwards (containers removed, `rag_verify_b2` dropped). **The dev database
  was never touched.**
- **Result: PASS=71 FAIL=0 SKIP=1** — full script output below (untrimmed
  except empty lines; cookie values redacted by the script itself).

## Deviations / skips

| Check | Status | Reason |
|---|---|---|
| 13 — legacy `/api/chat` end-to-end (real graph call) | **SKIP** | `PINECONE_API_KEY` is empty in `.env` since the 2026-11-05 working-tree incident; retrieval cannot connect without it. The legacy surface's *shape* is proven (422/401/400 all PASS). Re-run `bash scripts/verify/b2.sh` after refilling the key to complete this one check. |

## Honest history of the three runs (rule 4: what failed first)

1. **Run 1 (10:07Z): 6 FAIL.** All were verifier bugs or my mislabeled inputs,
   not app bugs: case-sensitive `Cache-Control` compare (uvicorn sends
   lowercase header names — RFC-correct, app is fine); a "11-char" test string
   that was actually 12 chars and legitimately succeeded (then the following
   policy checks used a stale `old_password` → 400 "Incorrect old password");
   `Retry-After` "missing" because `head -8` truncated the header block; a
   bash pipeline quirk (`VAR=$(cmd) | tail` runs the assignment in a pipeline
   subshell → unbound under `set -u`).
2. **Run 2 (10:12Z): 9 FAIL.** Remaining verifier defects: header-name case
   again for `Retry-After:` (`grep` without `-i`; h11 lowercases header
   *names* but not cookie values — this also matched what run 1 saw); check 3
   diff polluted by the lowercase `date:` header (my `grep -v '^Date:'`
   missed it); `tail -1` grabbed the wrong CLI output line for
   reset-password; the 1B-a refusal container hit `validate_admin_key`
   *first* (`app/main.py:23`) because the verifier didn't set a real
   `ADMIN_API_KEY`, so `validate_production_settings` never ran; an integer
   compared as a string ("1" != ">=1").
3. **Run 3 (10:18Z): 0 FAIL.** No app code was changed between runs — only
   `scripts/verify/b2.sh`.

## Full script output (run 3)

```
==================================================================
B2 LIVE VERIFICATION — 2026-11-05 10:18:08Z — disposable db rag_verify_b2
==================================================================
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
      unknown user:      median=0.018s p95=0.141s
      known-user wrong:  median=0.014s p95=0.139s
PASS  timing difference is small (<1.5x)  (expected=SMALL actual=SMALL)
---- CHECK 5: (username, IP) lockout + unknown-username parity + unlock
PASS  correct password after 5 failures -> 429  (found: 429)
PASS  Retry-After present  (found: Retry-After:)
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
SKIP  legacy chat end-to-end (real call) (PINECONE_API_KEY empty in .env — retrieval cannot connect; re-run after refill)
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
---- CHECK 1B-d: __Host- cookies when COOKIE_SECURE=true
PASS  __Host-sid cookie used  (found: __Host-sid=)
PASS  __Host-sid carries Secure  (found: Secure)
PASS  __Host-csrf cookie used  (found: __Host-csrf=)
---- CHECK 1B-e: no CLI command reads a password from argv
PASS  no argv password parameters in app/cli.py  (expected=0 actual=0)
PASS  passwords read via hidden prompts (getpass-style)  (expected=YES actual=YES)
==================================================================
RESULT: PASS=71 FAIL=0 SKIP=1  (2026-11-05 10:19:42Z)
==================================================================
```

## Judgement on the timing check (mandate item 4)

Median 14 ms (known user, wrong password) vs 18 ms (unknown user), p95
139 ms vs 141 ms — both paths run one argon2id verify (real vs `DUMMY_HASH`,
`app/services/passwords.py:46-48`), and the ≤1.5× guard passed. The medians
sit ~4 ms apart with p95 within 2 ms — **timing parity holds; no user-
enumeration oracle by response time.** (Absolute values are low because the
verify container had warm CPU; what matters is the *delta* between the two
classes, measured interleaved 20×20.)

## Sample raw commands behind the checks (trimmed)

```bash
# login + cookie flags (values redacted by the script's sed)
curl -si -c "$TMP/jar" -X POST http://127.0.0.1:8082/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"verifyboss","password":"Verify-Boss-Passphrase-1"}'
# → HTTP/1.1 200; Set-Cookie: sid=<redacted>; HttpOnly; Max-Age=2592000;
#   Path=/; SameSite=lax   (Secure absent — COOKIE_SECURE=false dev)
# → Set-Cookie: csrf=<redacted>; SameSite=lax; Path=/  (no HttpOnly)
# → cache-control: no-store

# lockout parity
for i in 1..5: POST /api/auth/login {"username":"verifyboss","password":"wrong-password-but-long"} → 401×5
POST /api/auth/login (correct password) → HTTP/1.1 429 + retry-after: 897
for i in 1..6: POST /api/auth/login {"username":"never-a-user-abc",...} → 401×5, 429  # parity
python -m app.cli unlock verifyboss → Cleared 1 throttle record(s) for 'verifyboss'.

# marker scan (audit_events scanned row-by-row via to_jsonb, all columns)
psql -d rag_verify_b2 -Atc "SELECT count(*) FROM audit_events ae
  WHERE to_jsonb(ae)::text ILIKE '%LEAKMARKER%';"   # → 0

# session storage (raw token from the cookie jar, first 12 chars)
psql -d rag_verify_b2 -Atc "SELECT count(*) FROM sessions
  WHERE token_hash !~ '^[0-9a-f]{64}$';"            # → 0
psql -d rag_verify_b2 -Atc "SELECT count(*) FROM sessions
  WHERE to_jsonb(sessions)::text ILIKE '%<raw-prefix>%';"  # → 0
```
