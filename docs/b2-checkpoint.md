# B2 checkpoint — verification recipe

**Status: B2 delivered — STOP for Farhad's review (§A12).** Everything below
is copy-paste ready. Design doc §A12 row B2: users+sessions tables/migrations ·
argon2id + `bootstrap-admin` · login/logout/me + CSRF · (username, IP)
exponential throttle + per-IP login limit · recovery CLIs · auth dependency
(session lookup, disabled-user check) + last-admin guard helpers.

Commits: schema+fixtures `0c4231d` → passwords `3fa1cb6` → sessions `a1374a7`
→ throttle `5687491` → users+CLIs `b94d2c0` → auth API `3223e39` → docs.

## Bring-up

```bash
docker compose --profile dev up -d app        # starts db (health-gated) + app
docker compose logs app | grep -i upgrade     # first start ran alembic 0001→0002
curl -s http://127.0.0.1:8081/api/health
# {"status":"ok","service":"Enterprise Agentic RAG"}
```

The app publishes **127.0.0.1:8081 only** (loopback measured fast, no LAN
exposure); the DB has no host port. Tests: `docker compose --profile test
run --rm test` (128 green at checkpoint time).

## 1. First-admin bootstrap (interactive; refuses if any admin exists)

```bash
docker compose run --rm app python -m app.cli bootstrap-admin
# Username: boss
# Password: <min 12 chars, shown nowhere>          (prompt hides input, asks twice)
# → Admin 'boss' created (id <uuid>)
```

Re-run the exact command — it must refuse: `An admin already exists`.

## 2. Login — cookie flags + Cache-Control (curl -i shows headers)

```bash
curl -si -c /tmp/b2-jar.txt -X POST http://127.0.0.1:8081/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"boss","password":"<the password you chose>"}'
```

Expected in the response:

- `Set-Cookie: sid=…; HttpOnly; Max-Age=2592000; Path=/; SameSite=lax`
  (`Secure` absent because dev runs plain http over loopback — flip
  `COOKIE_SECURE=true` in `.env` to see it; always on behind TLS)
- `Set-Cookie: csrf=…; SameSite=lax; Path=/` — **no** HttpOnly (the UI's JS
  reads it for double-submit)
- `Cache-Control: no-store` — on login, me, password and logout alike
- body: `{"user":{…,"must_change_password":false},"csrf_token":"…"}`

## 3. Wrong password → generic 401; 5 failures → lockout; unlock

```bash
for i in 1 2 3 4 5; do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8081/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username":"boss","password":"totally-wrong-passphrase"}'
done   # → 401 five times; same generic text as an unknown user
curl -s -i -X POST http://127.0.0.1:8081/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"boss","password":"<correct password>"}' | head -3
# → HTTP/1.1 429 Too Many Requests  +  Retry-After: ~900   (even with the
#    CORRECT password — the pair (boss, your IP) is locked for 15 min)
curl -s -X POST http://127.0.0.1:8081/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"nobody-here","password":"whatever"}' -o /dev/null -w '%{http_code}\n'
# → 401 (a different username from your IP is NOT throttled)
docker compose run --rm app python -m app.cli unlock boss
# → Cleared 1 throttle record(s) for 'boss'
# now the correct password logs in again (step 2)
```

## 4. CSRF double-submit rejection

```bash
curl -s -b /tmp/b2-jar.txt -o /dev/null -w '%{http_code} ' -X POST http://127.0.0.1:8081/api/auth/logout
curl -s -b /tmp/b2-jar.txt -X POST http://127.0.0.1:8081/api/auth/logout; echo
# → 403 {"code":"csrf_failed"}        (no header)
CSRF=$(grep csrf /tmp/b2-jar.txt | awk '{print $NF}')
curl -s -b /tmp/b2-jar.txt -o /dev/null -w '%{http_code}\n' \
  -X POST http://127.0.0.1:8081/api/auth/logout -H "X-CSRF-Token: forged"
# → 403                               (header ≠ cookie)
curl -s -b /tmp/b2-jar.txt -o /dev/null -w '%{http_code}\n' \
  -X POST http://127.0.0.1:8081/api/auth/logout -H "X-CSRF-Token: $CSRF"
# → 200                               (match → session revoked server-side)
curl -s -b /tmp/b2-jar.txt -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/api/me
# → 401  — replayed cookie is dead (revocation is server-side, not just a
#          cleared jar; re-login for the next steps)
```

## 5. Session check

```bash
curl -s -b /tmp/b2-jar.txt http://127.0.0.1:8081/api/me
# {"user":{"id":…,"username":"boss","display_name":null,"role":"admin",
#          "must_change_password":false}}
curl -si -b /tmp/b2-jar.txt http://127.0.0.1:8081/api/me | grep -i cache-control
# Cache-Control: no-store
```

## 6. Disabled user dies immediately (old cookie + login)

```bash
# log in again first so the jar holds a live session (steps 2/4)
docker compose exec db psql -U rag -d rag -c \
  "UPDATE users SET is_active=false WHERE username='boss';"
curl -s -b /tmp/b2-jar.txt -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/api/me
# → 401 (the still-live cookie dies on the very next request)
curl -s -X POST http://127.0.0.1:8081/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"boss","password":"<correct password>"}' ; echo
# → 401 {"detail":"Invalid username or password"}  (generic — no disabled hint)
docker compose exec db psql -U rag -d rag -c \
  "UPDATE users SET is_active=true WHERE username='boss';"
```

## 7. Forced password change (reset-password CLI → temp login → change)

```bash
docker compose run --rm app python -m app.cli reset-password boss
# New temporary password: <12+ chars, confirmed, shown nowhere>
curl -s -c /tmp/b2-jar.txt -X POST http://127.0.0.1:8081/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"boss","password":"<temp password>"}' | grep -o '"must_change_password":true'
curl -s -b /tmp/b2-jar.txt http://127.0.0.1:8081/api/me | grep -o '"must_change_password":true'
CSRF=$(grep csrf /tmp/b2-jar.txt | awk '{print $NF}')
curl -s -b /tmp/b2-jar.txt -X POST http://127.0.0.1:8081/api/auth/password \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"old_password":"<temp password>","new_password":"<your new one, 12+ chars>"}'
# → {"status":"password_changed","user":{…,"must_change_password":false}}
```

Note: B2 ships only the three change-exempt endpoints (me / password /
logout), so the 403 `password_change_required` on OTHER endpoints can't be
demonstrated over HTTP yet — it is unit-tested against a probe route
(`tests/test_auth_api.py::test_forced_change_gate_and_full_flow`); the first
non-exempt session router lands in B3, full admin-created temp users in B6.

Also verify the old sessions died: the pre-reset cookie jar must now 401 on
`/api/me` (reset-password revokes sessions), while the post-change session
survives a self-change.

## 8. Last-admin guard (helpers) + audit trail

The 409-enforcing endpoints arrive with B6 admin users; B2 delivers and
unit-tests the helpers (`users.would_leave_zero_admins`,
`users.active_admin_count` — `tests/test_auth_cli.py`). The B2-visible
guard is bootstrap's refusal (step 1 re-run). Operator check:

```bash
docker compose exec db psql -U rag -d rag -c \
  "SELECT count(*) AS active_admins FROM users WHERE role='admin' AND is_active;"
```

Audit trail of everything you just did (privacy rules §A6: no usernames for
unknown users, actor ids only for known ones, never a password):

```bash
docker compose exec db psql -U rag -d rag -c \
  "SELECT event_type, outcome, count(*) FROM audit_events GROUP BY 1, 2 ORDER BY 1;"
```

## 9. Tests green

```bash
docker compose --profile test run --rm test
# 128 passed (real Postgres; LLM/Tavily/Pinecone mocked offline)
```
