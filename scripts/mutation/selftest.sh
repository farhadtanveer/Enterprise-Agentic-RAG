#!/usr/bin/env bash
# =============================================================================
# scripts/mutation/selftest.sh — A5.3 self-test for the mutation harness
# machinery, run against a THROWAWAY sandbox git repo (offline: no docker,
# no pytest). Proven here:
#   S1  every mutation script passes bash -n;
#   S2  the shared lib exists and both harnesses source it;
#   S3  the harnesses are source-safe (main run guarded by BASH_SOURCE) —
#       sourcing them must NEVER start a mutant run;
#   S4  a modified tracked file refuses the run (target left untouched);
#   S5  an untracked file refuses the run (uncommitted work is never
#       discardable);
#   S6  a staged-but-uncommitted change refuses the run;
#   S7  restore is byte-identical to the PRISTINE BACKUP COPY, never from
#       git — HEAD deliberately holds different bytes than the working
#       content, so a git-restore would be detectably wrong;
#   S8  a missing backup copy fails loudly and leaves the file untouched
#       (never a silent git fallback);
#   S9  an interrupted run (EXIT trap) restores every backed-up file.
# No `git checkout`/`git restore`/`git stash` may appear in any harness
# (S10) — restores come from our own copies only.
#
# Run: bash scripts/mutation/selftest.sh     (exit 0 = all checks passed)
# =============================================================================
set -u
cd "$(dirname "$0")/../.."

PASS=0; FAIL=0
ck() { local d="$1" e="$2" a="$3"
  if [ "$e" = "$a" ]; then echo "PASS  $d"; PASS=$((PASS+1))
  else echo "FAIL  $d (expected=[$e] actual=[$a])"; FAIL=$((FAIL+1)); fi; }
ckc() { local d="$1" n="$2" h="$3"
  if printf '%s' "$h" | grep -q "$n"; then echo "PASS  $d"; PASS=$((PASS+1))
  else echo "FAIL  $d (missing: $n)"; FAIL=$((FAIL+1)); fi; }
note() { echo "---- $*"; }

G2A=scripts/mutation/run-g2a-mutations.sh
G1=scripts/mutation/run-g1-mutations.sh
LIB=scripts/mutation/_lib.sh

# --- S1: syntax ----------------------------------------------------------------
note "S1: bash -n"
for f in "$G2A" "$G1" "$LIB" scripts/mutation/selftest.sh; do
  if [ -f "$f" ] && bash -n "$f" 2>/dev/null; then
    echo "PASS  bash -n $f"; PASS=$((PASS+1))
  else
    echo "FAIL  bash -n $f"; FAIL=$((FAIL+1))
  fi
done

# --- S2: the shared lib exists and is sourced by both harnesses ----------------
note "S2: shared lib"
if [ -f "$LIB" ]; then echo "PASS  lib exists"; PASS=$((PASS+1))
else echo "FAIL  lib missing: $LIB"; FAIL=$((FAIL+1)); fi
for f in "$G2A" "$G1"; do
  if grep -q 'source.*mutation/_lib\.sh' "$f"; then
    echo "PASS  $f sources the lib"; PASS=$((PASS+1))
  else
    echo "FAIL  $f does not source the lib"; FAIL=$((FAIL+1))
  fi
done

# --- S3: source-safety guard (sourcing must never start a mutant run) ----------
note "S3: main-run guard"
for f in "$G2A" "$G1"; do
  if grep -q 'BASH_SOURCE\[0\]' "$f"; then
    echo "PASS  $f has a BASH_SOURCE main guard"; PASS=$((PASS+1))
  else
    echo "FAIL  $f runs its mutants when sourced — NOT source-safe"; FAIL=$((FAIL+1))
  fi
done

# --- S10: restores must never come from git -------------------------------------
note "S10: no git-restore commands in any harness"
HITS=$(cat "$G2A" "$G1" "$LIB" | grep -nE 'git (checkout|restore|stash)' || true)
if [ -z "$HITS" ]; then echo "PASS  no git checkout/restore/stash"; PASS=$((PASS+1))
else echo "FAIL  git-restore commands found: $HITS"; FAIL=$((FAIL+1)); fi

# --- sandbox repo ---------------------------------------------------------------
SB=$(mktemp -d)   # ours for this session; removed in the EXIT trap
trap 'rm -rf "$SB"' EXIT
git init -q -b main "$SB/repo"
git -C "$SB/repo" config user.email selftest@example.com
git -C "$SB/repo" config user.name selftest
mkdir -p "$SB/repo/scripts/mutation" "$SB/repo/app/services"
cp "$G2A" "$SB/repo/scripts/mutation/"
cp "$G1" "$SB/repo/scripts/mutation/"
cp "$LIB" "$SB/repo/scripts/mutation/" 2>/dev/null || true
TARGET=app/services/target.py
printf 'ORIGINAL line one\nline two\n' > "$SB/repo/$TARGET"
git -C "$SB/repo" add -A
git -C "$SB/repo" commit -qm "sandbox seed"

harness_refusal() { # $1 harness → "exit_code|REFUSING?"
  local out rc
  out=$(cd "$SB/repo" && bash "scripts/mutation/$(basename "$1")" 2>&1); rc=$?
  printf '%s|%s' "$rc" "$(printf '%s' "$out" | grep -c 'REFUSING')"
}

# --- S4: dirty tracked tree refused ---------------------------------------------
note "S4: modified tracked file refuses"
printf 'dirty line\n' >> "$SB/repo/$TARGET"
for f in "$G2A" "$G1"; do
  ck "$(basename "$f") refuses modified tree" "nonzero|1" \
    "$(harness_refusal "$f" | awk -F'|' '{print ($1!="0"?"nonzero":"zero")"|"$2}')"
done
ck "target untouched by the refusals" "ORIGINAL line one
line two
dirty line" "$(cat "$SB/repo/$TARGET")"
git -C "$SB/repo" checkout -- "$TARGET"   # sandbox-only reset of OUR dirty line

# --- S5: untracked file refuses ---------------------------------------------------
note "S5: untracked file refuses"
printf 'untracked\n' > "$SB/repo/stray.txt"
for f in "$G2A" "$G1"; do
  ck "$(basename "$f") refuses untracked file" "nonzero|1" \
    "$(harness_refusal "$f" | awk -F'|' '{print ($1!="0"?"nonzero":"zero")"|"$2}')"
done
rm "$SB/repo/stray.txt"

# --- S6: staged-but-uncommitted change refuses -----------------------------------
note "S6: staged change refuses"
printf 'staged line\n' >> "$SB/repo/$TARGET"
git -C "$SB/repo" add "$TARGET"
for f in "$G2A" "$G1"; do
  ck "$(basename "$f") refuses staged change" "nonzero|1" \
    "$(harness_refusal "$f" | awk -F'|' '{print ($1!="0"?"nonzero":"zero")"|"$2}')"
done
git -C "$SB/repo" reset -q "$TARGET"
git -C "$SB/repo" checkout -- "$TARGET"

# --- S7: restore comes from the PRISTINE BACKUP COPY, never from git -------------
note "S7: backup→restore round-trip (working content differs from HEAD on purpose)"
# Source the harness INTO this shell (safe since S3: the main run is
# guarded). It cd's to the sandbox repo; every later path is absolute.
# shellcheck disable=SC1090,SC1091
source "$SB/repo/scripts/mutation/run-g2a-mutations.sh"
if [ -n "$(declare -f mutation_backup)" ] && [ -n "$(declare -f mutation_restore)" ]; then
  echo "PASS  harness sourced; lib functions present"; PASS=$((PASS+1))
else
  echo "FAIL  harness sourcing or lib functions broken"; FAIL=$((FAIL+1))
fi
BAK="$SB/manual-bak"; mkdir -p "$BAK"
printf 'PRISTINE working content (differs from HEAD on purpose)\n' > "$SB/repo/$TARGET"
MUT_BACKUP_DIR="$BAK" mutation_backup "$TARGET"
printf 'MUTANT line\n' > "$SB/repo/$TARGET"
MUT_BACKUP_DIR="$BAK" mutation_restore "$TARGET"
ck "restored bytes == pristine COPY (git HEAD holds different bytes)" \
  "PRISTINE working content (differs from HEAD on purpose)" "$(cat "$SB/repo/$TARGET")"

# --- S8: missing backup fails loudly, file untouched ------------------------------
note "S8: missing backup copy"
rm -rf "$BAK"; mkdir -p "$BAK"
printf 'MUTANT8\n' > "$SB/repo/$TARGET"
# command substitution = subshell: mutation_restore's loud exit ends the
# subshell (not this selftest) and its status becomes the substitution's
S8=$(MUT_BACKUP_DIR="$BAK" mutation_restore "$TARGET" 2>&1); S8RC=$?
ckc "loud RESTORE-FAILED" "RESTORE-FAILED" "$S8"
ck "nonzero exit" "nonzero" "$([ "$S8RC" -ne 0 ] && echo nonzero || echo zero)"
ck "file left untouched for manual recovery" "MUTANT8" "$(cat "$SB/repo/$TARGET")"

# --- S9: EXIT trap restores an interrupted run -------------------------------------
note "S9: EXIT trap restores interrupted runs"
git -C "$SB/repo" checkout -- "$TARGET"   # discard S8's deliberate mutant
printf 'PRISTINE working content (differs from HEAD on purpose)\n' > "$SB/repo/$TARGET"
git -C "$SB/repo" add -A && git -C "$SB/repo" commit -qm "pristine content"
S9OUT=$(bash -c '
  cd "$1/repo" || exit 9
  source scripts/mutation/run-g2a-mutations.sh || exit 9
  mutation_preflight || exit 9
  echo "BAKDIR=$MUT_BACKUP_DIR"
  mutation_backup "'"$TARGET"'" || exit 9
  printf "MUTANT9\n" > "'"$TARGET"'"
  exit 3   # interrupted mid-mutant — the trap must restore
' _ "$SB")
S9RC=$?
ck "interrupted run exited nonzero" "nonzero" "$([ "$S9RC" -ne 0 ] && echo nonzero || echo zero)"
ck "EXIT trap restored the mutant from the backup copy" \
  "PRISTINE working content (differs from HEAD on purpose)" "$(cat "$SB/repo/$TARGET")"
S9BAK=$(printf '%s\n' "$S9OUT" | sed -n 's/^BAKDIR=//p' | head -1)
ck "backup temp dir removed by the trap" "gone" \
  "$([ -n "$S9BAK" ] && [ ! -e "$S9BAK" ] && echo gone || echo "present($S9BAK)")"

echo "=============================================================="
echo "RESULT: PASS=$PASS FAIL=$FAIL"
echo "=============================================================="
[ "$FAIL" -eq 0 ]
