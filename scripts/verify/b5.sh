#!/usr/bin/env bash
# =============================================================================
# scripts/verify/b5.sh — B5 live verification (legacy audit writer removal)
#
# Disposable by design: a THROWAWAY app container (repo bind-mounted) +
# scratch database (rag_verify_b5, loopback :8086). Proves that serving
# traffic creates/writes NOTHING in data/audit.db, that the legacy writer
# module is gone, and that a real legacy-chat call (ONE real LLM call)
# leaves no question text in logs or in the audit file. The EXISTING host
# data/audit.db is preserved — its row count is recorded before and after
# and must be IDENTICAL (C8: never delete; report only).
#
# Pinecone (CLAUDE.md): ALL app-side writes go to the dedicated namespace
# multi-user-test (PINECONE_NAMESPACE on the throwaway container); the
# cleanup trap deletes the script's vectors BY ID (this run's document_id
# plus any prior crashed run's b5-note.txt leftovers) before removing the
# container. rag-v2 and enterprise-agentic-rag are never written.
#
# Re-run:  bash scripts/verify/b5.sh   (needs OPENAI + PINECONE keys in .env)
# =============================================================================
set -u
cd "$(dirname "$0")/../.."
dc() { MSYS2_ARG_CONV_EXCL="*" docker "$@"; }

NET=enterprise-agentic-rag_default
DB=enterprise-agentic-rag-db-1
APP=rag-verify-b5
DBV=rag_verify_b5
NS=multi-user-test
PORT=8086
BASE=http://127.0.0.1:$PORT
TMP=$(mktemp -d)
PASS=0; FAIL=0; SKIPPED=0
ADMIN_PW="Verify-Boss-Passphrase-B5"
MARKER="B5LEAKMARKER-77"

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
audit_rows() { dc exec -i $APP python -c "
import sqlite3, os
p = '/app/data/audit.db'
print(sqlite3.connect(p).execute('select count(*) from query_audit').fetchone()[0] if os.path.exists(p) else 'NOFILE')
" 2>/dev/null; }

DOCID=""

cleanup() {
  # CLAUDE.md: clean up own vectors BY ID in a trap, test namespace only.
  # Runs while the throwaway container still exists (it has the keys).
  if [ -n "$DOCID" ]; then
    dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
idx = ensure_index()
ids = []
for page in idx.list(namespace='$NS', limit=100):
    ids.extend([v if isinstance(v, str) else v.id
                for v in (page if isinstance(page, list) else page.vectors)])
if not ids:
    print('cleanup: test namespace already empty')
else:
    fetched = idx.fetch(ids=ids, namespace='$NS').vectors
    ours = [i for i in ids if i in fetched and ((fetched[i].metadata or {}).get('doc_id') == '$DOCID'
            or (fetched[i].metadata or {}).get('source') == 'b5-note.txt')]
    if ours:
        idx.delete(ids=ours, namespace='$NS')
        print('cleanup: deleted', len(ours), 'vectors by id')
    else:
        print('cleanup: no script-owned vectors found')
" 2>/dev/null
  fi
  docker rm -f $APP >/dev/null 2>&1
  dc exec $DB dropdb -U rag --if-exists $DBV >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "=============================================================="
echo "B5 LIVE VERIFICATION — $(date -u '+%Y-%m-%d %H:%M:%SZ') — disposable db $DBV"
echo "=============================================================="

if ! grep -q '^PINECONE_API_KEY=..*' .env; then
  echo "SKIP ALL: PINECONE_API_KEY is empty in .env — the legacy-chat e2e check needs it."
  exit 0
fi

# --- bring up the disposable environment -------------------------------------
docker rm -f $APP >/dev/null 2>&1
dc exec $DB dropdb -U rag --if-exists $DBV >/dev/null 2>&1
dc exec $DB createdb -U rag $DBV >/dev/null
REPO_WIN=$(cygpath -w "$PWD")
dc run -d --name $APP --network $NET -p 127.0.0.1:$PORT:8080 \
  -v "${REPO_WIN}:/app" \
  -e DATABASE_URL="postgresql+psycopg://rag:rag@db:5432/$DBV" \
  -e RUN_MIGRATIONS_ON_STARTUP=true -e COOKIE_SECURE=false -e APP_ENV=development \
  -e ADMIN_API_KEY=verify-admin-key -e LOGIN_RATE_LIMIT_PER_MINUTE=1000 \
  -e PINECONE_NAMESPACE=$NS \
  enterprise-agentic-rag-app:latest \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 >/dev/null
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)" = "200" ] && break; sleep 2
done
ck "disposable app healthy" "200" "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)"

# ===== 1. the legacy writer module is gone =====================================
note "CHECK 1: legacy writer removed"
GONE=$(dc exec -i $APP python -c "
try:
    import app.services.audit
    print('STILL THERE')
except ModuleNotFoundError:
    print('GONE')
" 2>&1 | tail -1)
ck "app.services.audit no longer importable" "GONE" "$GONE"
AUDIT_DB_ENV=$(dc exec -i $APP python -c "
from app.core.config import get_settings
print('HAS' if hasattr(get_settings(), 'audit_db_path') else 'ABSENT')
" 2>&1 | tail -1)
ck "AUDIT_DB_PATH setting removed" "ABSENT" "$AUDIT_DB_ENV"

# ===== 2. startup + health created no audit.db =================================
note "CHECK 2: nothing (re)creates data/audit.db"
ROWS_BEFORE=$(audit_rows)
echo "      host data/audit.db rows before: $ROWS_BEFORE (existing file preserved, never deleted)"
docker logs $APP 2>&1 | grep -qi "init_db\|query_audit" && INIT_LOG="mentioned" || INIT_LOG="absent"
ck "no init_db/query_audit activity in app logs" "absent" "$INIT_LOG"

# ===== 3. bootstrap + legacy chat e2e (ONE real LLM call) ======================
note "CHECK 3: legacy chat writes no question/trace text anywhere"
BOOTSTRAP_OUT=$(printf "verifyboss5\n$ADMIN_PW\n$ADMIN_PW\n" | dc exec -i $APP python -m app.cli bootstrap-admin 2>&1 | tail -1)
ckc "bootstrap-admin works (app still healthy)" "created" "$BOOTSTRAP_OUT"
CSRF=$(curl -s -c "$TMP/jar" -o /dev/null -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"verifyboss5","password":"'$ADMIN_PW'"}' && \
  awk '/csrf/{print $NF}' "$TMP/jar" | tail -1)
CHAT=$(curl -s --max-time 180 -X POST $BASE/api/chat -H "Content-Type: application/json" \
  -d '{"question":"How do I connect to the company VPN? '"$MARKER"'"}')
ckc "legacy chat answered end-to-end (1 real LLM call)" '"answer"' "$CHAT"
ckn "marker absent from the response trace" "$MARKER" \
  "$(printf '%s' "$CHAT" | grep -o '"trace":\[[^]]*\]')"
ckn "marker absent from app logs" "$MARKER" "$(docker logs $APP 2>&1)"
ROWS_AFTER=$(audit_rows)
ck "data/audit.db row count UNCHANGED after chat" "$ROWS_BEFORE" "$ROWS_AFTER"

# ===== 4. legacy ingest writes no audit text ===================================
note "CHECK 4: legacy ingest leaves no audit db write (test namespace only)"
printf 'B5 verifier note about VPN portals.\n' > "$TMP/note.txt"
NOTEW=$(cygpath -w "$TMP/note.txt")
ING_RAW=$(curl -s --max-time 120 -w '\n%{http_code}' -X POST $BASE/api/ingest \
  -H "X-Admin-Key: verify-admin-key" -F "file=@$NOTEW;filename=b5-note.txt")
ING=$(printf '%s' "$ING_RAW" | tail -1)
ING_BODY=$(printf '%s' "$ING_RAW" | head -n -1)
ck "legacy ingest succeeded" "200" "$ING"
DOCID=$(printf '%s' "$ING_BODY" | dc exec -i $APP python -c "
import json, sys
print(json.loads(sys.stdin.read()).get('document_id', ''))
" 2>/dev/null)
ck "ingest returned document_id for the by-id trap cleanup" "uuid" \
  "$([ ${#DOCID} -eq 36 ] && echo uuid || echo "len=${#DOCID}")"
ROWS_FINAL=$(audit_rows)
ck "data/audit.db row count STILL unchanged after ingest" "$ROWS_BEFORE" "$ROWS_FINAL"

echo "=============================================================="
echo "RESULT: PASS=$PASS FAIL=$FAIL SKIP=$SKIPPED  ($(date -u '+%Y-%m-%d %H:%M:%SZ'))"
echo "NOTE: host data/audit.db preserved ($ROWS_FINAL rows) — report, never delete."
echo "NOTE: Pinecone writes were confined to namespace $NS; the trap deletes the script's vectors by id."
echo "=============================================================="
[ "$FAIL" -eq 0 ]
