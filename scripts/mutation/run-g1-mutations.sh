#!/usr/bin/env bash
# =============================================================================
# scripts/mutation/run-g1-mutations.sh — G1 mutation re-test (g1.md mandate)
#
# Hand-written mutants over the four mandated targets (group audience
# computation, membership check, last-admin lock, group filter), applied
# one at a time to the WORKING TREE, each run against the targeted offline
# suites in the compose test container. A mutant is KILLED when at least
# one test fails; SURVIVED when the selection stays green. Every mutated
# file is restored byte-identical from git afterwards (asserted).
#
# Usage: bash scripts/mutation/run-g1-mutations.sh
# =============================================================================
set -u
cd "$(dirname "$0")/../.."
dc() { MSYS2_ARG_CONV_EXCL="*" docker compose --profile test "$@"; }

SEL="tests/security/test_access_resolver.py tests/security/test_retrieval_acl.py tests/test_groups_api.py tests/test_groups_cli.py tests/test_documents_b4.py"
SEL_LOCK="$SEL tests/security/test_last_admin_race.py tests/test_admin_api.py"

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

run_selection() { # $1 extra test files ("" ok)
  dc run --rm test python -m pytest tests/security/test_access_resolver.py \
    tests/security/test_retrieval_acl.py tests/test_groups_api.py \
    tests/test_groups_cli.py tests/test_documents_b4.py $1 -q --tb=no 2>/dev/null | tail -1
}

mutate() { # $1 id, $2 file, $3 old, $4 new, $5 extra tests
  local id="$1" file="$2" old="$3" new="$4" extra="$5"
  if ! patch_file "$file" "$old" "$new"; then
    echo "M-$id  PATCH-FAILED (anchor not found or ambiguous)"; return
  fi
  local result
  result=$(run_selection "$extra")
  git checkout -- "$file"
  if git diff --quiet -- "$file"; then
    if printf '%s' "$result" | grep -q "failed"; then
      echo "M-$id  KILLED  ($result)"
    else
      echo "M-$id  SURVIVED ($result)"
    fi
  else
    echo "M-$id  RESTORE-FAILED — file differs from git!"; git checkout -- "$file"
  fi
}

echo "=== G1 mutation re-test — $(date -u '+%Y-%m-%d %H:%M:%SZ') ==="
echo "--- baseline (unmutated selection) ---"
run_selection "$SEL_LOCK"

echo "--- mutants ---"
# --- Target 1: the group audience computation (app/services/access.py) ------
mutate A1 app/services/access.py \
  "            .where(
                GroupMembership.user_id == user.id,
                Group.is_active.is_(True),  # archived groups are in NO audience
            )" \
  "            .where(
                GroupMembership.user_id == user.id,
            )" \
  "" # archived groups re-enter the audience

mutate A2 app/services/access.py \
  'return ["org"] + [f"group:{gid}" for gid in group_ids] + [f"user:{user.id}"]' \
  'return ["org"] + [f"group:{gid}" for gid in group_ids]' \
  "" # own private docs become unreachable

mutate A3 app/services/access.py \
  'return ["org"] + [f"group:{gid}" for gid in group_ids] + [f"user:{user.id}"]' \
  'return [] + [f"group:{gid}" for gid in group_ids] + [f"user:{user.id}"]' \
  "" # org docs become unreachable

# --- Target 2: the membership check ------------------------------------------
mutate B1 app/services/access.py \
  "            ).scalar_one_or_none()
            return membership is not None" \
  "            ).scalar_one_or_none()
            return membership is None" \
  "" # can_read group branch inverted

mutate B2 app/services/access.py \
  "                GroupMembership.user_id == user.id,
                GroupMembership.role == \"editor\",
            )" \
  "                GroupMembership.user_id == user.id,
            )" \
  "" # every member counts as editor

mutate B3 app/services/groups.py \
  "    return db.execute(
        select(GroupMembership)
        .where(GroupMembership.group_id == group_id, GroupMembership.user_id == user_id)
    ).scalar_one_or_none()" \
  "    return db.execute(
        select(GroupMembership)
        .where(GroupMembership.group_id == group_id)
    ).limit(1).scalar_one_or_none()" \
  "" # any membership in the group matches any user

# --- Target 3: the last-admin lock (app/services/users.py) -------------------
mutate C1 app/services/users.py \
  "            .where(User.role == \"admin\", User.is_active.is_(True))
            .with_for_update()" \
  "            .where(User.role == \"admin\", User.is_active.is_(True))" \
  "tests/security/test_last_admin_race.py tests/test_admin_api.py" # the H3 race fix removed

mutate C2 app/services/users.py \
  "            .where(User.role == \"admin\", User.is_active.is_(True))
            .with_for_update()" \
  "            .where(User.role == \"admin\")
            .with_for_update()" \
  "tests/security/test_last_admin_race.py tests/test_admin_api.py" # DISABLED admins count toward the guard

# --- Target 4: the group filter ----------------------------------------------
mutate D1 app/services/access.py \
  '    def filter_for(self, user: User) -> dict:
        return {"read_access": {"$in": self.audiences_for(user)}}' \
  '    def filter_for(self, user: User) -> dict:
        return {"read_access": {"$in": ["org"]}}' \
  "" # static org-only filter (drops groups + private)

mutate D2 app/api/conversations.py \
  "    retrieval_filter = DbAccessResolver(db).filter_for(user)" \
  "    from app.services.retrieval import ORG_ONLY_FILTER
    retrieval_filter = ORG_ONLY_FILTER" \
  "" # chat wiring regresses to org-only

echo "=== done — $(date -u '+%Y-%m-%d %H:%M:%SZ') ==="
git status --short
