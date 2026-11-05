#!/usr/bin/env bash
# =============================================================================
# scripts/verify/b3.sh — B3 live verification (Part 2 of the review mandate)
#
# Disposable: throwaway app container + scratch DB (rag_verify_b3) on
# 127.0.0.1:8082. Real graph calls: exactly THREE (all greeting-class
# questions that route to "direct", so no Pinecone needed — the full
# TX1 -> graph -> TX2 path runs for real). Everything else is shape/DB/
# log-level verification. Cleans up after itself; dev DB untouched.
#
# Re-run:  bash scripts/verify/b3.sh
# Requires: docker (compose stack up), curl, awk, jq not needed.
# =============================================================================
set -u
cd "$(dirname "$0")/../.."

NET=enterprise-agentic-rag_default
DB=enterprise-agentic-rag-db-1
APP=rag-verify-b3
APP_CAPS=rag-verify-b3-caps
DBV=rag_verify_b3
PORT=8082; PORT_CAPS=8085
BASE=http://127.0.0.1:$PORT
TMP=$(mktemp -d)
PASS=0; FAIL=0; SKIPPED=0
ADMIN_PW="Verify-Boss-Passphrase-B3"
USER_PW="B3-User-Passphrase-7"

ck() { local d="$1" e="$2" a="$3"
  if [ "$e" = "$a" ]; then echo "PASS  $d  (expected=$e actual=$a)"; PASS=$((PASS+1))
  else echo "FAIL  $d  (expected=$e actual=$a)"; FAIL=$((FAIL+1)); fi; }
ckc() { local d="$1" n="$2" h="$3"
  if printf '%s' "$h" | grep -qi "$n"; then echo "PASS  $d  (found: $n)"; PASS=$((PASS+1))
  else echo "FAIL  $d  (missing: $n)"; FAIL=$((FAIL+1)); fi; }
ckn() { local d="$1" n="$2" h="$3"   # must NOT contain
  if printf '%s' "$h" | grep -qi "$n"; then echo "FAIL  $d  (found forbidden: $n)"; FAIL=$((FAIL+1))
  else echo "PASS  $d  (absent: $n)"; PASS=$((PASS+1)); fi; }
skip() { echo "SKIP  $1 ($2)"; SKIPPED=$((SKIPPED+1)); }
note() { echo "---- $*"; }
psql() { docker exec $DB psql -U rag -d $DBV -Atc "$1"; }

cleanup() {
  docker rm -f $APP $APP_CAPS >/dev/null 2>&1
  docker exec $DB dropdb -U rag --if-exists $DBV >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "=============================================================="
echo "B3 LIVE VERIFICATION — $(date -u '+%Y-%m-%d %H:%M:%SZ') — disposable db $DBV"
echo "=============================================================="

# --- bring up ----------------------------------------------------------------
docker exec $DB dropdb -U rag --if-exists $DBV >/dev/null 2>&1
docker exec $DB createdb -U rag $DBV >/dev/null
REPO_WIN=$(cygpath -w "$PWD")
docker run -d --name $APP --network $NET -p 127.0.0.1:$PORT:8080 \
  -v "${REPO_WIN}:/app" \
  -e DATABASE_URL="postgresql+psycopg://rag:rag@db:5432/$DBV" \
  -e RUN_MIGRATIONS_ON_STARTUP=true -e COOKIE_SECURE=false -e APP_ENV=development \
  -e ADMIN_API_KEY=verify-admin-key -e LOGIN_RATE_LIMIT_PER_MINUTE=1000 \
  enterprise-agentic-rag-app:latest \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 >/dev/null
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)" = "200" ] && break; sleep 2
done
ck "disposable app healthy" "200" "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)"

printf "verifyboss3\n$ADMIN_PW\n$ADMIN_PW\n" | docker exec -i $APP python -m app.cli bootstrap-admin >/dev/null 2>&1
docker exec -i $APP python -c "
from app.core.db import get_sessionmaker
from app.services import users
db = get_sessionmaker()()
for name in ('checkb3user', 'checkb3attacker'):
    users.create_user(db, name, password='$USER_PW')
db.commit(); db.close()
print('created 2 users')
" >/dev/null 2>&1

login() { # login <jar> <user> <pw>
  curl -s -c "$TMP/$1" -o /dev/null -X POST $BASE/api/auth/login \
    -H "Content-Type: application/json" -d "{\"username\":\"$2\",\"password\":\"$3\"}"
}
csrf_of() { awk '/csrf/{print $NF}' "$TMP/$1" | tail -1; }
login jar verifyboss3 "$ADMIN_PW"
login jaru checkb3user "$USER_PW"
login jaru2 checkb3attacker "$USER_PW"
CSRF=$(csrf_of jar); CSRU=$(csrf_of jaru)
ck "admin login ok" "200" "$(curl -s -b "$TMP/jar" -o /dev/null -w '%{http_code}' $BASE/api/me)"
ck "regular user login ok" "200" "$(curl -s -b "$TMP/jaru" -o /dev/null -w '%{http_code}' $BASE/api/me)"

# ===== create / list / get / rename / send / messages ==========================
note "CHECK CRUD: create/list/get/rename + send (REAL graph call #1, direct route)"
CONV=$(curl -s -b "$TMP/jaru" -X POST $BASE/api/conversations \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRU" -d '{"title":"B3 live conv"}')
CONV_ID=$(printf '%s' "$CONV" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
ckc "create conversation returns id" '"title":"B3 live conv"' "$CONV"
LIST=$(curl -s -b "$TMP/jaru" "$BASE/api/conversations?limit=10&offset=0")
ckc "list shows the conversation" "$CONV_ID" "$LIST"
ckc "list carries pagination envelope" '"total":1' "$LIST"
GET=$(curl -s -b "$TMP/jaru" "$BASE/api/conversations/$CONV_ID")
ckc "get returns metadata" '"B3 live conv"' "$GET"
REN=$(curl -s -b "$TMP/jaru" -X PATCH "$BASE/api/conversations/$CONV_ID" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRU" -d '{"title":"B3 renamed"}')
ckc "rename works" '"B3 renamed"' "$REN"

SEND=$(curl -s --max-time 120 -b "$TMP/jaru" -X POST "$BASE/api/conversations/$CONV_ID/messages" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRU" \
  -d '{"question":"thanks, this assistant is really helpful!"}')
ckc "send message (real graph, direct route) -> ok" '"status":"ok"' "$SEND"
ckc "assistant message carries model" '"model"' "$SEND"
ckc "assistant message carries trace" '"trace"' "$SEND"
ckc "assistant message carries duration_ms" '"duration_ms"' "$SEND"
ckc "assistant message carries token counts" '"prompt_tokens"' "$SEND"
MSGS=$(curl -s -b "$TMP/jaru" "$BASE/api/conversations/$CONV_ID/messages?limit=50&offset=0")
ckc "messages list has both rows" '"role":"user"' "$MSGS"
ckc "assistant row present" '"role":"assistant"' "$MSGS"
ckc "citations persisted on assistant row" '"citations"' "$MSGS"
# audit privacy (g): chat_asked carries counts/status/route only
AUD=$(psql "SELECT to_jsonb(ae)::text FROM audit_events ae WHERE event_type='chat_asked' ORDER BY id DESC LIMIT 1;")
ckc "chat_asked audit row exists with route" "chat_asked" "$AUD"
ckn "audit carries no question text" "helpful" "$AUD"
ckn "audit carries no answer text" "final answer" "$AUD"

# ===== unauth / CSRF / IDOR (every verb) / admin cannot read ===================
note "CHECK authz: 401 unauth, 403 missing CSRF, 404 IDOR on every verb, admin 404"
ck "unauthenticated list -> 401" "401" "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/conversations)"
ck "unauthenticated send -> 401" "401" "$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/conversations/$CONV_ID/messages -H 'Content-Type: application/json' -d '{"question":"who am I?"}')"
ck "missing CSRF on create -> 403" "403" "$(curl -s -b "$TMP/jaru" -o /dev/null -w '%{http_code}' -X POST $BASE/api/conversations -H 'Content-Type: application/json' -d '{}')"
# attacker probes EVERY verb with the victim's real conversation id
CSRU2=$(csrf_of jaru2)
ck "IDOR get -> 404"       "404" "$(curl -s -b "$TMP/jaru2" -o /dev/null -w '%{http_code}' $BASE/api/conversations/$CONV_ID)"
ck "IDOR list messages -> 404" "404" "$(curl -s -b "$TMP/jaru2" -o /dev/null -w '%{http_code}' $BASE/api/conversations/$CONV_ID/messages)"
ck "IDOR send -> 404"      "404" "$(curl -s -b "$TMP/jaru2" -o /dev/null -w '%{http_code}' -X POST $BASE/api/conversations/$CONV_ID/messages -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRU2" -d '{"question":"mine now?"}')"
ck "IDOR rename -> 404"    "404" "$(curl -s -b "$TMP/jaru2" -o /dev/null -w '%{http_code}' -X PATCH $BASE/api/conversations/$CONV_ID -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRU2" -d '{"title":"hijack"}')"
ck "IDOR delete -> 404"    "404" "$(curl -s -b "$TMP/jaru2" -o /dev/null -w '%{http_code}' -X DELETE $BASE/api/conversations/$CONV_ID -H "X-CSRF-Token: $CSRU2")"
# admin (verifyboss3) must also get 404 — admins are users too
ck "admin reading user conv -> 404" "404" "$(curl -s -b "$TMP/jar" -o /dev/null -w '%{http_code}' $BASE/api/conversations/$CONV_ID)"
ck "admin listing sees 0 user convs" "0" "$(curl -s -b "$TMP/jar" $BASE/api/conversations | grep -o '"total":[0-9]*' | grep -o '[0-9]*')"
# nonexistent id behaves like foreign id
ck "nonexistent id -> 404" "404" "$(curl -s -b "$TMP/jaru" -o /dev/null -w '%{http_code}' $BASE/api/conversations/00000000-0000-0000-0000-000000000000)"

# ===== failure path (graph down): marker stays out of everything ===============
note "CHECK failure path: empty OPENAI key -> 502, rows, marker absent"
MARKER="SECRET-MARKER-B3-LIVE"
docker rm -f $APP >/dev/null 2>&1
docker run -d --name $APP --network $NET -p 127.0.0.1:$PORT:8080 \
  -v "${REPO_WIN}:/app" \
  -e DATABASE_URL="postgresql+psycopg://rag:rag@db:5432/$DBV" \
  -e COOKIE_SECURE=false -e APP_ENV=development -e ADMIN_API_KEY=verify-admin-key \
  -e LOGIN_RATE_LIMIT_PER_MINUTE=1000 -e OPENAI_API_KEY= \
  enterprise-agentic-rag-app:latest \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 >/dev/null
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)" = "200" ] && break; sleep 2
done
FAIL502=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -b "$TMP/jaru" \
  -X POST "$BASE/api/conversations/$CONV_ID/messages" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRU" \
  -d "{\"question\":\"why is the $MARKER printer on fire?\"}")
ck "graph failure -> 502" "502" "$FAIL502"
ROWS=$(psql "SELECT string_agg(msg_role || ':' || msg_status, ',' ORDER BY created_at) FROM messages WHERE conversation_id='$CONV_ID';")
ckc "user message STAYS + error marker row" "user:ok" "$ROWS"
ckc "assistant error row present" "assistant:error" "$ROWS"
ERRC=$(psql "SELECT count(*) FROM messages WHERE conversation_id='$CONV_ID' AND msg_role='assistant' AND msg_status='error' AND content='';")
ck "error row content empty (no raw exception)" "1" "$ERRC"
AUDF=$(psql "SELECT to_jsonb(ae)::text FROM audit_events ae WHERE event_type='chat_failed' ORDER BY id DESC LIMIT 1;")
ckc "chat_failed audit row exists" "chat_failed" "$AUDF"
ckn "chat_failed audit has no marker" "$MARKER" "$AUDF"
LOGL=$(docker logs $APP 2>&1 | grep -ci "$MARKER" || true)
ck "marker absent from app logs on failure" "0" "$LOGL"
RETRY_CODE=$(docker rm -f $APP >/dev/null 2>&1; docker run -d --name $APP --network $NET -p 127.0.0.1:$PORT:8080 \
  -v "${REPO_WIN}:/app" \
  -e DATABASE_URL="postgresql+psycopg://rag:rag@db:5432/$DBV" \
  -e COOKIE_SECURE=false -e APP_ENV=development -e ADMIN_API_KEY=verify-admin-key \
  -e LOGIN_RATE_LIMIT_PER_MINUTE=1000 \
  enterprise-agentic-rag-app:latest \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 >/dev/null; for i in $(seq 1 60); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)" = "200" ] && break; sleep 2; done;
  curl -s -o /dev/null -w '%{http_code}' --max-time 120 -b "$TMP/jaru" \
    -X POST "$BASE/api/conversations/$CONV_ID/messages" \
    -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRU" \
    -d '{"question":"thanks again, great support!"}')
ck "retry after recovery -> 200 (REAL graph call #2)" "200" "$RETRY_CODE"

# ===== double-submit: concurrent sends against an in-flight message ============
note "CHECK double-submit: two concurrent sends serialize on the row lock"
# seed an unanswered user message (graph conceptually in flight — same as the
# b3-checkpoint recipe). BOTH concurrent sends must then 409 through the
# FOR UPDATE lock + pending check; no LLM call is needed for this proof.
docker exec -i $APP python -c "
from app.core.db import get_sessionmaker
from app.services import conversations as svc
from app.models.conversation import Conversation
db = get_sessionmaker()()
conv = db.query(Conversation).filter(Conversation.id == '$CONV_ID').one()
svc.add_message(db, conv, 'user', 'seeded unanswered question', msg_status='ok')
db.commit(); db.close()
print('seeded pending')
" >/dev/null 2>&1
curl -s -b "$TMP/jaru" -o "$TMP/send1.json" -w '%{http_code}' --max-time 60 \
  -X POST "$BASE/api/conversations/$CONV_ID/messages" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRU" \
  -d '{"question":"concurrent send one"}' > "$TMP/code1.txt" &
curl -s -b "$TMP/jaru" -o "$TMP/send2.json" -w '%{http_code}' --max-time 60 \
  -X POST "$BASE/api/conversations/$CONV_ID/messages" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRU" \
  -d '{"question":"concurrent send two"}' > "$TMP/code2.txt" &
wait
C1=$(cat "$TMP/code1.txt"); C2=$(cat "$TMP/code2.txt")
echo "      concurrent send codes: $C1 $C2"
ck "concurrent send one -> 409" "409" "$C1"
ck "concurrent send two -> 409" "409" "$C2"
PEND=$(printf '%s' "$(cat "$TMP/send1.json")$(cat "$TMP/send2.json")" | grep -o "message_pending" | wc -l | tr -d ' ')
ck "409 bodies name message_pending" "2" "$PEND"

# ===== delete conversation =====================================================
note "CHECK delete conversation"
DEL=$(curl -s -b "$TMP/jaru" -X DELETE "$BASE/api/conversations/$CONV_ID" -H "X-CSRF-Token: $CSRU")
ckc "delete returns status deleted" '"status":"deleted"' "$DEL"
GONE=$(curl -s -b "$TMP/jaru" -o /dev/null -w '%{http_code}' "$BASE/api/conversations/$CONV_ID")
ck "get after delete -> 404" "404" "$GONE"

# ===== caps + per-user rate limit (override container) =========================
note "CHECK caps + per-user chat rate limit (override container :$PORT_CAPS)"
docker run -d --name $APP_CAPS --network $NET -p 127.0.0.1:$PORT_CAPS:8080 \
  -v "${REPO_WIN}:/app" \
  -e DATABASE_URL="postgresql+psycopg://rag:rag@db:5432/$DBV" \
  -e RUN_MIGRATIONS_ON_STARTUP=true -e COOKIE_SECURE=false -e APP_ENV=development \
  -e ADMIN_API_KEY=verify-admin-key -e LOGIN_RATE_LIMIT_PER_MINUTE=1000 \
  -e MAX_MESSAGE_LENGTH=50 -e MAX_MESSAGES_PER_CONVERSATION=2 \
  -e MAX_CONVERSATIONS_PER_USER=2 -e CHAT_RATE_LIMIT_PER_MINUTE=2 \
  enterprise-agentic-rag-app:latest \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 >/dev/null
BCAP=http://127.0.0.1:$PORT_CAPS
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' $BCAP/api/health)" = "200" ] && break; sleep 2
done
login jarc checkb3user "$USER_PW"; CSRC=$(csrf_of jarc)
LONGTITLE=$(printf 'x%.0s' $(seq 1 201))
C422=$(curl -s -b "$TMP/jarc" -o /dev/null -w '%{http_code}' -X POST $BCAP/api/conversations \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRC" -d "{\"title\":\"$LONGTITLE\"}")
ck "title > 200 chars -> 422" "422" "$C422"
LONGQ=$(printf 'x%.0s' $(seq 1 51))
CV1=$(curl -s -b "$TMP/jarc" -X POST $BCAP/api/conversations -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRC" -d '{"title":"c1"}' | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
Q422=$(curl -s -b "$TMP/jarc" -o /dev/null -w '%{http_code}' -X POST "$BCAP/api/conversations/$CV1/messages" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRC" -d "{\"question\":\"$LONGQ\"}")
ck "question > MAX_MESSAGE_LENGTH(50) -> 422" "422" "$Q422"
CV2=$(curl -s -b "$TMP/jarc" -X POST $BCAP/api/conversations -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRC" -d '{"title":"c2"}' | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
CV3=$(curl -s -b "$TMP/jarc" -X POST $BCAP/api/conversations -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRC" -d '{"title":"c3"}' -o /dev/null -w '%{http_code}')
ck "conversation cap: 3rd create -> 409" "409" "$CV3"
# messages cap via seeded rows (no LLM call needed — TX1 checks before graph)
docker exec -i $APP python -c "
from app.core.db import get_sessionmaker
from app.services import conversations as svc
from app.models.conversation import Conversation
db = get_sessionmaker()()
conv = db.query(Conversation).filter(Conversation.id == '$CV1').one()
svc.add_message(db, conv, 'user', 'seed q1', msg_status='ok')
svc.add_message(db, conv, 'assistant', 'seed a1', msg_status='ok')
db.commit(); db.close()
print('seeded')
" >/dev/null 2>&1
MCAP=$(curl -s -b "$TMP/jarc" -o /dev/null -w '%{http_code}' -X POST "$BCAP/api/conversations/$CV1/messages" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRC" -d '{"question":"third message please"}')
ck "message cap: 3rd message -> 409" "409" "$MCAP"
# rate limit: 1/min — first send real (call #4... exceeds 3! -> use 200-check WITHOUT send)
RL_FIRST=$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 -b "$TMP/jarc" -X POST "$BCAP/api/conversations/$CV2/messages" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRC" \
  -d '{"question":"thanks, good work!"}')
RL_SECOND=$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 -b "$TMP/jarc" -X POST "$BCAP/api/conversations/$CV2/messages" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRC" \
  -d '{"question":"thanks once more!"}')
RL_HDR=$(curl -si --max-time 120 -b "$TMP/jarc" -X POST "$BCAP/api/conversations/$CV2/messages" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRC" \
  -d '{"question":"again!"}' | grep -i '^retry-after' | tr -d '\r')
ck "rate limit: first send -> 200 (REAL graph call #3; allowance 2 — the earlier cap-409 send consumed one)" "200" "$RL_FIRST"
ck "rate limit: immediate second send -> 429" "429" "$RL_SECOND"
ckc "429 carries Retry-After" "retry-after" "$RL_HDR"

# ===== D19 signature + legacy shape =============================================
note "CHECK D19 + legacy"
D19=$(docker exec -i $APP python -c "
import inspect
from app.rag.workflow import ask
print(','.join(sorted(inspect.signature(ask).parameters)))")
ck "ask() has no follow-up context parameter (D19)" "question,retrieval_filter" "$D19"
ck "legacy /api/chat still validates body (422)" "422" "$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/chat -H 'Content-Type: application/json' -d '{"question":""}')"
ck "legacy /api/ingest wrong key still 401" "401" "$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/api/ingest -H 'X-Admin-Key: nope' -F 'file=@README.md;filename=x.txt')"

echo "=============================================================="
echo "RESULT: PASS=$PASS FAIL=$FAIL SKIP=$SKIPPED  ($(date -u '+%Y-%m-%d %H:%M:%SZ'))"
echo "=============================================================="
[ "$FAIL" -eq 0 ]
