#!/usr/bin/env bash
# =============================================================================
# scripts/verify/b4.sh — B4 live verification (Part 3 of the review mandate)
#
# ALL Pinecone work happens in the DEDICATED TEST NAMESPACE (multi-user-test,
# the PINECONE_TEST_NAMESPACE setting). The real/legacy namespace
# (enterprise-agentic-rag) is only ever READ via describe_index_stats before/
# after, to prove it stayed untouched. The script deletes the test-namespace
# data when it finishes.
#
# Per-document verification by EXPLICIT VECTOR-ID ENUMERATION (index.list in
# the test namespace), never the stats counter. LLM budget: ONE real chat
# request (user A retrieves their private doc through the full chat path);
# the other retrieval proofs run at retriever level (embedding-only, same
# production filter code path).
#
# Re-run:  bash scripts/verify/b4.sh   (needs OPENAI + PINECONE keys in .env)
# =============================================================================
set -u
cd "$(dirname "$0")/../.."
# Git Bash (MSYS) rewrites POSIX-looking arguments into Windows paths before
# external commands see them — container paths like /tmp/orgdoc must survive.
# Scope MSYS path-conversion exclusion to docker calls only (see dc() below):
dc() { MSYS2_ARG_CONV_EXCL="*" docker "$@"; }

NET=enterprise-agentic-rag_default
DB=enterprise-agentic-rag-db-1
APP=rag-verify-b4
APP_CAPS=rag-verify-b4-caps
DBV=rag_verify_b4
NS=multi-user-test
LEGACY_NS=enterprise-agentic-rag
PORT=8082; PORT_CAPS=8085
BASE=http://127.0.0.1:$PORT
TMP=$(mktemp -d)
PASS=0; FAIL=0; SKIPPED=0
ADMIN_PW="Verify-Boss-Passphrase-B4"
USER_PW="B4-User-Passphrase-8"

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

# python helpers that run INSIDE the app container (repo bind mount + env set)
py_list_ns() { dc exec -i $APP python -c "
from pinecone.exceptions import NotFoundException
from app.rag.vectorstore import ensure_index
idx = ensure_index()
try:
    ids = [i for page in idx.list(namespace='$NS') for i in page]
except NotFoundException:
    ids = []   # empty/unknown namespace lists as 404
print(' '.join(sorted(ids)))
" 2>/dev/null; }
py_stats() { dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
import json
s = ensure_index().describe_index_stats()
ns = s.namespaces or {}
print(json.dumps({k: v.vector_count for k, v in ns.items()}, sort_keys=True))
" 2>/dev/null; }

cleanup() {
  # mandate: delete the TEST NAMESPACE data afterwards (namespace-scoped only)
  dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
ensure_index().delete(delete_all=True, namespace='$NS')
print('test namespace cleared')
" >/dev/null 2>&1
  docker rm -f $APP $APP_CAPS >/dev/null 2>&1
  dc exec $DB dropdb -U rag --if-exists $DBV >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "=============================================================="
echo "B4 LIVE VERIFICATION — $(date -u '+%Y-%m-%d %H:%M:%SZ') — disposable db $DBV — namespace $NS"
echo "=============================================================="

if ! grep -q '^PINECONE_API_KEY=..*' .env; then
  echo "SKIP ALL: PINECONE_API_KEY is empty in .env — B4 live checks need real Pinecone."
  exit 0
fi

# --- old-namespace baseline (must not change across the whole run) ------------
docker rm -f $APP $APP_CAPS >/dev/null 2>&1   # defensive: stale runs must not block bring-up
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

STATS_BEFORE=$(py_stats)
echo "      old-namespace baseline: $STATS_BEFORE"

# --- fixture data + users ------------------------------------------------------
mkdir -p "$TMP/orgdir"
printf 'Company VPN handbook.\nThe finance policy system is accessed via the intranet portal.\nSubmit an access request form to IT security to get finance policy permissions.\n' > "$TMP/orgdir/vpn-handbook.txt"
printf 'PRIVATE POLICY NOTES (owned by user A).\nThe secret verification phrase for finance access is: AMBER-CORVID-42.\nContact data protection before sharing outside the company.\n' > "$TMP/policy-notes.txt"
BOOTSTRAP_OUT=$(printf "verifyboss4\n$ADMIN_PW\n$ADMIN_PW\n" | dc exec -i $APP python -m app.cli bootstrap-admin 2>&1 | tail -1)
echo "      bootstrap: $BOOTSTRAP_OUT"
USERS_OUT=$(dc exec -i $APP python -c "
from app.core.db import get_sessionmaker
from app.services import users
db = get_sessionmaker()()
for name in ('checkb4usera', 'checkb4userb'):
    users.create_user(db, name, password='$USER_PW')
db.commit(); db.close()
print('users ok')
" 2>&1 | tail -1)
echo "      users: $USERS_OUT"

# ===== ingest org + private docs (D15-style CLI) ================================
note "CHECK ingest: org doc + private doc for A (real embeddings, test namespace)"
dc cp "$(cygpath -w "$TMP/orgdir")" $APP:/tmp/orgdoc >/dev/null
dc exec -i $APP sh -c "ls -la /tmp/orgdoc" 2>&1 | head -3 | sed 's/^/      cp-check: /'
ORG_OUT=$(dc exec -i $APP python -m app.cli ingest-org /tmp/orgdoc 2>&1 | tail -1)
echo "      org ingest raw: $ORG_OUT"
ckc "org doc indexed" "org document" "$ORG_OUT"
ORG_DOC_ID=$(printf '%s' "$ORG_OUT" | grep -o '[0-9a-f-]\{36\}' | head -1)
# A's file must be NAMED policy-notes.txt for the same-filename collision check,
# but the ORIGINAL /tmp/policy-notes.txt stays untouched as the chunk-id
# recompute source — so A ingests from a dedicated directory.
dc cp "$(cygpath -w "$TMP/policy-notes.txt")" $APP:/tmp/policy-notes-A.txt >/dev/null
dc exec -i $APP sh -c "mkdir -p /tmp/adir && cp /tmp/policy-notes-A.txt /tmp/adir/policy-notes.txt" >/dev/null
PA_OUT=$(dc exec -i $APP python -m app.cli ingest-private /tmp/adir/policy-notes.txt --owner checkb4usera 2>&1 | tail -1)
echo "      A ingest raw: $PA_OUT"
ckc "private doc A indexed" "private document" "$PA_OUT"
PA_DOC_ID=$(printf '%s' "$PA_OUT" | grep -o '[0-9a-f-]\{36\}' | head -1)
S_ORG=$(py_list_ns)
N_ORG=$(printf '%s' "$S_ORG" | wc -w | tr -d ' ')
ck "test namespace populated (org + A private vectors)" "YES" "$([ "$N_ORG" -gt 0 ] && echo YES || echo NO)"
echo "      vector ids in test namespace after org+A ingest: $N_ORG"

# ===== same filename, second owner: no collision ================================
note "CHECK same-filename upload by two owners"
dc exec -i $APP sh -c "mkdir -p /tmp/bdir && printf 'PRIVATE POLICY NOTES owned by user B, different content entirely.\nB keeps their own notes about the fleet vehicles policy.\n' > /tmp/bdir/policy-notes.txt" >/dev/null
PB_OUT=$(dc exec -i $APP python -m app.cli ingest-private /tmp/bdir/policy-notes.txt --owner checkb4userb 2>&1 | tail -1)
echo "      B ingest raw: $PB_OUT"
PB_DOC_ID=$(printf '%s' "$PB_OUT" | grep -o '[0-9a-f-]\{36\}' | head -1)
ck "two docs share display_name, distinct ids" "2" "$(psql "SELECT count(*) FROM documents WHERE display_name='policy-notes.txt';")"
ck "distinct document ids for same filename" "2" "$(psql "SELECT count(DISTINCT id) FROM documents WHERE display_name='policy-notes.txt';")"
S_ALL=$(py_list_ns)
N_ALL=$(printf '%s' "$S_ALL" | wc -w | tr -d ' ')
ck "same filename two owners: vector count grew by exactly B's chunks" "$((N_ORG + $(psql "SELECT COALESCE(chunk_count,0) FROM documents WHERE id='$PB_DOC_ID';"))) " "$((N_ALL)) "

# ===== full chat path: A retrieves THEIR private doc (real graph, 1 call) ======
note "CHECK retrieval ACL: user A full chat path (REAL graph call)"
login() { curl -s -c "$TMP/$1" -o /dev/null -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" -d "{\"username\":\"$2\",\"password\":\"$3\"}"; }
csrf_of() { awk '/csrf/{print $NF}' "$TMP/$1" | tail -1; }
login jara checkb4usera "$USER_PW"; CSRA=$(csrf_of jara)
echo "      app cookie_secure setting: $(dc exec -i $APP python -c 'from app.core.config import get_settings; print(get_settings().cookie_secure)' 2>&1 | tail -1)"
echo "      login Set-Cookie: $(curl -si -X POST $BASE/api/auth/login -H 'Content-Type: application/json' -d "{\"username\":\"checkb4usera\",\"password\":\"$USER_PW\"}" | grep -i '^set-cookie' | sed -E 's/(sid|csrf)=[^;]*/\1=<redacted>/g' | tr '\n' ';')"
echo "      A login check: $(curl -s -b "$TMP/jara" -o /dev/null -w '%{http_code}' $BASE/api/me); login body probe: $(curl -s -X POST $BASE/api/auth/login -H 'Content-Type: application/json' -d "{\"username\":\"checkb4usera\",\"password\":\"$USER_PW\"}" | head -c 120)"
CONV=$(curl -s -b "$TMP/jara" -X POST $BASE/api/conversations -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRA" -d '{"title":"b4 acl"}')
CONV_ID=$(printf '%s' "$CONV" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
CHAT=$(curl -s --max-time 180 -b "$TMP/jara" -X POST "$BASE/api/conversations/$CONV_ID/messages" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRA" \
  -d '{"question":"How do I get access to the finance policy system?"}')
echo "      A chat raw (first 200 chars): $(printf '%s' "$CHAT" | head -c 200)"
echo "      A conv id: $CONV_ID; A login check: $(curl -s -b "$TMP/jara" -o /dev/null -w '%{http_code}' $BASE/api/me)"
ckc "A's chat succeeded through the graph" '"status":"ok"' "$CHAT"
ckc "A's answer cites THEIR private doc" "policy-notes" "$CHAT"
TR=$(printf '%s' "$CHAT" | grep -o '"source_used":"[^"]*"' | head -1)
echo "      A's chat source_used: $TR"
ckc "A's chat used the private KB" "private_kb" "$TR"

# ===== B must NOT receive A's private chunk (retriever-level, same filter code) =
note "CHECK retrieval ACL: user B + legacy org-only + fail-closed (retriever-level)"
BRES=$(dc exec -i $APP python -c "
from app.core.db import get_sessionmaker
from app.services import users, retrieval
from app.rag.vectorstore import get_retriever
db = get_sessionmaker()()
b = users.get_by_username(db, 'checkb4userb')
a = users.get_by_username(db, 'checkb4usera')
docs = get_retriever(retrieval.retrieval_filter_for(b)).invoke('finance policy system access')
print('B-DOCS', sorted(d.metadata.get('doc_id','') for d in docs))
docs_a = get_retriever(retrieval.retrieval_filter_for(a)).invoke('finance policy system access')
print('A-DOCS', sorted(d.metadata.get('doc_id','') for d in docs_a))
" 2>&1)
echo "      B/A retrieval raw: $(printf '%s' "$BRES" | grep -E '^(B-DOCS|A-DOCS)' | tr '\n' '; ')"
echo "      B/A retrieval stderr tail: $(printf '%s' "$BRES" | grep -vE '^(B-DOCS|A-DOCS)' | tail -2 | tr '\n' '; ')"
BRES=$(printf '%s' "$BRES" | grep -E "^(B-DOCS|A-DOCS)")
B_DOCS=$(printf '%s' "$BRES" | grep '^B-DOCS' | cut -d' ' -f2-)
A_DOCS=$(printf '%s' "$BRES" | grep '^A-DOCS' | cut -d' ' -f2-)
ckc "B's retrieval includes the org doc" "$ORG_DOC_ID" "$B_DOCS"
ckn "B's retrieval EXCLUDES A's private doc" "$PA_DOC_ID" "$B_DOCS"
ckc "A's retrieval includes their private doc" "$PA_DOC_ID" "$A_DOCS"
ckn "A's retrieval excludes B's private doc" "$PB_DOC_ID" "$A_DOCS"
LEGRES=$(dc exec -i $APP python -c "
from app.services.retrieval import ORG_ONLY_FILTER
from app.rag.vectorstore import get_retriever
docs = get_retriever(ORG_ONLY_FILTER).invoke('finance policy system access')
print('LEGACY-DOCS', sorted(d.metadata.get('doc_id','') for d in docs))
" 2>&1 | grep '^LEGACY-DOCS' | cut -d' ' -f2-)
ckc "legacy org-only filter sees the org doc" "$ORG_DOC_ID" "$LEGRES"
ckn "legacy org-only filter NEVER sees private docs" "$PA_DOC_ID" "$LEGRES"
FAILOLD=$(dc exec -i $APP python -c "
from app.core.config import get_settings
from app.rag.vectorstore import ensure_index, get_embeddings
from langchain_pinecone import PineconeVectorStore
from app.services.retrieval import ORG_ONLY_FILTER
idx = ensure_index()
old_store = PineconeVectorStore(index=idx, embedding=get_embeddings(), namespace='$LEGACY_NS')
docs = old_store.as_retriever(search_kwargs={'k': 4, 'filter': ORG_ONLY_FILTER}).invoke('VPN setup steps handbook')
print('OLD-NS-HITS', len(docs))
" 2>&1 | grep '^OLD-NS-HITS' | cut -d' ' -f2)
ck "fail-closed: legacy namespace vectors (no read_access) match NOTHING under the filter" "0" "$FAILOLD"

# ===== re-ingest idempotency (same doc_id upserts over itself) ==================
note "CHECK re-ingest idempotency"
IDEM_RAW=$(dc exec -i $APP python -c "
import time
from app.rag.vectorstore import ensure_index, get_embeddings, add_documents
from langchain_pinecone import PineconeVectorStore
from langchain_core.documents import Document
from uuid import uuid4
idx = ensure_index()
store = PineconeVectorStore(index=idx, embedding=get_embeddings(), namespace='$NS')
synthetic = str(uuid4())   # decoupled from the fixture docs on purpose

def list_ids():
    # Pinecone is eventually consistent: poll until the expected count shows
    for _ in range(12):
        ids = sorted(i for page in idx.list(namespace='$NS') for i in page)
        return ids   # first read after a short settle wait
    return []

before = list_ids()
chunks = [Document(page_content='Company VPN handbook reingest check.', metadata={'start_index': 0})]
ids1 = add_documents(chunks, doc_id=synthetic, scope='org', owner_id=None, source_name='vpn-handbook.txt')
time.sleep(10)   # Pinecone eventual consistency: let the upsert land
after1 = sorted(i for page in idx.list(namespace='$NS') for i in page)
ids2 = add_documents(chunks, doc_id=synthetic, scope='org', owner_id=None, source_name='vpn-handbook.txt')
time.sleep(10)
after2 = sorted(i for page in idx.list(namespace='$NS') for i in page)
new1 = set(after1) - set(before)
print('IDEM', int(set(ids1) == new1), int(after1 == after2), len(after1), len(after2))
" 2>&1)
echo "      idempotency raw: $(printf '%s' "$IDEM_RAW" | tail -2 | tr '\n' '; ')"
IDEM=$(printf '%s' "$IDEM_RAW" | grep '^IDEM' | cut -d' ' -f2-)
echo "      idempotency (same-doc upsert | stable id set | counts): $IDEM"
ck "re-ingest writes land under the same doc_id" "1" "$(printf '%s' "$IDEM" | cut -d' ' -f1)"
ck "re-ingest is idempotent (id set unchanged after double write)" "1" "$(printf '%s' "$IDEM" | cut -d' ' -f2)"

# ===== delete A's doc: only A's vectors go ======================================
note "CHECK delete: vectors-first protocol removes ONLY A's vectors"
DEL=$(dc exec -i $APP python -c "
from app.core.db import get_sessionmaker
from app.services import documents as docs
from app.rag.vectorstore import delete_document_vectors
db = get_sessionmaker()()
d = docs.get_document(db, '$PA_DOC_ID')
docs.begin_delete(db, d)
db.commit()
delete_document_vectors('$PA_DOC_ID')   # idempotent, outside TX
docs.complete_delete(db, d.id)
db.commit(); db.close()
print('deleted A doc')
" 2>&1 | tail -1)
ckc "A's private doc deleted via §A9.1 protocol" "deleted A doc" "$DEL"
# per-document proof: recompute A's chunk ids from the source file with the
# app's OWN chunk-id algorithm, then assert NONE remain in the namespace
AGONE_RAW=$(dc exec -i $APP python -c "
from pathlib import Path
from app.rag.vectorstore import ensure_index, _chunk_id
from app.services.ingestion import load_file, chunk_documents
ids = set(i for page in ensure_index().list(namespace='$NS') for i in page)
a_ids = {_chunk_id('$PA_DOC_ID', c) for c in chunk_documents(load_file(Path('/tmp/policy-notes-A.txt')))}
# F7: the old first flag (a_ids <= ids - a_ids) was a tautology — the
# intersection count below IS the assertion (ck compares it against 0).
print('A-GONE', len(a_ids & ids))
" 2>&1)
echo "      A-gone raw: $(printf '%s' "$AGONE_RAW" | tail -2 | tr '\n' '; ')"
AGONE=$(printf '%s' "$AGONE_RAW" | grep '^A-GONE' | cut -d' ' -f2-)
ck "none of A's recomputed vector ids remain" "0" "$(printf '%s' "$AGONE" | awk '{print $NF}')"
N_AFTER=$(py_list_ns | wc -w | tr -d ' ')
N_BEFORE_DEL=$(printf '%s' "$S_ALL" | wc -w | tr -d ' ')
PA_CHUNKS=$(psql "SELECT COALESCE(chunk_count,0) FROM documents WHERE id='$PA_DOC_ID';")
ck "only A's vectors removed (count dropped by exactly A's chunk_count)" "$((N_BEFORE_DEL - PA_CHUNKS))" "$N_AFTER"
BOK=$(dc exec -i $APP python -c "
from app.core.db import get_sessionmaker
from app.services import users, retrieval
from app.rag.vectorstore import get_retriever
db = get_sessionmaker()()
b = users.get_by_username(db, 'checkb4userb')
docs = get_retriever(retrieval.retrieval_filter_for(b)).invoke('fleet vehicles policy notes')
print('B-VISIBLE', any(d.metadata.get('doc_id')=='$PB_DOC_ID' for d in docs))
" 2>&1 | grep '^B-VISIBLE' | cut -d' ' -f2)
ck "B's same-name doc still fully visible after A's delete" "True" "$BOK"

# ===== oversize upload -> 413 ===================================================
note "CHECK oversize upload -> 413"
BIG="$TMP/big.txt"; head -c 26999999 /dev/zero | tr '\0' 'a' > "$BIG" 2>/dev/null || printf 'a%.0s' $(seq 1 1000) > "$BIG"
BIGW=$(cygpath -w "$BIG")   # Windows curl.exe cannot read MSYS /tmp paths
ING413=$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 -X POST $BASE/api/ingest \
  -H "X-Admin-Key: verify-admin-key" -F "file=@$BIGW;filename=big.txt")
ck "legacy ingest 26MB (> MAX_UPLOAD_MB=25) -> 413" "413" "$ING413"
NODOC=$(psql "SELECT count(*) FROM documents WHERE display_name='big.txt';")
ck "no document row left behind" "0" "$NODOC"

# ===== old namespace untouched + cleanup ========================================
note "CHECK old namespace untouched"
STATS_AFTER=$(py_stats)
echo "      old-namespace after: $STATS_AFTER"
ck "legacy namespace vector count unchanged (test namespace may legitimately appear)" \
   "$(printf '%s' "$STATS_BEFORE" | grep -o "\"$LEGACY_NS\": [0-9]*")" \
   "$(printf '%s' "$STATS_AFTER" | grep -o "\"$LEGACY_NS\": [0-9]*")"

echo "=============================================================="
echo "RESULT: PASS=$PASS FAIL=$FAIL SKIP=$SKIPPED  ($(date -u '+%Y-%m-%d %H:%M:%SZ'))"
echo "=============================================================="
[ "$FAIL" -eq 0 ]
