#!/usr/bin/env bash
# =============================================================================
# scripts/verify/b6.sh — B6 live verification (admin surface + DELETE /api/me)
#
# Disposable by design: THROWAWAY app container + scratch database
# (rag_verify_b6, loopback :8087). ALL Pinecone work happens in the
# DEDICATED TEST NAMESPACE (multi-user-test); the legacy namespace is only
# read via describe_index_stats before/after. Every document the script
# creates is tracked and the cleanup trap deletes its vectors BY ID
# (list -> fetch -> metadata match -> delete(ids=...), never delete_all,
# never another namespace). Deletion proofs use EXACT vector ids — the
# script fetches the concrete ids of each document and asserts those ids
# are gone afterwards (never namespace stats).
# Real LLM calls: ZERO (B6 has no chat).
#
# Re-run:  bash scripts/verify/b6.sh   (needs PINECONE key for the delete proofs)
# =============================================================================
set -u
cd "$(dirname "$0")/../.."
dc() { MSYS2_ARG_CONV_EXCL="*" docker "$@"; }

NET=enterprise-agentic-rag_default
DB=enterprise-agentic-rag-db-1
APP=rag-verify-b6
DBV=rag_verify_b6
NS=multi-user-test
LEGACY_NS=enterprise-agentic-rag
PORT=8087
BASE=http://127.0.0.1:$PORT
TMP=$(mktemp -d)
PASS=0; FAIL=0; SKIPPED=0
ADMIN_PW="Verify-Boss-Passphrase-B6"
USER_PW="B6-User-Passphrase-7"
DOCIDS=""   # every document id the script creates — trap cleans their vectors by id

ck() { local d="$1" e="$2" a="$3"
  if [ -z "$e" ] || [ -z "$a" ]; then echo "FAIL  $d  (EMPTY expected/actual — upstream step failed)"; FAIL=$((FAIL+1)); return; fi
  if [ "$e" = "$a" ]; then echo "PASS  $d  (expected=$e actual=$a)"; PASS=$((PASS+1))
  else echo "FAIL  $d  (expected=$e actual=$a)"; FAIL=$((FAIL+1)); fi; }
ckc() { local d="$1" n="$2" h="$3"
  if [ -z "$n" ] || [ -z "$h" ]; then echo "FAIL  $d  (EMPTY needle/haystack — upstream step failed)"; FAIL=$((FAIL+1)); return; fi
  if printf '%s' "$h" | grep -qi "$n"; then echo "PASS  $d  (found: $n)"; PASS=$((PASS+1))
  else echo "FAIL  $d  (missing: $n)"; FAIL=$((FAIL+1)); fi; }
ckn() { local d="$1" n="$2" h="$3"
  if [ -z "$n" ] || [ -z "$h" ]; then echo "FAIL  $d  (EMPTY needle/haystack — upstream step failed)"; FAIL=$((FAIL+1)); return; fi
  if printf '%s' "$h" | grep -qi "$n"; then echo "FAIL  $d  (found forbidden: $n)"; FAIL=$((FAIL+1))
  else echo "PASS  $d  (absent: $n)"; PASS=$((PASS+1)); fi; }
note() { echo "---- $*"; }
psql() { dc exec $DB psql -U rag -d $DBV -Atc "$1"; }
jsonget() { dc exec -i $APP python -c "
import json, sys
print(json.loads(sys.stdin.read()).get('$1', ''))
" 2>/dev/null; }

# Exact vector ids of one document (space-separated) — list -> fetch ->
# metadata match in the test namespace. Never stats counters.
vec_ids_for() { # $1 = document id
  dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
idx = ensure_index()
ids = []
for page in idx.list(namespace='$NS', limit=100):
    ids.extend([v if isinstance(v, str) else v.id
                for v in (page if isinstance(page, list) else page.vectors)])
if ids:
    fetched = idx.fetch(ids=ids, namespace='$NS').vectors
    print(' '.join(i for i in ids if i in fetched
                   and str((fetched[i].metadata or {}).get('doc_id')) == '$1'))
" 2>/dev/null; }

# How many of the given exact ids are STILL PRESENT (0 = all gone;
# EMPTY guards against a false PASS when the upstream capture failed).
absent_ids() { # $1 = space-separated vector ids
  if [ -z "$(printf '%s' "$1" | tr -d ' ')" ]; then echo EMPTY; return; fi
  dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
idx = ensure_index()
want = '$1'.split()
got = idx.fetch(ids=want, namespace='$NS').vectors
print(len([i for i in want if i in got]))
" 2>/dev/null; }

cleanup() {
  # CLAUDE.md: clean up own vectors BY ID in a trap — tracked doc ids only,
  # test namespace only, never delete_all, never another namespace.
  dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
idx = ensure_index()
TRACKED = '''$DOCIDS'''.split()
if not TRACKED:
    print('cleanup: no documents were created — nothing to clean')
else:
    ids = []
    for page in idx.list(namespace='$NS', limit=100):
        ids.extend([v if isinstance(v, str) else v.id
                    for v in (page if isinstance(page, list) else page.vectors)])
    if not ids:
        print('cleanup: namespace already empty')
    else:
        fetched = idx.fetch(ids=ids, namespace='$NS').vectors
        ours = [i for i in ids if i in fetched
                and str((fetched[i].metadata or {}).get('doc_id')) in TRACKED]
        if ours:
            idx.delete(ids=ours, namespace='$NS')
            print('cleanup: deleted', len(ours), 'vectors by id')
        else:
            print('cleanup: no script-owned vectors found')
" 2>/dev/null
  docker rm -f $APP >/dev/null 2>&1
  dc exec $DB dropdb -U rag --if-exists $DBV >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "=============================================================="
echo "B6 LIVE VERIFICATION — $(date -u '+%Y-%m-%d %H:%M:%SZ') — disposable db $DBV — namespace $NS"
echo "=============================================================="

# --- bring up the disposable environment -------------------------------------
docker rm -f $APP >/dev/null 2>&1
dc exec $DB dropdb -U rag --if-exists $DBV >/dev/null 2>&1
dc exec $DB createdb -U rag $DBV >/dev/null
REPO_WIN=$(cygpath -w "$PWD")
dc run -d --name $APP --network $NET -p 127.0.0.1:$PORT:8080 \
  -v "${REPO_WIN}:/app" \
  -e DATABASE_URL="postgresql+psycopg://rag:rag@db:5432/$DBV" \
  -e RUN_MIGRATIONS_ON_STARTUP=true -e COOKIE_SECURE=false -e APP_ENV=development \
  -e LOGIN_RATE_LIMIT_PER_MINUTE=1000 -e PINECONE_NAMESPACE=$NS \
  enterprise-agentic-rag-app:latest \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 >/dev/null
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)" = "200" ] && break; sleep 2
done
ck "disposable app healthy" "200" "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)"

BOOTSTRAP_OUT=$(printf "verifyboss6\n$ADMIN_PW\n$ADMIN_PW\n" | dc exec -i $APP python -m app.cli bootstrap-admin 2>&1 | tail -1)
ckc "bootstrap-admin" "created" "$BOOTSTRAP_OUT"

admin_login() { curl -s -c "$TMP/admin-jar" -o /dev/null -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"verifyboss6","password":"'$ADMIN_PW'"}'; }
admin_login
ACSRF=$(awk '/csrf/{print $NF}' "$TMP/admin-jar" | tail -1)
ACODE=$(curl -s -b "$TMP/admin-jar" -o /dev/null -w '%{http_code}' $BASE/api/me)
ck "admin login works" "200" "$ACODE"

# ===== 1. create user (temp credentials) =======================================
note "CHECK 1: create user — temp password + forced-change gate"
CREATED=$(curl -s -b "$TMP/admin-jar" -X POST $BASE/api/admin/users \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $ACSRF" \
  -d '{"username":"b6tempuser","password":"B6-Temp-Passphrase-1"}')
ckc "user created with must_change_password" '"must_change_password":true' "$CREATED"
UID6=$(printf '%s' "$CREATED" | jsonget id)
TJAR="$TMP/temp-jar"
curl -s -c "$TJAR" -o /dev/null -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"b6tempuser","password":"B6-Temp-Passphrase-1"}'
TCSRF=$(awk '/csrf/{print $NF}' "$TJAR" | tail -1)
GATED=$(curl -s -b "$TJAR" -o /dev/null -w '%{http_code}' $BASE/api/admin/users)
ck "gated user blocked from admin route (403)" "403" "$GATED"
GATED2=$(curl -s -b "$TJAR" -X POST $BASE/api/conversations \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $TCSRF" -d '{}')
ckc "gated user blocked from conversations" "password_change_required" "$GATED2"
TCSRF=$(curl -s -b "$TJAR" -o /dev/null -X POST $BASE/api/auth/password \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $TCSRF" \
  -d '{"old_password":"B6-Temp-Passphrase-1","new_password":"'$USER_PW'"}'; \
  awk '/csrf/{print $NF}' "$TJAR" | tail -1)
UNLOCKED=$(curl -s -b "$TJAR" -o /dev/null -w '%{http_code}' $BASE/api/conversations)
ck "after password change the user can use the app" "200" "$UNLOCKED"

# ===== 2. authz matrix: anonymous + plain user =================================
note "CHECK 2: 401 anonymous / 403 plain user on admin routes"
ANON=$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/admin/users)
ck "anonymous -> 401" "401" "$ANON"
FORB=$(curl -s -b "$TJAR" -o /dev/null -w '%{http_code}' $BASE/api/admin/users)
ck "plain user -> 403" "403" "$FORB"
CHATREAD=$(curl -s -b "$TMP/admin-jar" -o /dev/null -w '%{http_code}' $BASE/api/admin/conversations)
ck "admin chat-reading route does not exist (404/405)" "NOTEXIST" \
  "$([ "$CHATREAD" = "404" ] || [ "$CHATREAD" = "405" ] && echo NOTEXIST || echo "$CHATREAD")"

# ===== 3. last-admin guard on EVERY path incl. self ============================
note "CHECK 3: last-admin guard"
DEMOTE=$(curl -s -o /dev/null -w '%{http_code}' -b "$TMP/admin-jar" -X PATCH \
  $BASE/api/admin/users/$(dc exec $DB psql -U rag -d $DBV -Atc "SELECT id FROM users WHERE username='verifyboss6'") \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $ACSRF" \
  -d '{"role":"user"}')
ck "sole admin demotes self -> 409" "409" "$DEMOTE"
SELFDIS=$(curl -s -o /dev/null -w '%{http_code}' -b "$TMP/admin-jar" -X PATCH \
  $BASE/api/admin/users/$(dc exec $DB psql -U rag -d $DBV -Atc "SELECT id FROM users WHERE username='verifyboss6'") \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $ACSRF" \
  -d '{"is_active":false}')
ck "sole admin disables self -> 409" "409" "$SELFDIS"
SELFDEL=$(curl -s -o /dev/null -w '%{http_code}' -b "$TMP/admin-jar" -X DELETE \
  "$BASE/api/admin/users/$(dc exec $DB psql -U rag -d $DBV -Atc "SELECT id FROM users WHERE username='verifyboss6'")?confirm=true" \
  -H "X-CSRF-Token: $ACSRF")
ck "sole admin deletes self -> 409" "409" "$SELFDEL"
MEDEL=$(curl -s -o /dev/null -w '%{http_code}' -b "$TMP/admin-jar" -X DELETE \
  "$BASE/api/me?confirm=true" -H "X-CSRF-Token: $ACSRF")
ck "sole admin self-deletes via /api/me -> 409" "409" "$MEDEL"

# ===== 4. disable + reset-password =============================================
note "CHECK 4: disable + reset-password"
TUID=$(dc exec $DB psql -U rag -d $DBV -Atc "SELECT id FROM users WHERE username='b6tempuser'")
DIS=$(curl -s -o /dev/null -w '%{http_code}' -b "$TMP/admin-jar" -X PATCH \
  $BASE/api/admin/users/$TUID -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $ACSRF" -d '{"is_active":false}')
ck "disable user -> 200" "200" "$DIS"
DEAD=$(curl -s -b "$TJAR" -o /dev/null -w '%{http_code}' $BASE/api/me)
ck "disabled user session dies immediately -> 401" "401" "$DEAD"
RESET=$(curl -s -b "$TMP/admin-jar" -X POST $BASE/api/admin/users/$TUID/reset-password \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $ACSRF" \
  -d '{"new_password":"B6-Temp-Passphrase-2"}')
ckc "reset-password -> temp flag" "must_change_password" "$RESET"
RELOG=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"b6tempuser","password":"B6-Temp-Passphrase-2"}')
ck "temp password does NOT log in while account still disabled -> 401" "401" "$RELOG"
REEN=$(curl -s -o /dev/null -w '%{http_code}' -b "$TMP/admin-jar" -X PATCH \
  $BASE/api/admin/users/$TUID -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $ACSRF" -d '{"is_active":true}')
ck "admin re-enables the user -> 200" "200" "$REEN"
RELOGIN=$(curl -s -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"b6tempuser","password":"B6-Temp-Passphrase-2"}')
ckc "temp password logs in after explicit re-enable" '"must_change_password":true' "$RELOGIN"

# ===== 5. documents: org upload/list, delete-ANY by exact ids (C3) ============
note "CHECK 5: admin documents (org upload, delete-ANY proved by exact vector ids)"
printf 'B6 live verification note about VPN portals.\n' > "$TMP/note.txt"
NOTEW=$(cygpath -w "$TMP/note.txt")
UP=$(curl -s -b "$TMP/admin-jar" -X POST $BASE/api/admin/documents \
  -H "X-CSRF-Token: $ACSRF" -F "file=@$NOTEW;filename=b6-org-note.txt")
ckc "org upload over admin session" '"document_id"' "$UP"
DOCID5=$(printf '%s' "$UP" | jsonget document_id)
DOCIDS="$DOCIDS $DOCID5"
LISTING=$(curl -s -b "$TMP/admin-jar" $BASE/api/admin/documents)
ckc "org doc appears in the listing" "b6-org-note.txt" "$LISTING"
VID5=$(vec_ids_for "$DOCID5")
ck "org doc has exact vector ids before delete" ">=1" \
  "$([ "$(printf '%s' "$VID5" | wc -w)" -ge 1 ] && echo ">=1" || echo 0)"
STATS_BEFORE=$(dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
import json
s = ensure_index().describe_index_stats()
ns = s.namespaces or {}
print(json.dumps({k: v.vector_count for k, v in ns.items()}, sort_keys=True))
" 2>/dev/null)
echo "      namespace stats before: $STATS_BEFORE"
DELANY=$(curl -s -o /dev/null -w '%{http_code}' -b "$TMP/admin-jar" -X DELETE \
  $BASE/api/admin/documents/$DOCID5 -H "X-CSRF-Token: $ACSRF")
ck "admin deletes ANY document (org) -> 200" "200" "$DELANY"
DEL404=$(curl -s -o /dev/null -w '%{http_code}' -b "$TMP/admin-jar" -X DELETE \
  $BASE/api/admin/documents/00000000-0000-0000-0000-000000000000 -H "X-CSRF-Token: $ACSRF")
ck "unknown document id -> 404" "404" "$DEL404"
ck "org doc vectors deleted BY ID (exact ids absent)" "0" "$(absent_ids "$VID5")"
STATS_AFTER=$(dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
import json
s = ensure_index().describe_index_stats()
ns = s.namespaces or {}
print(json.dumps({k: v.vector_count for k, v in ns.items()}, sort_keys=True))
" 2>/dev/null)
LEGACY_STATS_BEFORE=$(printf '%s' "$STATS_BEFORE" | grep -o "\"$LEGACY_NS\": [0-9]*")
LEGACY_STATS_AFTER=$(printf '%s' "$STATS_AFTER" | grep -o "\"$LEGACY_NS\": [0-9]*")
ck "legacy namespace untouched" "$LEGACY_STATS_BEFORE" "$LEGACY_STATS_AFTER"

# ===== 6. private documents: admin purge + self-delete, BY-ID proofs (C4/A9.1)
note "CHECK 6: private docs — admin bulk-purge, then /api/me self-delete"

# --- (a) admin bulk-purge of a user's REAL private document -------------------
MADE_P=$(curl -s -b "$TMP/admin-jar" -X POST $BASE/api/admin/users \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $ACSRF" \
  -d '{"username":"b6docowner","password":"B6-Doc-Passphrase-5"}')
ckc "purge-target user created" '"must_change_password":true' "$MADE_P"
DUID=$(printf '%s' "$MADE_P" | jsonget id)
printf 'B6 purge-target private note about fleet vehicles.\n' > "$TMP/purge-note.txt"
PURGE_NOTE_W=$(cygpath -w "$TMP/purge-note.txt")
# host side must be a native Windows path (MSYS_NO_PATHCONV is for the container side)
MSYS_NO_PATHCONV=1 docker cp "$PURGE_NOTE_W" "$APP:/tmp/b6-purge-note.txt" >/dev/null
ING_P=$(dc exec -i $APP python -m app.cli ingest-private /tmp/b6-purge-note.txt \
  --owner b6docowner 2>&1 | tail -1)
ckc "ingest-private CLI indexed a REAL private doc" "private document" "$ING_P"
DOCID_P=$(printf '%s' "$ING_P" | sed -E 's/.*private document ([0-9a-f-]{36}).*/\1/')
DOCIDS="$DOCIDS $DOCID_P"
VID_P=$(vec_ids_for "$DOCID_P")
ck "purge-target private doc has exact vector ids" ">=1" \
  "$([ "$(printf '%s' "$VID_P" | wc -w)" -ge 1 ] && echo ">=1" || echo 0)"
APURGE=$(curl -s -b "$TMP/admin-jar" -o /dev/null -w '%{http_code}' -X POST \
  "$BASE/api/admin/users/$DUID/purge-private-documents" -H "X-CSRF-Token: $ACSRF")
ck "admin bulk-purge of that user's private docs -> 200" "200" "$APURGE"
ck "purged doc vectors deleted BY ID (exact ids absent)" "0" "$(absent_ids "$VID_P")"
ck "purged doc row gone" "0" \
  "$(psql "SELECT count(*) FROM documents WHERE id='$DOCID_P'")"

# --- (b) DELETE /api/me for a user owning a REAL private document -------------
MADE=$(curl -s -b "$TMP/admin-jar" -X POST $BASE/api/admin/users \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $ACSRF" \
  -d '{"username":"b6selfdel","password":"B6-Self-Passphrase-3"}')
ckc "self-delete user created" '"must_change_password":true' "$MADE"
SUID=$(printf '%s' "$MADE" | jsonget id)
SJAR="$TMP/self-jar"
curl -s -c "$SJAR" -o /dev/null -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"b6selfdel","password":"B6-Self-Passphrase-3"}'
SSRF=$(awk '/csrf/{print $NF}' "$SJAR" | tail -1)
curl -s -b "$SJAR" -o /dev/null -X POST $BASE/api/auth/password \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $SSRF" \
  -d '{"old_password":"B6-Self-Passphrase-3","new_password":"B6-Self-Passphrase-4"}'
printf 'B6 self-delete user private note about VPN portals.\n' > "$TMP/self-note.txt"
SELF_NOTE_W=$(cygpath -w "$TMP/self-note.txt")
MSYS_NO_PATHCONV=1 docker cp "$SELF_NOTE_W" "$APP:/tmp/b6-self-note.txt" >/dev/null
ING_S=$(dc exec -i $APP python -m app.cli ingest-private /tmp/b6-self-note.txt \
  --owner b6selfdel 2>&1 | tail -1)
ckc "self-delete user owns a REAL private doc" "private document" "$ING_S"
DOCID_S=$(printf '%s' "$ING_S" | sed -E 's/.*private document ([0-9a-f-]{36}).*/\1/')
DOCIDS="$DOCIDS $DOCID_S"
VID_S=$(vec_ids_for "$DOCID_S")
ck "self-delete user's private doc has exact vector ids" ">=1" \
  "$([ "$(printf '%s' "$VID_S" | wc -w)" -ge 1 ] && echo ">=1" || echo 0)"
PURGE=$(curl -s -b "$SJAR" -o /dev/null -w '%{http_code}' -X POST \
  "$BASE/api/admin/users/$SUID/purge-private-documents" -H "X-CSRF-Token: $SSRF")
ck "purge-private-documents by plain user on self -> 403" "403" "$PURGE"
NOC=$(curl -s -b "$SJAR" -o /dev/null -w '%{http_code}' -X DELETE $BASE/api/me \
  -H "X-CSRF-Token: $SSRF")
ck "self-delete without confirm -> 422" "422" "$NOC"
DELETED=$(curl -s -b "$SJAR" -o /dev/null -w '%{http_code}' -X DELETE \
  "$BASE/api/me?confirm=true" -H "X-CSRF-Token: $SSRF")
ck "self-delete with confirm -> 200" "200" "$DELETED"
ck "self-deleted user's private doc vectors deleted BY ID" "0" "$(absent_ids "$VID_S")"
ck "self-deleted user's doc row gone" "0" \
  "$(psql "SELECT count(*) FROM documents WHERE id='$DOCID_S'")"
ck "self-deleted user row gone" "0" \
  "$(psql "SELECT count(*) FROM users WHERE id='$SUID'")"
RELOG2=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"b6selfdel","password":"B6-Self-Passphrase-4"}')
ck "deleted user login -> 401" "401" "$RELOG2"
AUD=$(psql "SELECT count(*) FROM audit_events ae WHERE ae.event_type='user_deleted' AND ae.details->>'self'='true' AND ae.actor_user_id IS NULL")
ck "user_deleted audit event anonymized (actor NULL)" "1" "$AUD"

echo "=============================================================="
echo "RESULT: PASS=$PASS FAIL=$FAIL SKIP=$SKIPPED  ($(date -u '+%Y-%m-%d %H:%M:%SZ'))"
echo "=============================================================="
[ "$FAIL" -eq 0 ]
