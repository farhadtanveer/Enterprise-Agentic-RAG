#!/usr/bin/env bash
# =============================================================================
# scripts/verify/b2.sh — B2 live verification (Part 1 of the review mandate)
#
# Disposable by design: spins up a THROWAWAY app container + scratch database
# (rag_verify_b2) on 127.0.0.1:8082, runs every B2/B2-review check over real
# HTTP and real Postgres, then cleans everything up. The dev database and its
# data are never touched. Real LLM calls: exactly ONE (legacy /api/chat).
#
# Re-run:  bash scripts/verify/b2.sh
# Requires: docker (compose stack up), curl, awk. Run from anywhere.
# =============================================================================
set -u
cd "$(dirname "$0")/../.."   # repo root

NET=enterprise-agentic-rag_default
DB=enterprise-agentic-rag-db-1
APP=rag-verify-b2                 # main throwaway app (COOKIE_SECURE=false)
APP_HOST=rag-secure-b2            # COOKIE_SECURE=true variant (__Host- check)
APP_SPRAY=rag-spray-b2            # per-IP spray variant (limit=5)
DBV=rag_verify_b2
PORT=8082; PORT_SEC=8083; PORT_HOST=8084
BASE=http://127.0.0.1:$PORT
TMP=$(mktemp -d)
PASS=0; FAIL=0; SKIPPED=0

redact() { sed -E 's/(sid|csrf|__Host-sid|__Host-csrf)=[^;]*/\1=<redacted>/g'; }

ck() {  # ck <desc> <expected> <actual>
  local desc="$1" exp="$2" act="$3"
  if [ "$exp" = "$act" ]; then
    echo "PASS  $desc  (expected=$exp actual=$act)"; PASS=$((PASS+1))
  else
    echo "FAIL  $desc  (expected=$exp actual=$act)"; FAIL=$((FAIL+1))
  fi
}
ckc() { # ck_contains <desc> <needle> <haystack-file-or-string>
  local desc="$1" needle="$2" hay="$3"
  # F7: empty-guard (same as b3/b4) — an empty needle makes grep -qi ""
  # match ANYTHING (vacuous pass), and an empty haystack means an upstream
  # step failed. Both FAIL loudly instead of passing silently.
  if [ -z "$needle" ] || [ -z "$hay" ]; then
    echo "FAIL  $desc  (EMPTY needle/haystack — upstream step failed)"; FAIL=$((FAIL+1)); return
  fi
  if printf '%s' "$hay" | grep -qi "$needle"; then
    echo "PASS  $desc  (found: $needle)"; PASS=$((PASS+1))
  else
    echo "FAIL  $desc  (missing: $needle)"; FAIL=$((FAIL+1))
  fi
}
skip() { echo "SKIP  $1 ($2)"; SKIPPED=$((SKIPPED+1)); }
note() { echo "---- $*"; }
psql() { docker exec $DB psql -U rag -d $DBV -Atc "$1"; }
cli()  { printf "$2" | docker exec -i $APP python -m app.cli "$1" ${3:-} 2>&1 | tail -2; }

cleanup() {
  docker rm -f $APP $APP_HOST $APP_SPRAY >/dev/null 2>&1
  docker exec $DB dropdb -U rag --if-exists $DBV >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "=============================================================="
echo "B2 LIVE VERIFICATION — $(date -u '+%Y-%m-%d %H:%M:%SZ') — disposable db $DBV"
echo "=============================================================="

# --- bring up the disposable environment -------------------------------------
docker exec $DB dropdb -U rag --if-exists $DBV >/dev/null 2>&1
docker exec $DB createdb -U rag $DBV >/dev/null
REPO_WIN=$(cygpath -w "$PWD")
docker run -d --name $APP --network $NET -p 127.0.0.1:$PORT:8080 \
  -v "${REPO_WIN}:/app" \
  -e DATABASE_URL="postgresql+psycopg://rag:rag@db:5432/$DBV" \
  -e RUN_MIGRATIONS_ON_STARTUP=true \
  -e COOKIE_SECURE=false \
  -e APP_ENV=development \
  -e ADMIN_API_KEY=verify-admin-key \
  -e LOGIN_RATE_LIMIT_PER_MINUTE=1000 \
  -e CHAT_RATE_LIMIT_PER_MINUTE=1000 \
  enterprise-agentic-rag-app:latest \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 >/dev/null

for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)" = "200" ] && break
  sleep 2
done
ck "disposable app healthy on $BASE" "200" "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)"

# ===== 1. bootstrap-admin: first run succeeds, second refuses; recovery CLIs ==
note "CHECK 1: bootstrap-admin / reset-password / unlock (password via stdin, never argv)"
OUT=$(cli bootstrap-admin 'verifyboss\nVerify-Boss-Passphrase-1\nVerify-Boss-Passphrase-1\n')
ckc "bootstrap-admin first run creates the admin" "created (id" "$OUT"
OUT2=$(cli bootstrap-admin 'x\nx-passphrase-longer-12\nx-passphrase-longer-12\n')
ckc "bootstrap-admin second run refuses" "already exists" "$OUT2"

# ===== 2. login: cookie flags + Cache-Control: no-store =======================
note "CHECK 2: login Set-Cookie flags + no-store"
LOGIN=$(curl -si -c "$TMP/jar" -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"verifyboss","password":"Verify-Boss-Passphrase-1"}' | redact)
SC=$(printf '%s' "$LOGIN" | grep -i '^set-cookie: sid=' )
ckc "sid cookie HttpOnly"                    "HttpOnly" "$SC"
ckc "sid cookie SameSite=lax"                "SameSite=lax" "$SC"
ckc "sid cookie Path=/"                      "Path=/" "$SC"
SC_CS=$(printf '%s' "$LOGIN" | grep -i '^set-cookie: csrf=')
ckc "csrf cookie present"                    "csrf=" "$SC_CS"
if printf '%s' "$SC_CS" | grep -qi 'httponly'; then ck "csrf cookie NOT HttpOnly (JS-readable)" "no-httponly" "HAS-httponly"
else ck "csrf cookie NOT HttpOnly (JS-readable)" "no-httponly" "no-httponly"; fi
if printf '%s' "$SC" | grep -qi 'secure'; then ck "sid Secure ABSENT on plain-http dev" "absent" "present"
else ck "sid Secure ABSENT on plain-http dev" "absent" "absent"; fi
ck  "login Cache-Control: no-store" "cache-control: no-store" \
    "$(printf '%s' "$LOGIN" | grep -i '^cache-control' | tr -d '\r' | tr 'A-Z' 'a-z')"
CSRF=$(awk '/csrf/{print $NF}' "$TMP/jar" | tail -1)
ME=$(curl -s -b "$TMP/jar" $BASE/api/me)
ckc "/api/me returns the user" '"username":"verifyboss"' "$ME"
ck  "/api/me Cache-Control: no-store" "cache-control: no-store" \
    "$(curl -si -b "$TMP/jar" $BASE/api/me | grep -i '^cache-control' | tr -d '\r' | tr 'A-Z' 'a-z')"

# ===== 3. unknown user vs known user + wrong password: identical responses ====
note "CHECK 3: unknown user vs known user/wrong password — identical status+body+headers"
curl -s -i -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"ghost-user-xyz","password":"whatever-long-enough"}' | \
  grep -vi '^date:' | tr -d '\r' > "$TMP/resp_unknown.txt"
curl -s -i -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"verifyboss","password":"wrong-password-but-long"}' | \
  grep -vi '^date:' | tr -d '\r' > "$TMP/resp_knownwrong.txt"
if diff -q "$TMP/resp_unknown.txt" "$TMP/resp_knownwrong.txt" >/dev/null; then
  ck "responses byte-identical (headers+body, Date excluded)" "identical" "identical"
else
  ck "responses byte-identical (headers+body, Date excluded)" "identical" "DIFFER:"$(diff "$TMP/resp_unknown.txt" "$TMP/resp_knownwrong.txt" | head -3)
fi
ck  "generic 401 status" "401" "$(head -1 "$TMP/resp_unknown.txt" | grep -o '401')"

# ===== 4. timing parity: 20 attempts each, median + p95 =======================
note "CHECK 4: timing parity (20 unknown vs 20 known-wrong)"
for i in $(seq 1 20); do
  curl -s -o /dev/null -w '%{time_total}\n' -X POST $BASE/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username":"ghost-user-xyz","password":"whatever-long-enough"}' >> "$TMP/t_unknown.txt"
  curl -s -o /dev/null -w '%{time_total}\n' -X POST $BASE/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username":"verifyboss","password":"wrong-password-but-long"}' >> "$TMP/t_known.txt"
done
stats() { sort -n "$1" | awk '{a[NR]=$1} END{if(NR%2==0) m=(a[NR/2]+a[NR/2+1])/2; else m=a[(NR+1)/2]; printf "median=%.3fs p95=%.3fs", m, a[int(NR*0.95+0.999)]}'; }
MED_U=$(stats "$TMP/t_unknown.txt"); MED_K=$(stats "$TMP/t_known.txt")
echo "      unknown user:      $MED_U"
echo "      known-user wrong:  $MED_K"
MED_U_V=$(stats "$TMP/t_unknown.txt" | grep -o 'median=[0-9.]*' | cut -d= -f2)
MED_K_V=$(stats "$TMP/t_known.txt" | grep -o 'median=[0-9.]*' | cut -d= -f2)
JUDGE=$(awk -v u="$MED_U_V" -v k="$MED_K_V" 'BEGIN{d=(u>k?u/k:k/u); print (d<1.5)?"SMALL":"LARGE"}')
ck  "timing difference is small (<1.5x)" "SMALL" "$JUDGE"

# ===== 5. lockout: 5 failures -> 429 + Retry-After; unknown-user parity =======
note "CHECK 5: (username, IP) lockout + unknown-username parity + unlock"
for i in $(seq 1 5); do
  curl -s -o /dev/null -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
    -d '{"username":"verifyboss","password":"wrong-password-but-long"}'
done
LOCK=$(curl -si -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"verifyboss","password":"Verify-Boss-Passphrase-1"}' | redact)
ckc "correct password after 5 failures -> 429" "429" "$(printf '%s' "$LOCK" | head -1)"
ckc "Retry-After present" "Retry-After:" "$LOCK"
for i in $(seq 1 5); do
  curl -s -o /dev/null -w '%{http_code} ' -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
    -d '{"username":"never-a-user-abc","password":"wrong-password-but-long"}'
done
PAR=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"never-a-user-abc","password":"wrong-password-but-long"}')
ck "unknown username 6th attempt -> 429 (parity)" "429" "$PAR"
UNLOCK=$(printf 'x\n' | docker exec -i $APP python -m app.cli unlock verifyboss 2>&1 | tail -1)
ckc "unlock clears throttle records" "Cleared" "$UNLOCK"
RELOGIN=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"verifyboss","password":"Verify-Boss-Passphrase-1"}')
ck "unlock restores access (200 on correct login)" "200" "$RELOGIN"
# fresh session for later checks (jar may hold revoked session from earlier flows)
curl -s -c "$TMP/jar" -o /dev/null -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"verifyboss","password":"Verify-Boss-Passphrase-1"}'
CSRF=$(awk '/csrf/{print $NF}' "$TMP/jar" | tail -1)

# ===== 6. CSRF: missing / wrong / correct / other-session token ================
note "CHECK 6: CSRF double-submit + session binding"
NO_H=$(curl -s -b "$TMP/jar" -X POST $BASE/api/auth/logout)
ckc "no header -> 403 csrf_failed" '"code":"csrf_failed"' "$NO_H"
NO_H_CODE=$(curl -s -b "$TMP/jar" -o /dev/null -w '%{http_code}' -X POST $BASE/api/auth/logout)
ck  "no header status 403" "403" "$NO_H_CODE"
WRONG=$(curl -s -b "$TMP/jar" -o /dev/null -w '%{http_code}' -X POST $BASE/api/auth/logout -H "X-CSRF-Token: forged")
ck  "wrong token 403" "403" "$WRONG"
# session-bound CSRF (1B-d): token from ANOTHER session must fail
curl -s -c "$TMP/jar2" -o /dev/null -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"verifyboss","password":"Verify-Boss-Passphrase-1"}'
CSRF2=$(awk '/csrf/{print $NF}' "$TMP/jar2" | tail -1)
XSESS=$(curl -s -b "$TMP/jar" -o /dev/null -w '%{http_code}' -X POST $BASE/api/auth/logout -H "X-CSRF-Token: $CSRF2")
ck  "token from another session -> 403" "403" "$XSESS"
OKCSRF=$(curl -s -b "$TMP/jar" -o /dev/null -w '%{http_code}' -X POST $BASE/api/auth/logout -H "X-CSRF-Token: $CSRF")
ck  "correct token -> 200 (and revokes server-side)" "200" "$OKCSRF"

# ===== 7. logout invalidates server-side: replay old cookie -> 401 =============
note "CHECK 7: logout revokes server-side"
REPLAY=$(curl -s -b "$TMP/jar" -o /dev/null -w '%{http_code}' $BASE/api/me)
ck "replayed cookie after logout -> 401" "401" "$REPLAY"

# ===== 8. password change: sessions + policy rules =============================
note "CHECK 8: password change semantics + policy"
# jar2 still holds a live 2nd session; jar1's session is revoked — relogin
curl -s -c "$TMP/jar" -o /dev/null -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"verifyboss","password":"Verify-Boss-Passphrase-1"}'
CSRF=$(awk '/csrf/{print $NF}' "$TMP/jar" | tail -1)
PC=$(curl -s -b "$TMP/jar" -X POST $BASE/api/auth/password -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"old_password":"Verify-Boss-Passphrase-1","new_password":"Brand-New-Passphrase-2"}')
ckc "password change succeeds" "password_changed" "$PC"
OTHER=$(curl -s -b "$TMP/jar2" -o /dev/null -w '%{http_code}' $BASE/api/me)
ck "other session -> 401 after change" "401" "$OTHER"
CUR=$(curl -s -b "$TMP/jar" -o /dev/null -w '%{http_code}' $BASE/api/me)
ck "current session stays valid" "200" "$CUR"
OLDPW=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"verifyboss","password":"Verify-Boss-Passphrase-1"}')
ck "old password rejected" "401" "$OLDPW"
R11=$(curl -s -b "$TMP/jar" -X POST $BASE/api/auth/password -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"old_password":"Brand-New-Passphrase-2","new_password":"short11char"}' -o /dev/null -w '%{http_code}')
ck "11-char password rejected (422)" "422" "$R11"
R129=$(curl -s -b "$TMP/jar" -X POST $BASE/api/auth/password -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d "{\"old_password\":\"Brand-New-Passphrase-2\",\"new_password\":\"$(printf 'a%.0s' $(seq 1 129))\"}" -o /dev/null -w '%{http_code}')
ck "129-char password rejected (422)" "422" "$R129"
RUSER=$(curl -s -b "$TMP/jar" -X POST $BASE/api/auth/password -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"old_password":"Brand-New-Passphrase-2","new_password":"verifyboss"}' -o /dev/null -w '%{http_code}')
ck "username as password rejected (422)" "422" "$RUSER"
RUC=$(curl -s -b "$TMP/jar" -X POST $BASE/api/auth/password -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"old_password":"Brand-New-Passphrase-2","new_password":"VerifyBoss"}' -o /dev/null -w '%{http_code}')
ck "username as password, case variant rejected" "422" "$RUC"
RWS=$(curl -s -b "$TMP/jar" -X POST $BASE/api/auth/password -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"old_password":"Brand-New-Passphrase-2","new_password":"  verifyboss  "}' -o /dev/null -w '%{http_code}')
ck "username as password, whitespace variant rejected" "422" "$RWS"
# restore a known password for later checks
curl -s -b "$TMP/jar" -o /dev/null -X POST $BASE/api/auth/password -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"old_password":"Brand-New-Passphrase-2","new_password":"Verify-Boss-Passphrase-1"}'

# ===== 9. disabled user dies immediately =======================================
note "CHECK 9: disabled user — session + login"
CREATE_OUT=$(docker exec -i $APP python -c "
from app.core.db import get_sessionmaker
from app.services import users
db = get_sessionmaker()()
u = users.create_user(db, 'checkvictim', password='Victim-Passphrase-9')
db.commit(); db.close()
print('created', u.username)
" 2>&1 | tail -1)
ckc "victim user created (check_ prefix)" "created checkvictim" "$CREATE_OUT"
curl -s -c "$TMP/jarv" -o /dev/null -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"checkvictim","password":"Victim-Passphrase-9"}'
ALIVE=$(curl -s -b "$TMP/jarv" -o /dev/null -w '%{http_code}' $BASE/api/me)
ck "victim session works before disable" "200" "$ALIVE"
psql "UPDATE users SET is_active=false WHERE username='checkvictim';" >/dev/null
DEAD=$(curl -s -b "$TMP/jarv" -o /dev/null -w '%{http_code}' $BASE/api/me)
ck "disabled user session dies on next request" "401" "$DEAD"
VDIS=$(curl -s -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"checkvictim","password":"Victim-Passphrase-9"}')
ckc "disabled user login -> generic 401 (no disabled hint)" '"detail":"Invalid username or password"' "$VDIS"
psql "UPDATE users SET is_active=true WHERE username='checkvictim';" >/dev/null

# ===== 10. forced password change gate =========================================
note "CHECK 10: forced-change gate on a temporary password"
RESET_OUT=$(printf 'Temp-Passphrase-T1\nTemp-Passphrase-T1\n' | docker exec -i $APP python -m app.cli reset-password checkvictim 2>&1 | tail -2)
ckc "reset-password issues temp password + revokes sessions" "revoked" "$RESET_OUT"
curl -s -c "$TMP/jart" -o /dev/null -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"checkvictim","password":"Temp-Passphrase-T1"}'
MCP=$(curl -s -b "$TMP/jart" $BASE/api/me | grep -o '"must_change_password":true')
ck "temp login flags must_change_password" "true" "$(echo $MCP | grep -o true)"
CSRT=$(awk '/csrf/{print $NF}' "$TMP/jart" | tail -1)
BLOCKED=$(curl -s -b "$TMP/jart" -X POST $BASE/api/conversations -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRT" -d '{}' -o /dev/null -w '%{http_code}')
ck "non-exempt route blocked while temp" "403" "$BLOCKED"
BLOCKED_BODY=$(curl -s -b "$TMP/jart" -X POST $BASE/api/conversations -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRT" -d '{}')
ckc "gate body is password_change_required" '"code":"password_change_required"' "$BLOCKED_BODY"
EX_ME=$(curl -s -b "$TMP/jart" -o /dev/null -w '%{http_code}' $BASE/api/me)
ck "exempt: GET /api/me reachable" "200" "$EX_ME"
EX_PC=$(curl -s -b "$TMP/jart" -o /dev/null -w '%{http_code}' -X POST $BASE/api/auth/password -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRT" \
  -d '{"old_password":"Temp-Passphrase-T1","new_password":"Victim-Passphrase-9"}')
ck "exempt: password change reachable" "200" "$EX_PC"
EX_LO=$(curl -s -b "$TMP/jart" -o /dev/null -w '%{http_code}' -X POST $BASE/api/auth/logout -H "X-CSRF-Token: $CSRT")
ck "exempt: logout reachable" "200" "$EX_LO"

# ===== 11. no content leakage: markers =========================================
note "CHECK 11: marker strings never reach logs / audit_events / throttle rows"
MU="LEAKMARKERUSER77"; MP="LEAKMARKERPASS88"
for i in 1 2 3; do
  curl -s -o /dev/null -X POST $BASE/api/auth/login -H "Content-Type: application/json" \
    -d "{\"username\":\"$MU\",\"password\":\"$MP\"}"
done
LOGL=$(docker logs $APP 2>&1 | grep -c "LEAKMARKER" || true)
ck "marker absent from app logs" "0" "$LOGL"
AUD=$(psql "SELECT count(*) FROM audit_events ae WHERE to_jsonb(ae)::text ILIKE '%LEAKMARKER%';")
ck "marker absent from audit_events" "0" "$AUD"
THR=$(psql "SELECT count(*) FROM login_throttle lt WHERE to_jsonb(lt)::text ILIKE '%LEAKMARKER%';")
ck "marker absent from login_throttle rows" "0" "$THR"
USR=$(psql "SELECT count(*) FROM users u WHERE to_jsonb(u)::text ILIKE '%LEAKMARKER%';")
ck "marker absent from users table" "0" "$USR"

# ===== 12. session token storage ===============================================
note "CHECK 12: sessions store only sha256 token hashes"
RAW=$(awk '/sid/{print $NF}' "$TMP/jar2" | tail -1)
BADFMT=$(psql "SELECT count(*) FROM sessions WHERE token_hash !~ '^[0-9a-f]{64}$';")
ck "every token_hash is 64-hex" "0" "$BADFMT"
RAWDB=$(psql "SELECT count(*) FROM sessions WHERE token_hash ILIKE '%${RAW:0:12}%';")
ck "raw cookie value nowhere in sessions" "0" "$RAWDB"
RAWANY=$(psql "SELECT count(*) FROM sessions WHERE to_jsonb(sessions)::text ILIKE '%${RAW:0:12}%';")
ck "raw cookie value nowhere in sessions (any column)" "0" "$RAWANY"

# ===== 13. legacy endpoints =====================================================
note "CHECK 13: legacy /api/chat + /api/ingest unchanged in shape"
L422=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/chat -H "Content-Type: application/json" -d '{"question":""}')
ck "legacy chat validates body (422)" "422" "$L422"
ING401=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/ingest -H "X-Admin-Key: wrong" -F "file=@README.md;filename=x.txt")
ck "legacy ingest wrong admin key -> 401" "401" "$ING401"
ING400=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/ingest -H "X-Admin-Key: verify-admin-key" -F "file=@README.md;filename=x.exe")
ck "legacy ingest unsupported type -> 400" "400" "$ING400"
if [ -n "${OPENAI_API_KEY:-}" ] && grep -q '^PINECONE_API_KEY=..*' .env; then
  LCHAT=$(curl -s --max-time 120 -X POST $BASE/api/chat -H "Content-Type: application/json" \
    -d '{"question":"How do I connect to the company VPN?"}' -o /dev/null -w '%{http_code}')
  ck "legacy chat answers end-to-end (1 real LLM call)" "200" "$LCHAT"
else
  skip "legacy chat end-to-end (real call)" "PINECONE_API_KEY empty in .env — retrieval cannot connect; re-run after refill"
fi

# ===== 14. container: appuser, no pytest in default build, HEALTHCHECK ==========
note "CHECK 14: container posture"
WHO=$(docker exec $APP whoami 2>/dev/null || docker exec $APP id -un)
ck "container runs as appuser" "appuser" "$WHO"
PYT=$(docker run --rm enterprise-agentic-rag-app:latest pip show pytest 2>/dev/null | grep -c "^Name: pytest" || true)
ck "default-build image has no pytest" "0" "$PYT"
HC=$(docker inspect --format '{{.Config.Healthcheck.Test}}' enterprise-agentic-rag-app:latest | grep -c api/health || true)
ck "image declares HEALTHCHECK on /api/health" "1" "$HC"
ST=$(docker inspect --format '{{.State.Health.Status}}' $APP)
ck "container reports healthy" "healthy" "$ST"

# ===== 1B-a: production settings validator (live refusal) ======================
note "CHECK 1B-a: APP_ENV=production with dev defaults refuses to start"
docker run --rm --name rag-prod-refusal --network $NET \
  -v "${REPO_WIN}:/app" \
  -e DATABASE_URL="postgresql+psycopg://rag:rag@db:5432/$DBV" \
  -e APP_ENV=production \
  -e COOKIE_SECURE=false \
  -e ADMIN_API_KEY=real-prod-key \
  enterprise-agentic-rag-app:latest \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 > "$TMP/prod_refusal.log" 2>&1
RC=$?
ckc "refusal message lists the dev settings" "Refusing to start" "$(cat "$TMP/prod_refusal.log")"
ckc "refusal names COOKIE_SECURE" "COOKIE_SECURE=false" "$(cat "$TMP/prod_refusal.log")"
ckc "refusal names the throttle pepper" "LOGIN_THROTTLE_PEPPER" "$(cat "$TMP/prod_refusal.log")"
ckc "refusal names dev DB credentials" "dev default credentials" "$(cat "$TMP/prod_refusal.log")"
if [ "$RC" -ne 0 ]; then ck "process exits non-zero" "NONZERO" "NONZERO"
else ck "process exits non-zero" "NONZERO" "ZERO"; fi

# ===== 1B-c: per-IP login limit (password spraying) ============================
note "CHECK 1B-c: per-IP login attempt limit (spray many usernames, one IP)"
docker run -d --name $APP_SPRAY --network $NET -p 127.0.0.1:$PORT_SEC:8080 \
  -v "${REPO_WIN}:/app" \
  -e DATABASE_URL="postgresql+psycopg://rag:rag@db:5432/$DBV" \
  -e RUN_MIGRATIONS_ON_STARTUP=true -e COOKIE_SECURE=false -e APP_ENV=development \
  -e LOGIN_RATE_LIMIT_PER_MINUTE=5 \
  enterprise-agentic-rag-app:latest \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 >/dev/null
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT_SEC/api/health)" = "200" ] && break; sleep 2
done
SPRAYS=""
for i in $(seq 1 8); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:$PORT_SEC/api/auth/login \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"check-spray-$i\",\"password\":\"wrong-password-long-enough\"}")
  SPRAYS="$SPRAYS $CODE"
done
echo "      spray sequence (8 distinct usernames, 1 IP):$SPRAYS"
LAST=$(echo $SPRAYS | awk '{print $NF}')
ck "8th distinct username from one IP -> 429" "429" "$LAST"

# ===== 1B-d: __Host- cookies when COOKIE_SECURE=true ===========================
note "CHECK 1B-d: __Host- cookie names when COOKIE_SECURE=true"
docker run -d --name $APP_HOST --network $NET -p 127.0.0.1:$PORT_HOST:8080 \
  -v "${REPO_WIN}:/app" \
  -e DATABASE_URL="postgresql+psycopg://rag:rag@db:5432/$DBV" \
  -e RUN_MIGRATIONS_ON_STARTUP=true -e COOKIE_SECURE=true -e APP_ENV=development \
  -e LOGIN_RATE_LIMIT_PER_MINUTE=1000 \
  enterprise-agentic-rag-app:latest \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 >/dev/null
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT_HOST/api/health)" = "200" ] && break; sleep 2
done
SECC=$(curl -si -X POST http://127.0.0.1:$PORT_HOST/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"verifyboss","password":"Verify-Boss-Passphrase-1"}' | grep -i '^set-cookie' | redact)
ckc "__Host-sid cookie used" "__Host-sid=" "$SECC"
ckc "__Host-sid carries Secure" "Secure" "$(printf '%s' "$SECC" | grep -i '__Host-sid')"
ckc "__Host-csrf cookie used" "__Host-csrf=" "$SECC"

# ===== 1B-e: CLIs never take a password via argv ================================
note "CHECK 1B-e: no CLI command reads a password from argv"
ARGV=$(grep -nE "click\.(argument|option).*password|password.*click\.(argument|option)" app/cli.py | grep -cv "prompt\|validate_password" || true)
ck "no argv password parameters in app/cli.py" "0" "$ARGV"
DOCS=$(grep -c "hide_input=True" app/cli.py)
if [ "$DOCS" -ge 1 ]; then ck "passwords read via hidden prompts (getpass-style)" "YES" "YES"
else ck "passwords read via hidden prompts" "YES" "NO"; fi

echo "=============================================================="
echo "RESULT: PASS=$PASS FAIL=$FAIL SKIP=$SKIPPED  ($(date -u '+%Y-%m-%d %H:%M:%SZ'))"
echo "=============================================================="
[ "$FAIL" -eq 0 ]
