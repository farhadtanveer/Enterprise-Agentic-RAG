#!/usr/bin/env bash
# =============================================================================
# scripts/verify/g1.sh — G1 live verification (groups access control)
#
# Disposable by design: THROWAWAY app container + scratch database
# (rag_verify_g1, loopback :8088). ALL Pinecone work happens in the
# DEDICATED TEST NAMESPACE (multi-user-test); the legacy namespace is only
# READ via describe_index_stats before/after. The trap deletes the script's
# vectors BY ID. ZERO Pinecone writes reach rag-v2 (checked externally
# after the run).
#
# Scenarios (docs/tasks/g1.md PART 1, live mandate):
#   users A (member of group alpha), B (member of beta), admin (no groups);
#   org + alpha + beta documents via the operator CLIs;
#   A asks the shared question through the FULL chat path and cites BOTH the
#   alpha doc AND the org doc (positive controls);
#   B asks the same question — org doc cited, NO alpha chunk/citation;
#   admin the same;
#   REMOVE A from alpha → A's NEXT question with the SAME session cookie
#   loses the alpha doc (org still cited — nothing else changed);
#   the org document was visible to all three throughout.
#
# Real model usage: FOUR chat conversations (A-before, B, admin, A-after)
# = the FOUR mandated scenarios. The task text says "at most 3 real LLM
# calls" but mandates FOUR distinct conversations; the removal-with-same-
# cookie proof cannot be dropped (it IS the revocation semantics), so the
# deviation is DISCLOSED here and in the evidence rather than silently
# skipping a mandated scenario. Every conversation makes ~3 real LLM API
# requests (router + KB grade + generation) inside the graph.
# =============================================================================
set -u
cd "$(dirname "$0")/../.."
dc() { MSYS2_ARG_CONV_EXCL="*" docker "$@"; }

NET=enterprise-agentic-rag_default
DB=enterprise-agentic-rag-db-1
APP=rag-verify-g1
DBV=rag_verify_g1
NS=multi-user-test
LEGACY_NS=enterprise-agentic-rag
PORT=8088
BASE=http://127.0.0.1:$PORT
TMP=$(mktemp -d)
PASS=0; FAIL=0; SKIPPED=0
ADMIN_PW="Verify-Boss-Passphrase-G1"
USER_PW="G1-User-Passphrase-9"
DOCIDS=""

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

vec_ids_for() { # $1 = document id → exact vector ids (space-separated)
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

cleanup() {
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

chat() { # $1 jar, $2 question → conversation answer (citations+content+trace)
  local jar="$1" q="$2" csrf conv
  csrf=$(awk '/csrf/{print $NF}' "$jar" | tail -1)
  conv=$(curl -s -b "$jar" -X POST $BASE/api/conversations \
    -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" -d '{}')
  local cid
  cid=$(printf '%s' "$conv" | jsonget id)
  if [ -z "$cid" ]; then echo "CONV-FAILED: $conv"; return; fi
  curl -s --max-time 240 -b "$jar" -X POST "$BASE/api/conversations/$cid/messages" \
    -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" \
    -d '{"question":"'"$q"'"}'
}

echo "=============================================================="
echo "G1 LIVE VERIFICATION — $(date -u '+%Y-%m-%d %H:%M:%SZ') — disposable db $DBV — namespace $NS"
echo "=============================================================="

if ! grep -q '^OPENAI_API_KEY=..*' .env || ! grep -q '^PINECONE_API_KEY=..*' .env; then
  echo "SKIP ALL: OPENAI/PINECONE keys missing in .env — the chat proofs need them."
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
  -e LOGIN_RATE_LIMIT_PER_MINUTE=1000 -e PINECONE_NAMESPACE=$NS \
  -e ENABLE_WEB_FALLBACK=false \
  enterprise-agentic-rag-app:latest \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 >/dev/null
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)" = "200" ] && break; sleep 2
done
ck "disposable app healthy" "200" "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)"

LEGACY_STATS_BEFORE=$(dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
import json
s = ensure_index().describe_index_stats()
ns = s.namespaces or {}
print(json.dumps({k: v.vector_count for k, v in ns.items()}, sort_keys=True))
" 2>/dev/null)
echo "      namespace stats before: $LEGACY_STATS_BEFORE"

BOOTSTRAP_OUT=$(printf "g1boss\n$ADMIN_PW\n$ADMIN_PW\n" | dc exec -i $APP python -m app.cli bootstrap-admin 2>&1 | tail -1)
ckc "bootstrap-admin" "created" "$BOOTSTRAP_OUT"
AJAR="$TMP/admin-jar"
curl -s -c "$AJAR" -o /dev/null -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" -d '{"username":"g1boss","password":"'$ADMIN_PW'"}'
ACSRF=$(awk '/csrf/{print $NF}' "$AJAR" | tail -1)
ck "admin login works" "200" "$(curl -s -b "$AJAR" -o /dev/null -w '%{http_code}' $BASE/api/me)"

# --- users A and B (temp credentials -> forced change) ------------------------
mkuser() { # $1 username, $2 jar
  local made
  made=$(curl -s -b "$AJAR" -X POST $BASE/api/admin/users \
    -H "Content-Type: application/json" -H "X-CSRF-Token: $ACSRF" \
    -d '{"username":"'$1'","password":"'$USER_PW'"}')
  ckc "user $1 created (temp credentials)" '"must_change_password":true' "$made"
  local jar="$2"
  curl -s -c "$jar" -o /dev/null -X POST $BASE/api/auth/login \
    -H "Content-Type: application/json" -d '{"username":"'$1'","password":"'$USER_PW'"}'
  local csrf
  csrf=$(awk '/csrf/{print $NF}' "$jar" | tail -1)
  curl -s -b "$jar" -o /dev/null -X POST $BASE/api/auth/password \
    -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" \
    -d '{"old_password":"'$USER_PW'","new_password":"'$USER_PW'-changed"}'
}
mkuser alice "$TMP/alice-jar"
mkuser bob "$TMP/bob-jar"
ck "alice can use the app (gate passed)" "200" \
  "$(curl -s -b "$TMP/alice-jar" -o /dev/null -w '%{http_code}' $BASE/api/me)"

# --- documents via the operator CLIs (test namespace) --------------------------
note "CHECK 1: org + alpha + beta documents via the CLIs"
mkdir -p "$TMP/files/orgkb"
# org-note must ALONE be sufficient for the shared question (the grader is
# correctness-driven: without an explicit alpha mention it flips to weak
# for B/admin, which under web-fallback opt-in would detour through Tavily)
printf 'Company IT portal: ALL teams — including the alpha team — reach the company VPN portal at vpn-novaretail.internal; the IT helpdesk answers on extension 4000 around the clock.\n' > "$TMP/files/orgkb/org-note.txt"
printf 'ALPHA GROUP ONLY — alpha runs a dedicated STAGING concentrator at alpha-staging.novaretail.internal; it is separate from the company VPN portal (which stays vpn-novaretail.internal per the org note). Alpha members use the staging concentrator for deployments and never share it outside the alpha group.\n' > "$TMP/files/g1-note.txt"
printf 'BETA GROUP ONLY — the beta team build queue endpoint is beta-queue.novaretail.internal; beta members use it for nightly builds and never share it outside the beta group.\n' > "$TMP/files/g2-note.txt"
SRC_FILES=$(cygpath -w "$TMP/files")
# host side must be a native Windows path (MSYS_NO_PATHCONV is for the container side)
MSYS_NO_PATHCONV=1 docker cp "$SRC_FILES" "$APP:/tmp/g1files" >/dev/null

ORG_OUT=$(dc exec -i $APP python -m app.cli ingest-org /tmp/g1files/orgkb 2>&1 | tail -1)
ckc "ingest-org indexed the org document" "org document" "$ORG_OUT"
ORG_DOC=$(printf '%s' "$ORG_OUT" | sed -E 's/.*org document ([0-9a-f-]{36}).*/\1/')
DOCIDS="$DOCIDS $ORG_DOC"

G1_OUT=$(dc exec -i $APP python -m app.cli create-group alpha --contact alpha-alias 2>&1 | tail -1)
ckc "create-group alpha" "created" "$G1_OUT"
ALPHA_GROUP=$(printf '%s' "$G1_OUT" | sed -E 's/.*created \(id ([0-9a-f-]{36}),.*/\1/')
G2_OUT=$(dc exec -i $APP python -m app.cli create-group beta 2>&1 | tail -1)
ckc "create-group beta" "created" "$G2_OUT"

ING1=$(dc exec -i $APP python -m app.cli ingest-group alpha /tmp/g1files/g1-note.txt 2>&1 | tail -1)
ckc "ingest-group alpha" "group document" "$ING1"
G1_DOC=$(printf '%s' "$ING1" | sed -E 's/.*group document ([0-9a-f-]{36}).*/\1/')
DOCIDS="$DOCIDS $G1_DOC"
ING2=$(dc exec -i $APP python -m app.cli ingest-group beta /tmp/g1files/g2-note.txt 2>&1 | tail -1)
ckc "ingest-group beta" "group document" "$ING2"
G2_DOC=$(printf '%s' "$ING2" | sed -E 's/.*group document ([0-9a-f-]{36}).*/\1/')
DOCIDS="$DOCIDS $G2_DOC"

MEM1=$(dc exec -i $APP python -m app.cli add-member alpha alice 2>&1 | tail -1)
ckc "add-member alpha alice" "as member" "$MEM1"
MEM2=$(dc exec -i $APP python -m app.cli add-member beta bob 2>&1 | tail -1)
ckc "add-member beta bob" "as member" "$MEM2"

# exact vector ids per document (test namespace; never stats)
ck "alpha doc has exact vector ids" ">=1" \
  "$([ "$(vec_ids_for "$G1_DOC" | wc -w)" -ge 1 ] && echo ">=1" || echo 0)"
ck "beta doc has exact vector ids" ">=1" \
  "$([ "$(vec_ids_for "$G2_DOC" | wc -w)" -ge 1 ] && echo ">=1" || echo 0)"
ck "org doc has exact vector ids" ">=1" \
  "$([ "$(vec_ids_for "$ORG_DOC" | wc -w)" -ge 1 ] && echo ">=1" || echo 0)"

# metadata ACL spot check: alpha vectors carry ONLY the alpha-group token
ACL_PROBE=$(dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
idx = ensure_index()
ids = '''$(vec_ids_for "$G1_DOC")'''.split()
fetched = idx.fetch(ids=ids, namespace='$NS').vectors
tokens = set()
for i in ids:
    tokens |= set((fetched[i].metadata or {}).get('read_access', []))
print(sorted(tokens))
" 2>/dev/null)
ck "alpha vector read_access tokens are exactly the group token" \
  "['group:$ALPHA_GROUP']" "$ACL_PROBE"

echo "      (doc ids: org=$ORG_DOC alpha=$G1_DOC beta=$G2_DOC)"

# --- CHECK 2: the full-chat isolation matrix -----------------------------------
note "CHECK 2: shared question — A cites alpha+org; B org only; admin org only"
# "According to the company knowledge base" biases the router to KB for
# every asker identically (the graph's temp-0 router still occasionally
# flips to 'direct' on terse questions, which yields no citations at all)
Q="According to the company knowledge base, what is the VPN portal address used by the alpha team?"

diag() { # $1 label, $2 raw answer — print source_used + cited titles + trace tail
  dc exec -i $APP python -c "
import json, sys
try:
    b = json.loads(sys.stdin.read())
    print('      $1: source_used=' + str(b.get('source_used')) +
          ' citations=' + str([c.get('title') for c in b.get('citations', [])]))
    tr = b.get('trace') or []
    for line in tr[-4:]:
        print('        trace>', str(line)[:110])
except Exception:
    print('      $1: <unparseable response>')
" <<<"$2" 2>/dev/null
}

A_ANS=$(chat "$TMP/alice-jar" "$Q")
diag "A" "$A_ANS"
ckc "A's answer cites the ALPHA document" '"g1-note.txt"' "$A_ANS"
ckc "A's answer cites the ORG document (positive control)" '"org-note.txt"' "$A_ANS"
ckn "A's answer never surfaces the BETA doc" '"g2-note.txt"' "$A_ANS"

B_ANS=$(chat "$TMP/bob-jar" "$Q")
diag "B" "$B_ANS"
ckn "B's answer never cites the ALPHA doc" '"g1-note.txt"' "$B_ANS"
ckc "B's answer cites the ORG document (org visible to B)" '"org-note.txt"' "$B_ANS"
ckn "B's answer never surfaces the BETA leak via alpha" 'alpha-vpn' "$B_ANS"

AD_ANS=$(chat "$AJAR" "$Q")
diag "admin" "$AD_ANS"
ckn "admin's answer never cites the ALPHA doc (no implicit access)" '"g1-note.txt"' "$AD_ANS"
ckc "admin's answer cites the ORG document" '"org-note.txt"' "$AD_ANS"

# --- CHECK 3: remove A from alpha → next question with SAME cookie loses it ----
note "CHECK 3: membership revoke applies on A's very next question"
RM=$(dc exec -i $APP python -m app.cli remove-member alpha alice 2>&1 | tail -1)
ckc "remove-member alpha alice" "removed" "$RM"
AUD=$(psql "SELECT count(*) FROM audit_events WHERE event_type='membership_removed'")
ck "membership_removed audited" "1" "$AUD"

A2_ANS=$(chat "$TMP/alice-jar" "$Q")
diag "A-after-revoke" "$A2_ANS"
ckn "A's NEXT answer no longer cites the ALPHA doc (same cookie)" '"g1-note.txt"' "$A2_ANS"
ckc "A's answer STILL cites the ORG document (nothing else changed)" '"org-note.txt"' "$A2_ANS"

# legacy namespace untouched (read-only stats)
LEGACY_STATS_AFTER=$(dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
import json
s = ensure_index().describe_index_stats()
ns = s.namespaces or {}
print(json.dumps({k: v.vector_count for k, v in ns.items()}, sort_keys=True))
" 2>/dev/null)
ck "legacy namespace untouched" \
  "$(printf '%s' "$LEGACY_STATS_BEFORE" | grep -o "\"$LEGACY_NS\": [0-9]*")" \
  "$(printf '%s' "$LEGACY_STATS_AFTER" | grep -o "\"$LEGACY_NS\": [0-9]*")"

echo "=============================================================="
echo "RESULT: PASS=$PASS FAIL=$FAIL SKIP=$SKIPPED  ($(date -u '+%Y-%m-%d %H:%M:%SZ'))"
echo "NOTE: Pinecone writes confined to $NS; trap deletes tracked doc vectors by id."
echo "=============================================================="
[ "$FAIL" -eq 0 ]
