#!/usr/bin/env bash
# =============================================================================
# scripts/mutation/run-g2a-mutations.sh — G2 PART A mutation re-test
# (docs/tasks/g2.md: "quota enforcement, can_read in listings, the
# group-editor upload check, the subprocess limits")
#
# Hand-written mutants applied one at a time to the WORKING TREE, each run
# against its targeted offline selection in the compose test container.
# KILLED = at least one test failed; SURVIVED = selection green.
#
# Working-tree safety (A5.3, via _lib.sh): refuses a dirty tree (modified,
# staged or untracked); backs up each target file to a per-run temp dir;
# restores FROM THAT COPY — never from git — asserting a byte-identical
# restore (cmp); an EXIT trap restores everything even on interruption.
#
# Usage: bash scripts/mutation/run-g2a-mutations.sh
# =============================================================================
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/mutation/_lib.sh
source "$LIB_DIR/_lib.sh"
dc() { MSYS2_ARG_CONV_EXCL="*" docker compose --profile test "$@"; }

SEL_QUOTA="tests/test_upload_quotas.py tests/security/test_upload_quota_race.py"
SEL_DOCS="tests/test_documents_api.py"
# the wiring pin (ingest_gate built FROM INGEST_CONCURRENCY) lives in the
# settings audit — without it the P5 mutant (gate opened wide) survives,
# because the behavior test constructs its own IngestGate(1)
SEL_PARSE="tests/test_parser_subprocess.py tests/test_settings_audit.py"

run_selection() { # $1 selection
  dc run --rm test python -m pytest $1 -q --tb=no 2>/dev/null | tail -1
}

mutate() { # $1 id, $2 file, $3 selection, $4 old, $5 new
  local id="$1" file="$2" sel="$3" old="$4" new="$5"
  mutation_backup "$file"   # pristine copy, kept once per file
  if ! patch_file "$file" "$old" "$new"; then
    echo "M-$id  PATCH-FAILED (anchor not found or ambiguous)"; return
  fi
  local result
  result=$(run_selection "$sel")
  if mutation_restore "$file"; then
    if printf '%s' "$result" | grep -q "failed"; then
      echo "M-$id  KILLED  ($result)"
    else
      echo "M-$id  SURVIVED ($result)"
    fi
  fi   # a failed restore already exited loudly; the EXIT trap repairs
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then   # source-safe: no mutants when sourced
  echo "=== G2 PART A mutation re-test — $(date -u '+%Y-%m-%d %H:%M:%SZ') ==="
  mutation_preflight

  echo "--- baseline selections (unmutated) ---"
  run_selection "$SEL_QUOTA"
  run_selection "$SEL_DOCS"
  run_selection "$SEL_PARSE"

  echo "--- mutants: quota enforcement ---"
  mutate Q1 app/services/documents.py "$SEL_QUOTA" \
    "        select(User.id).where(User.id == user_id).with_for_update()" \
    "        select(User.id).where(User.id == user_id)"
  # ^ the race-safety lock removed (the 4-thread race oversubscribes)

  mutate Q2 app/services/documents.py "$SEL_QUOTA" \
    '    if count + 1 > settings.user_upload_max_docs:
        raise QuotaExceeded("docs")' \
    '    if count + 1 > settings.user_upload_max_docs:
        pass'
  # ^ the doc-count quota never enforced (51st upload sails through)

  mutate Q3 app/api/documents.py "$SEL_QUOTA" \
    '    is_admin = user.role == "admin"
    cap_mb = settings.max_upload_mb if is_admin else min(' \
    '    is_admin = False
    cap_mb = settings.max_upload_mb if is_admin else min(' \
    # ^ admin exemption removed (admins hit the personal caps)

  mutate Q4 app/api/documents.py "$SEL_QUOTA" \
    '    if not is_admin and settings.user_uploads_per_hour > 0:  # order 4
        if not upload_limiter.allow(str(user.id)):' \
    '    if False and settings.user_uploads_per_hour > 0:  # order 4
        if not upload_limiter.allow(str(user.id)):'
  # ^ the hourly window never consulted (no 429, no Retry-After)

  echo "--- mutants: can_read in listings/detail ---"
  mutate C1 app/api/documents.py "$SEL_DOCS" \
    '                sa_and(Document.scope == "private", Document.owner_id == user.id),' \
    '                Document.scope == "private",'
  # ^ every user's private docs leak into EVERYONE's listing

  mutate C2 app/api/documents.py "$SEL_DOCS" \
    '    doc = documents_service.get_document(db, doc_id)
    if doc is None or not DbAccessResolver(db).can_read(user, doc):
        raise HTTPException(status_code=404, detail="Not found")
    return doc' \
    '    doc = documents_service.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Not found")
    return doc'
  # ^ detail serves foreign/private documents to anyone with the id

  echo "--- mutants: the group-editor upload check ---"
  mutate G1 app/api/documents.py "$SEL_DOCS" \
    '        if not resolver.is_editor(user, group_uuid):
            raise HTTPException(status_code=403, detail="Editor role required")' \
    '        if False:
            raise HTTPException(status_code=403, detail="Editor role required")'
  # ^ any MEMBER (or admin) can upload into the group

  mutate G2 app/api/documents.py "$SEL_DOCS" \
    '        if group is None or not group.is_active:
            raise HTTPException(status_code=404, detail="Not found")' \
    '        if group is None:
            raise HTTPException(status_code=404, detail="Not found")'
  # ^ ARCHIVED groups accept uploads again (H13 violated on the write path)

  echo "--- mutants: subprocess limits ---"
  mutate P1 app/services/ingestion.py "$SEL_PARSE" \
    "    proc.join(timeout=timeout_s)" \
    "    proc.join(timeout=10**9)"
  # ^ the hard timeout disarmed (the hang child decides when the parent moves on)

  mutate P2 app/services/ingestion.py "$SEL_PARSE" \
    '        if len(docs) > settings.parser_max_pages:  # pages cap
            docs = docs[: settings.parser_max_pages]' \
    '        if False:  # pages cap
            docs = docs[: settings.parser_max_pages]'
  # ^ page cap removed

  mutate P3 app/services/ingestion.py "$SEL_PARSE" \
    '            if uncompressed > settings.parser_max_docx_uncompressed_mb * 1024 * 1024:
                conn.send(("caps", "docx uncompressed size"))
                return' \
    '            if False:
                conn.send(("caps", "docx uncompressed size"))
                return'
  # ^ zip-bomb guard removed

  mutate P4 app/services/ingestion.py "$SEL_PARSE" \
    '        remaining = settings.parser_max_chars  # extracted-characters cap' \
    '        remaining = settings.parser_max_chars * 1000  # extracted-characters cap'
  # ^ characters cap inflated beyond reach

  mutate P5 app/services/ingestion.py "$SEL_PARSE" \
    'ingest_gate = IngestGate(get_settings().ingest_concurrency)' \
    'ingest_gate = IngestGate(10**9)'
  # ^ the global ingest gate opened wide (parallel uploads starve the process)

  echo "=== done — $(date -u '+%Y-%m-%d %H:%M:%SZ') ==="
  git status --short
fi
