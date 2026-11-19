#!/usr/bin/env bash
# =============================================================================
# scripts/mutation/_lib.sh — shared mutation-harness machinery (A5.3).
#
# Sourced by run-g1-mutations.sh / run-g2a-mutations.sh (and exercised by
# selftest.sh in a throwaway sandbox). Working-tree safety rules
# (CLAUDE.md, docs/tasks/g2.md A5.3):
#   - REFUSES to run on a dirty tree — modified, staged OR untracked
#     (uncommitted work is never discardable; the 2026-11-05 incident class);
#   - every target file is BACKED UP to a per-run temp dir (mktemp -d —
#     a path the run created itself);
#   - restores happen FROM THAT COPY — never from git — and each restore is
#     asserted byte-identical (cmp); a failed restore exits loudly, leaving
#     the file for manual recovery (the EXIT trap then retries from the
#     same copy);
#   - an EXIT trap restores every backed-up file even when the run is
#     interrupted mid-mutant, then removes the temp dir (the one recursive
#     delete allowed here: our own mktemp path).
# =============================================================================
MUT_BACKUP_DIR=""

mutation_die() { echo "REFUSING: $*" >&2; exit 1; }

mutation_preflight() {
  # git status --porcelain covers modified, staged AND untracked files —
  # all of them are uncommitted work the old diff-based check let through
  [ -n "$(git status --porcelain)" ] && mutation_die \
    "working tree not clean (modified, staged or untracked files) — commit first (the H10/A2 lesson)"
  MUT_BACKUP_DIR="$(mktemp -d)" || mutation_die "could not create the backup temp dir"
  trap mutation_restore_all EXIT
}

mutation_backup() { # $1 repo-relative file — keep the PRISTINE copy (once per file)
  [ -n "$MUT_BACKUP_DIR" ] || mutation_die "mutation_preflight was not run"
  local bak="$MUT_BACKUP_DIR/$1"
  [ -e "$bak" ] && return 0   # the pristine copy is already kept
  mkdir -p "$(dirname "$bak")"
  cp -- "$1" "$bak" || mutation_die "backup of $1 failed"
}

mutation_restore() { # $1 file — restore from OUR copy, assert byte-identical
  local bak="$MUT_BACKUP_DIR/$1"
  if [ ! -f "$bak" ]; then
    echo "RESTORE-FAILED: no backup copy for $1 — MUTANT LEFT IN PLACE; restore $1 by hand" >&2
    exit 1
  fi
  cp -- "$bak" "$1"
  if cmp -s -- "$1" "$bak"; then
    return 0
  fi
  echo "RESTORE-FAILED: $1 differs from its backup copy after restore — MUTANT LEFT IN PLACE; restore $1 by hand" >&2
  exit 1
}

mutation_restore_all() {
  local bak rel bad=0
  [ -n "$MUT_BACKUP_DIR" ] || return 0
  while IFS= read -r bak; do
    rel="${bak#"$MUT_BACKUP_DIR"/}"
    cmp -s -- "$rel" "$bak" && continue   # already restored byte-identical
    cp -- "$bak" "$rel"
    if cmp -s -- "$rel" "$bak"; then
      echo "restored $rel from the backup copy (byte-identical)"
    else
      echo "RESTORE-FAILED: $rel differs from its backup copy — restore by hand" >&2
      bad=1
    fi
  done < <(find "$MUT_BACKUP_DIR" -type f)
  rm -rf "$MUT_BACKUP_DIR"   # the run's own mktemp dir — the allowed delete
  MUT_BACKUP_DIR=""
  [ "$bad" -eq 0 ] || exit 1
}

patch_file() { # $1 file, $2 old, $3 new → 0 if applied
  python - "$1" "$2" "$3" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
t = open(path, encoding="utf-8").read()
if t.count(old) != 1:
    sys.exit(1)
open(path, "w", encoding="utf-8", newline="").write(t.replace(old, new))
PYEOF
}
