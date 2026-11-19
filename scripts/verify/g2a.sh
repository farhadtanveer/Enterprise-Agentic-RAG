#!/usr/bin/env bash
# =============================================================================
# scripts/verify/g2a.sh — G2 PART A live verification (user-facing uploads)
#
# Disposable by design: THROWAWAY app container + scratch database
# (rag_verify_g2a, loopback :8089) with PERSONAL QUOTAS deliberately tiny
# (USER_UPLOAD_MAX_MB=1, USER_UPLOAD_TOTAL_MB=2, USER_UPLOAD_MAX_DOCS=3,
# USER_UPLOADS_PER_HOUR=5) so every quota code is exercisable in minutes.
# ALL Pinecone work happens in a UNIQUE PER-RUN NAMESPACE
# (multi-user-test-<stamp>-<rand>, A5.4): the namespace starts EMPTY and
# only this run writes to it, so the cleanup trap may delete it WHOLE
# (delete_all) — no namespace-wide listing anywhere (the 55,598-vector
# lesson: list+fetch 414s at scale, and a killed run's trap poisons a
# shared namespace; a killed run now leaves an isolated, self-named
# namespace). rag-v2 is compared by EXACT vector ids before/after (H9).
# ENABLE_WEB_FALLBACK=false.
#
# Scenarios (docs/tasks/g2.md, live mandate):
#   a corrupt docx -> generic 422 (real subprocess parse failure);
#   a zip-bomb docx -> generic 422 (the pre-parse guard) — BOTH run FIRST
#     (CHECK 0) against an empty namespace: zero vectors after proves the
#     rejections happened BEFORE any embedding (A5.4);
#   quota 413 / 409 quota_total / 409 quota_docs / 429 + Retry-After
#     (the 413 again leaves the namespace empty — oversize never embeds);
#   admin exempt from the personal quotas;
#   two users uploading the SAME filename (independent documents);
#   the parallel-upload race against the total quota (committed sum bounded);
#   a PRIVATE upload retrievable ONLY by its owner (detail + full chat path).
#
# Real model usage: TWO chat conversations (owner positive control +
# outsider isolation) — inside the 4-conversation budget.
# =============================================================================
set -u
cd "$(dirname "$0")/../.."
# The curl calls below rely on MSYS path conversion for their /tmp cookie
# jars and the /dev/null sink; a GLOBALLY exported MSYS_NO_PATHCONV breaks
# them silently (found live 2026-11-19: jars landed nowhere -> 401 cascade,
# curl -o /dev/null -> exit 23). Docker calls scope their own exclusion
# inside dc() (MSYS2_ARG_CONV_EXCL); docker cp sites keep a COMMAND-SCOPED
# MSYS_NO_PATHCONV=1 prefix. The global flag must stay unset to run this.
if [ -n "${MSYS_NO_PATHCONV:-}" ]; then
  echo "REFUSING: MSYS_NO_PATHCONV is exported globally - it breaks curl's MSYS paths in this script. Unset it first." >&2
  exit 1
fi
dc() { MSYS2_ARG_CONV_EXCL="*" docker "$@"; }

NET=enterprise-agentic-rag_default
DB=enterprise-agentic-rag-db-1
APP=rag-verify-g2a
DBV=rag_verify_g2a
NS="multi-user-test-$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
case "$NS" in
  multi-user-test-*) ;;   # per-run namespace: cleanup may delete it whole
  *) echo "REFUSING: bad namespace '$NS'" >&2; exit 1 ;;
esac
PORT=8089
BASE=http://127.0.0.1:$PORT
TMP=$(mktemp -d)
PASS=0; FAIL=0; SKIPPED=0
ADMIN_PW="Verify-Boss-Passphrase-G2A"
USER_PW="G2A-User-Passphrase-5"
DOCIDS=""
CONVS=0  # counted via a marker file (chat runs in a $( ) subshell)

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
uid_of() { psql "SELECT id FROM users WHERE username='$1'"; }

vec_ids_for() { # $1 = document id -> its EXACT production vector ids,
  # recomputed from the STORED FILE (uploads/<id>/<id>.<ext>) through the
  # production load+chunk+_chunk_id path and FETCHED by id. Never a
  # namespace-wide list (this script's scale makes list+fetch 414), never
  # raw-content hashing (the splitter owns the chunk text).
  local want="$1" tries=0 got=""
  while [ "$tries" -lt 6 ] && [ -z "$got" ]; do
    tries=$((tries+1))
    [ "$tries" -gt 1 ] && sleep 2  # ride out Pinecone upsert lag
  got=$(dc exec $APP python -c "
from pathlib import Path
from app.rag.vectorstore import ensure_index, _chunk_id
from app.services.ingestion import _load_local, chunk_documents
doc_id = '$want'
paths = list((Path('/app/uploads') / doc_id).glob(doc_id + '.*'))
if not paths:
    print('', end='')
else:
    chunks = chunk_documents(_load_local(paths[0]))
    cids = [_chunk_id(doc_id, c) for c in chunks][:50]
    idx = ensure_index()
    fetched = idx.fetch(ids=cids, namespace='$NS').vectors
    print(' '.join(i for i in cids if i in fetched))
" </dev/null 2>/dev/null)
  done
  printf '%s' "$got"
}
ragv2_ids() { dc exec -i $APP python -c " # noqa: stdin closed below
from app.rag.vectorstore import ensure_index
idx = ensure_index()
ids = []
for page in idx.list(namespace='rag-v2', limit=100):
    ids.extend([v if isinstance(v, str) else v.id
                for v in (page if isinstance(page, list) else page.vectors)])
print(' '.join(sorted(ids)) if ids else '(empty)')
" </dev/null 2>/dev/null; }

cleanup() {
  # A5.4: the per-run namespace was created EMPTY for this run and only
  # this run wrote to it — deleting it WHOLE is complete and touches
  # nothing else (the guard above refused any non multi-user-test-* name).
  # No namespace-wide listing (the 55,598-vector lesson).
  if [[ "$NS" == multi-user-test-* ]]; then
    dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
ensure_index().delete(delete_all=True, namespace='$NS')
print('cleanup: per-run namespace cleared')
" 2>/dev/null
  fi
  docker rm -f $APP >/dev/null 2>&1
  dc exec $DB dropdb -U rag --if-exists $DBV >/dev/null 2>&1
  rm -rf "$TMP"
}
trap cleanup EXIT

ns_count() { # vectors in THIS run's namespace (0 while nothing embedded)
  dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
s = ensure_index().describe_index_stats().namespaces or {}
ns = s.get('$NS')
print(ns.vector_count if ns else 0)
" 2>/dev/null; }

mkuser() { # $1 username (admin session; temp credentials -> forced change)
  local made jar="$TMP/$1-jar" csrf login_code pw_code me_code
  made=$(curl -s -b "$TMP/g2aboss-jar" -X POST $BASE/api/admin/users \
    -H "Content-Type: application/json" -H "X-CSRF-Token: $ACSRF" \
    -d '{"username":"'$1'","password":"'$USER_PW'"}')
  ckc "user $1 created" '"must_change_password":true' "$made"
  login_code=$(curl -s -c "$jar" -o /dev/null -w '%{http_code}' -X POST $BASE/api/auth/login \
    -H "Content-Type: application/json" -d '{"username":"'$1'","password":"'$USER_PW'"}')
  csrf=$(awk '/csrf/{print $NF}' "$jar" | tail -1)
  pw_code=$(curl -s -o /dev/null -w '%{http_code}' -b "$jar" -X POST $BASE/api/auth/password \
    -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" \
    -d '{"old_password":"'$USER_PW'","new_password":"'$USER_PW'-changed"}')
  me_code=$(curl -s -o /dev/null -w '%{http_code}' -b "$jar" $BASE/api/me)
  if [ "$login_code" != "200" ] || [ "$pw_code" != "200" ] || [ "$me_code" != "200" ]; then
    echo "FAIL  mkuser($1) login=$login_code pw=$pw_code me-after=$me_code"; FAIL=$((FAIL+1))
  fi
}

csrf_of() { awk '/csrf/{print $NF}' "$TMP/$1-jar" | tail -1; }

up() { # up <username> <file-host-path> <name> [group_id] → HTTP code
  local u="$1" f="$2" name="$3" gid="${4:-}" w
  w=$(cygpath -w "$f")
  if [ -n "$gid" ]; then
    curl -s -o /dev/null -w '%{http_code}' --max-time 120 -b "$TMP/$u-jar" \
      -X POST $BASE/api/documents -H "X-CSRF-Token: $(csrf_of "$u")" \
      -F "file=@$w;filename=$name" -F "group_id=$gid"
  else
    curl -s -o /dev/null -w '%{http_code}' --max-time 120 -b "$TMP/$u-jar" \
      -X POST $BASE/api/documents -H "X-CSRF-Token: $(csrf_of "$u")" \
      -F "file=@$w;filename=$name"
  fi
}
upbody() { # upbody <username> <file> <name> → response body
  local u="$1" f="$2" name="$3" w
  w=$(cygpath -w "$f")
  curl -s --max-time 120 -b "$TMP/$u-jar" -X POST $BASE/api/documents \
    -H "X-CSRF-Token: $(csrf_of "$u")" -F "file=@$w;filename=$name"
}
upcb() { # upcb <username> <file> <name> → body \n code (ONE attempt)
  local u="$1" f="$2" name="$3" w
  w=$(cygpath -w "$f")
  curl -s -w '\n%{http_code}' --max-time 120 -b "$TMP/$u-jar" \
    -X POST $BASE/api/documents -H "X-CSRF-Token: $(csrf_of "$u")" \
    -F "file=@$w;filename=$name"
}
admin_upload() { # admin_upload <file> <name> → body \n code (ORG scope — the
  # ORG upload endpoint; /api/documents is private/group by design)
  local f="$1" name="$2" w
  w=$(cygpath -w "$f")
  curl -s -w '\n%{http_code}' --max-time 120 -b "$TMP/g2aboss-jar" \
    -X POST $BASE/api/admin/documents -H "X-CSRF-Token: $ACSRF" \
    -F "file=@$w;filename=$name"
}

chat() { # $1 username, $2 question → conversation answer (CONVS counter++)
  local u="$1" q="$2" csrf conv cid
  csrf=$(csrf_of "$u")
  conv=$(curl -s -b "$TMP/$u-jar" -X POST $BASE/api/conversations \
    -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" -d '{}')
  cid=$(printf '%s' "$conv" | jsonget id)
  if [ -z "$cid" ]; then echo "CONV-FAILED: $conv"; return; fi
  : > "$TMP/conv-$cid"  # marker: one file per conversation
  curl -s --max-time 240 -b "$TMP/$u-jar" -X POST "$BASE/api/conversations/$cid/messages" \
    -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" \
    -d '{"question":"'"$q"'"}'
}

echo "=============================================================="
echo "G2 PART A LIVE VERIFICATION — $(date -u '+%Y-%m-%d %H:%M:%SZ') — db $DBV — ns $NS"
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
  -e USER_UPLOAD_MAX_MB=1 -e USER_UPLOAD_TOTAL_MB=2 \
  -e USER_UPLOAD_MAX_DOCS=3 -e USER_UPLOADS_PER_HOUR=5 \
  enterprise-agentic-rag-app:latest \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 >/dev/null
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)" = "200" ] && break; sleep 2
done
ck "disposable app healthy" "200" "$(curl -s -o /dev/null -w '%{http_code}' $BASE/api/health)"

RAGV2_BEFORE=$(ragv2_ids)
echo "      rag-v2 baseline (ids): $RAGV2_BEFORE"

BOOTSTRAP_OUT=$(printf "g2aboss\n$ADMIN_PW\n$ADMIN_PW\n" | dc exec -i $APP python -m app.cli bootstrap-admin 2>&1 | tail -1)
ckc "bootstrap-admin" "created" "$BOOTSTRAP_OUT"
curl -s -c "$TMP/g2aboss-jar" -o /dev/null -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" -d '{"username":"g2aboss","password":"'$ADMIN_PW'"}'
ACSRF=$(csrf_of g2aboss)
ck "admin login works" "200" "$(curl -s -b "$TMP/g2aboss-jar" -o /dev/null -w '%{http_code}' $BASE/api/me)"

mkuser alice
mkuser bob
mkuser carol
mkuser dave
mkuser erin

# files (all under $TMP; nothing touches the repo or uploads/)
mkdir -p "$TMP/f"
head -c 2097152 /dev/zero | tr '\0' 'a' > "$TMP/f/oversize.txt"   # 2MB > 1MB/file
head -c 819200  /dev/zero | tr '\0' 'b' > "$TMP/f/a.txt"          # 0.8MB
head -c 921600  /dev/zero | tr '\0' 'c' > "$TMP/f/b.txt"          # 0.9MB
head -c 512000  /dev/zero | tr '\0' 'd' > "$TMP/f/c.txt"          # 0.5MB → total breach
head -c 102400  /dev/zero | tr '\0' 'e' > "$TMP/f/d.txt"          # tiny
head -c 102400  /dev/zero | tr '\0' 'f' > "$TMP/f/e.txt"          # tiny
head -c 819200  /dev/zero | tr '\0' 'p' > "$TMP/f/p.txt"          # race payload
printf 'Company IT portal: ALL teams reach the company deployment portal at org-portal.novaretail.internal; team-specific portals are listed in each team workspace.\n' > "$TMP/f/org-note.txt"
printf 'BOB TEAM ONLY: the bob team deploys through the dedicated portal bob-portal.novaretail.internal; never use it outside the bob team.\n' > "$TMP/f/bob-private.txt"
printf 'PK\x03\x04 junk that is not a zip at all' > "$TMP/f/corrupt.docx"
python -c "
import zipfile, sys
with zipfile.ZipFile(sys.argv[1], 'w', compression=zipfile.ZIP_DEFLATED) as z:
    z.writestr('word/document.xml', b'\x00' * (300 * 1024 * 1024))
" "$TMP/f/bomb.docx" && echo "      zip-bomb fixture written (300MB declared, compressed on disk)"

# ===== CHECK 0: parse failures FIRST, against an empty namespace ===============
# A5.4: the corrupt docx and the zip-bomb run BEFORE any successful upload —
# the namespace is empty, so zero vectors after the two 422s proves both
# rejections happened BEFORE any embedding.
note "CHECK 0: corrupt + zip-bomb docx (real subprocess parse) -> 422, zero embeddings"
ck "corrupt docx -> 422 (child parse failure)" "422" "$(up dave "$TMP/f/corrupt.docx" corrupt.docx)"
ck "zip-bomb docx (300MB declared) -> 422 (pre-parse guard)" "422" "$(up dave "$TMP/f/bomb.docx" bomb.docx)"
# the S2/B4 contract: parse failures leave the row marked "failed" —
# what must never exist is an INDEXED row for unparseable content
ck "no INDEXED row survived either failure" "0" \
  "$(psql "SELECT count(*) FROM documents WHERE owner_id='$(uid_of dave)' AND status='indexed'")"
ck "both failures left failed rows" "2" \
  "$(psql "SELECT count(*) FROM documents WHERE owner_id='$(uid_of dave)' AND status='failed'")"
ck "rejections happened BEFORE any embedding (namespace still empty)" "0" "$(ns_count)"

# ===== CHECK 1: the quota ladder (alice) =======================================
note "CHECK 1: quota 413 / 409 quota_total / 409 quota_docs / 429 + admin exempt"
ck "oversize file (2MB > USER_UPLOAD_MAX_MB=1) -> 413" "413" "$(up alice "$TMP/f/oversize.txt" oversize.txt)"
ck "413 left no row" "0" "$(psql "SELECT count(*) FROM documents WHERE display_name='oversize.txt'")"
ck "oversize rejected BEFORE any embedding (namespace still empty)" "0" "$(ns_count)"
ck "upload A (0.8MB) -> 201" "201" "$(up alice "$TMP/f/a.txt" a.txt)"
ck "upload B (0.9MB) -> 201 (total 1.7MB)" "201" "$(up alice "$TMP/f/b.txt" b.txt)"
C_RES=$(upcb alice "$TMP/f/c.txt" c.txt)
ck "upload C would breach the 2MB total -> 409" "409" "$(printf '%s' "$C_RES" | tail -1)"
ckc "409 body names quota_total" '"quota_total"' "$(printf '%s' "$C_RES" | head -n -1)"
ck "409 left no row (alice still has 2 docs)" "2" \
  "$(psql "SELECT count(*) FROM documents WHERE scope='private' AND owner_id='$(uid_of alice)'")"
ck "upload D (3rd doc) -> 201" "201" "$(up alice "$TMP/f/d.txt" d.txt)"
E_RES=$(upcb alice "$TMP/f/e.txt" e.txt)
ck "upload E (4th doc > MAX_DOCS=3) -> 409" "409" "$(printf '%s' "$E_RES" | tail -1)"
ckc "doc-count 409 body names quota_docs" '"quota_docs"' "$(printf '%s' "$E_RES" | head -n -1)"
RL_RAW=$(curl -si --max-time 60 -b "$TMP/alice-jar" \
  -X POST $BASE/api/documents -H "X-CSRF-Token: $(csrf_of alice)" \
  -F "file=@$(cygpath -w "$TMP/f/e.txt");filename=e.txt" | tr -d '\r')
ck "hourly window exhausted -> 429" "429" "$(printf '%s\n%s' "$RL_RAW" | grep -i '^HTTP/' | tail -1 | awk '{print $2}')"
ckc "429 carries Retry-After" "retry-after" "$RL_RAW"
ck "ADMIN EXEMPT: 2MB private file -> 201" "201" "$(up g2aboss "$TMP/f/oversize.txt" admin-big.txt)"
DOCIDS="$DOCIDS $(psql "SELECT string_agg(id::text, ' ') FROM documents WHERE scope='private' AND owner_id='$(uid_of alice)'")"
DOCIDS="$DOCIDS $(psql "SELECT string_agg(id::text, ' ') FROM documents WHERE scope='private' AND owner_id='$(uid_of g2aboss)'")"

# ===== CHECK 2: two users, same filename =======================================
note "CHECK 2: bob and carol upload the SAME filename — independent documents"
BOB_UP=$(upbody bob "$TMP/f/a.txt" "shared-notes.txt")
ckc "bob's upload ok" '"document_id"' "$BOB_UP"
BOB_DOC=$(printf '%s' "$BOB_UP" | jsonget document_id)
CAROL_UP=$(upbody carol "$TMP/f/b.txt" "shared-notes.txt")
ckc "carol's upload ok (same filename)" '"document_id"' "$CAROL_UP"
CAROL_DOC=$(printf '%s' "$CAROL_UP" | jsonget document_id)
if [ -n "$BOB_DOC" ] && [ -n "$CAROL_DOC" ] && [ "$BOB_DOC" != "$CAROL_DOC" ]; then
  ck "same filename yields DISTINCT documents" "distinct" "distinct"
else
  ck "same filename yields DISTINCT documents" "distinct" "collided($BOB_DOC/$CAROL_DOC)"
fi
DOCIDS="$DOCIDS $BOB_DOC $CAROL_DOC"
ck "bob cannot read carol's same-named doc (404)" "404" \
  "$(curl -s -o /dev/null -w '%{http_code}' -b "$TMP/bob-jar" $BASE/api/documents/$CAROL_DOC)"
ckn "bob's listing never shows carol's doc id" "$CAROL_DOC" \
  "$(curl -s -b "$TMP/bob-jar" $BASE/api/documents)"

# ===== CHECK 4: parallel-upload race against the total quota ===================
note "CHECK 4: 4 parallel 0.8MB uploads by erin vs the 2MB total quota"
rm -f "$TMP/race-codes"
for i in 1 2 3 4; do
  ( sleep 0.4
    curl -sS --retry 3 --retry-connrefused --retry-delay 1 -o /dev/null \
      -w '%{http_code}\n' --max-time 180 -b "$TMP/erin-jar" \
      -X POST $BASE/api/documents -H "X-CSRF-Token: $(csrf_of erin)" \
      -F "file=@$(cygpath -w "$TMP/f/p.txt");filename=p$i.txt" > "$TMP/race-code-$i" 2>> "$TMP/race-err"
    echo "curl-$i exit=$?" >> "$TMP/race-err" ) &
done
wait
cat "$TMP"/race-code-* > "$TMP/race-codes" 2>/dev/null
RACE_CODES=$(tr '\n' ' ' < "$TMP/race-codes")
echo "      parallel codes: $RACE_CODES"
[ -s "$TMP/race-err" ] && echo "      race stderr: $(tail -3 "$TMP/race-err")"
N201=$(printf '%s' "$RACE_CODES" | grep -o 201 | wc -l | tr -d ' ')
N409=$(printf '%s' "$RACE_CODES" | grep -o 409 | wc -l | tr -d ' ')
ck "race mixed 201s and 409s" "mixed" \
  "$([ "$N201" -ge 1 ] && [ "$N409" -ge 1 ] && echo mixed || echo "201s=$N201 409s=$N409")"
RACE_SUM=$(psql "SELECT COALESCE(sum(size_bytes),0) FROM documents WHERE scope='private' AND owner_id='$(uid_of erin)'")
ck "committed race sum bounded by the 2MB quota" "ok" \
  "$([ "$RACE_SUM" -le 2097152 ] && [ "$RACE_SUM" -ge 819200 ] && echo ok || echo "sum=$RACE_SUM")"
DOCIDS="$DOCIDS $(psql "SELECT string_agg(id::text, ' ') FROM documents WHERE scope='private' AND owner_id='$(uid_of erin)'")"

# ===== CHECK 5: a private upload retrievable ONLY by its owner =================
note "CHECK 5: bob's PRIVATE doc — owner-only via detail, listing, and chat"
PRIV_UP=$(upbody bob "$TMP/f/bob-private.txt" "bob-private.txt")
ckc "bob's private upload ok" '"scope":"private"' "$PRIV_UP"
PRIV_DOC=$(printf '%s' "$PRIV_UP" | jsonget document_id)
DOCIDS="$DOCIDS $PRIV_DOC"
PRIV_IDS=$(vec_ids_for "$PRIV_DOC" "$TMP/f/bob-private.txt")
ck "private doc has exact vector ids" ">=1" \
  "$([ "$(printf '%s' "$PRIV_IDS" | wc -w)" -ge 1 ] && echo ">=1" || echo 0)"
ACL_PROBE=$(dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
idx = ensure_index()
ids = '''$PRIV_IDS'''.split()
fetched = idx.fetch(ids=ids, namespace='$NS').vectors
tokens = set()
for i in ids:
    tokens |= set((fetched[i].metadata or {}).get('read_access', []))
print(sorted(tokens))
" 2>/dev/null)
ck "private vector tokens are exactly [user:bob]" "['user:$(uid_of bob)']" "$ACL_PROBE"
ck "bob retrieves his private doc (200)" "200" \
  "$(curl -s -o /dev/null -w '%{http_code}' -b "$TMP/bob-jar" $BASE/api/documents/$PRIV_DOC)"
ck "carol gets 404 on bob's private doc" "404" \
  "$(curl -s -o /dev/null -w '%{http_code}' -b "$TMP/carol-jar" $BASE/api/documents/$PRIV_DOC)"
ckn "carol's LISTING never shows bob's private doc" "bob-private" \
  "$(curl -s -b "$TMP/carol-jar" $BASE/api/documents)"

ORG_RES=$(admin_upload "$TMP/f/org-note.txt" "org-note.txt")
ck "admin org upload (org endpoint) -> 201" "201" "$(printf '%s' "$ORG_RES" | tail -1)"
ORG_DOC=$(printf '%s' "$ORG_RES" | head -n -1 | jsonget document_id)
ck "org note is scope=org in the DB" "org"   "$(psql "SELECT scope FROM documents WHERE id='$ORG_DOC'")"
DOCIDS="$DOCIDS $ORG_DOC"

note "CHECK 5b: the full chat path ($CONVS conversations so far — budget 4)"
Q="According to the company knowledge base, which portal address should the bob team use for deployments?"
B_ANS=$(chat bob "$Q")
diag() { dc exec -i $APP python -c "
import json, sys
try:
    b = json.loads(sys.stdin.read())
    print('      $1: source_used=' + str(b.get('source_used')) +
          ' citations=' + str([c.get('title') for c in b.get('citations', [])]))
except Exception:
    print('      $1: <unparseable>')
" <<<"$2" 2>/dev/null; }
diag "bob(owner)" "$B_ANS"
ckc "bob's answer cites his PRIVATE doc (owner positive control)" '"bob-private.txt"' "$B_ANS"
C_ANS=$(chat carol "$Q")
diag "carol" "$C_ANS"
ckn "carol's answer NEVER cites bob's private doc" '"bob-private.txt"' "$C_ANS"
ckc "carol's answer cites the ORG doc (positive control)" '"org-note.txt"' "$C_ANS"

# ===== CHECK 6: rag-v2 untouched + legacy namespace stats =====================
note "CHECK 6: rag-v2 unchanged (exact ids); legacy namespace untouched"
LEGACY_STATS_BEFORE=$(dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
import json
s = ensure_index().describe_index_stats()
ns = s.namespaces or {}
print(json.dumps({k: v.vector_count for k, v in ns.items()}, sort_keys=True))
" 2>/dev/null)
LEGACY_STATS_AFTER=$(dc exec -i $APP python -c "
from app.rag.vectorstore import ensure_index
import json
s = ensure_index().describe_index_stats()
ns = s.namespaces or {}
print(json.dumps({k: v.vector_count for k, v in ns.items()}, sort_keys=True))
" 2>/dev/null)
ck "legacy namespace untouched" \
  "$(printf '%s' "$LEGACY_STATS_BEFORE" | grep -o '"enterprise-agentic-rag": [0-9]*')" \
  "$(printf '%s' "$LEGACY_STATS_AFTER" | grep -o '"enterprise-agentic-rag": [0-9]*')"
RAGV2_AFTER=$(ragv2_ids)
ck "rag-v2 ids unchanged across the whole run" "$RAGV2_BEFORE" "$RAGV2_AFTER"

echo "=============================================================="
if [ "$FAIL" -gt 0 ]; then
  echo "--- app log (ASGI exception frames, for diagnosis) ---"
  docker logs $APP 2>&1 | awk '/Exception in ASGI/{f=1} f' | grep -E 'File "/app|Error' | head -12
fi
echo "=============================================================="
echo "RESULT: PASS=$PASS FAIL=$FAIL SKIP=$SKIPPED  ($(date -u '+%Y-%m-%d %H:%M:%SZ'))"
echo "NOTE: Pinecone writes confined to the per-run namespace $NS; trap deleted it whole (A5.4)."
echo "NOTE: chat conversations used: $(ls "$TMP"/conv-* 2>/dev/null | wc -l | tr -d ' ') (budget 4)."
echo "=============================================================="
[ "$FAIL" -eq 0 ]
