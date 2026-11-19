#!/usr/bin/env bash
# =============================================================================
# scripts/verify/no-root-files.sh — A5.5: prove NO root-owned files appear
# in the repo's SOURCE TREE from containers that bind-mount it.
#
# Detection (Docker Desktop on Windows): bind-mounted host files map to
# root:root WORLD-WRITABLE (rwxrwxrwx) inside any container, while files
# CREATED by a root container carry the creating process's umask — root:root
# and NOT world-writable. The discriminator is therefore
#     find /app -user root ! -perm -002
# (ownership alone flags every host file; the perm split isolates
# container-created artifacts — verified live 2026-11-19: .env/README map
# rwxrwxrwx, .pytest_cache from root test containers was drwxr-xr-x).
#
# Scope: the SOURCE TREE only. uploads/ and data/ are runtime-data
# directories (gitignored, CLAUDE.md: never touched by code or cleanup);
# root-owned entries there are listed INFORMATIONALLY (historical operator
# artifacts — report to Farhad, never delete from a script) and do not
# fail the check. The writer fix lives in docker-compose.yml: the test
# service now runs as uid 1000 with the pytest cache provider disabled,
# so nothing in the suite writes to the repo at all.
#
# The check runs in a THROWAWAY container over a READ-ONLY mount (it can
# never itself write anything), appuser (the runtime image's non-root user).
#
# Usage:  bash scripts/verify/no-root-files.sh    (exit 0 = source tree clean)
# =============================================================================
set -u
cd "$(dirname "$0")/../.."
MSYS_NO_PATHCONV=1 docker run --rm -v "$(cygpath -w "$PWD"):/app:ro" \
  --entrypoint sh enterprise-agentic-rag-app:latest -c '
SOURCE_HITS=$(find /app -user root ! -perm -002 \
               -not -path "/app/.git/*" -not -name ".git" \
               -not -path "/app/venv/*" -not -name "venv" \
               -not -path "/app/uploads/*" -not -path "/app/uploads" \
               -not -path "/app/data/*" -not -path "/app/data" 2>/dev/null)
if [ -n "$SOURCE_HITS" ]; then
  echo "FAIL: root-owned container-created files in the source tree:"
  echo "$SOURCE_HITS" | head -40
  echo "count: $(printf "%s\n" "$SOURCE_HITS" | wc -l)"
  exit 1
fi
echo "PASS: no root-owned container-created files in the source tree"
UPLOADED_ROOT=$(find /app/uploads -user root ! -perm -002 2>/dev/null | wc -l)
echo "INFO: root-owned entries under uploads/ (operator data, reported only): $UPLOADED_ROOT"
'
