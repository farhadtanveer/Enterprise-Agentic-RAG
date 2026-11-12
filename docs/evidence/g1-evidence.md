# G1 — executed evidence (docs/tasks/g1.md PART 1)

One section per step, appended by the step's own commit (CLAUDE.md).
Live-verification records (g1.sh) join at the verify step.

## G1.1 — Alembic 0005 groups schema (2026-11-12)

Tests FIRST: tests/test_groups_schema.py (red before — `app.models.group`
did not exist, collection error; then the models + migration landed).

Schema delivered (migration `0005`, revises `0004`):
- `groups`: uuidv7 PK; `name` unique among ACTIVE groups only (partial
  unique index `uq_groups_name_active … WHERE is_active`); `kind` CHECK
  `department|project_team`; nullable `contact`; `is_active`.
- `group_memberships`: composite PK (group_id, user_id), both FKs CASCADE;
  `role` CHECK `member|editor` (C1 — no group_editor role anywhere);
  `added_by` FK users SET NULL (attribution without retention); `added_at`.
- `documents`: `scope` CHECK extends to `org|group|private`;
  `group_id` FK groups **ON DELETE RESTRICT**; new `uploaded_by` FK users
  **ON DELETE SET NULL** (backfilled `uploaded_by = owner_id` for existing
  private rows); scope/owner/group CHECKs (`ck_documents_scope_owner_group`)
  enforce org+group ownerless (D16/C2) and private always owned; the
  advisory-dedupe unique extends to `(checksum, scope, owner_id, group_id)`
  so different groups may hold identical files.
- `users.role` untouched (CHECK `IN ('admin','user')` — DB rejects
  'group_editor').

| Check | Expected | Actual | Result |
|---|---|---|---|
| schema tests before implementation | red | collection error (no module) | RED (as designed) |
| schema tests after | all green | 28 passed | PASS |
| full offline suite | 0 fail | **272 passed / 0 failed** | PASS |

What failed first during the step (test bugs, not app bugs): (1) the
unknown-scope probe used `'department'` (10 chars) which died on the
VARCHAR(8) column limit before the CHECK — replaced with `'secret'`; (2)
the C2 test initially asserted via `session.get()`, which serves stale
identity-map objects for rows the DB deleted/updated behind the session
(FK CASCADE/SET NULL are DB-side) — switched to column-level SELECTs; the
same pattern produced `ObjectDeletedError` with `expire_all()`. Both are
test-quality fixes; assertions unchanged in strength.

## G1.2 — AccessResolver + strictly prefixed read_access tokens (2026-11-12)

**DECISION #9 precondition, verified live before the switch:**
`rag-v2` holds 5 vectors, **zero** with bare-UUID/non-prefixed
`read_access` tokens → no vector migration, strict grammar
(`org` / `group:<id>` / `user:<id>`) from day one.

**Delivered:**
- `app/services/access.py`: the `AccessResolver` Protocol exactly as
  §A14.3 + `DbAccessResolver` — `audiences_for` =
  `["org"] + active-group tokens + ["user:<self>"]` recomputed from the DB
  on EVERY call (no cache anywhere); `filter_for` = the same single `$in`;
  `can_read` mirrors the filter in Postgres terms and FAILS CLOSED
  (unknown scope, dangling group, archived group, admin ≠ implicit access);
  `is_editor` per-group off the membership row (C1).
- `conversations.send_message` now builds its filter via the resolver.
  **A §A8 regression this introduced and the fix it forced** (report what
  failed first): building the filter AFTER TX1's commit checked out a
  connection that then spanned the graph run — caught red by the B3 pool
  test `test_tx_discipline_pool_released_during_graph` ("a DB connection
  was held across the graph run"). Fix: the resolver SELECT runs INSIDE
  TX1 (same connection) before the commit, so the commit still releases
  everything and `checkedout()==0` holds during the graph run.
- `vectorstore.add_documents`: strict prefixed tokens; `group_id` param
  added; `ValueError` on group-scope without group_id / private without
  owner. Legacy `/api/chat` keeps the static `ORG_ONLY_FILTER` (D8).

**DECLARED CONTRACT CHANGES to existing tests** (deliberate, per g1.md —
not weakenings; every negative proof keeps its strength):
- tests/security/test_retrieval_acl.py: seed private vector now carries
  `["user:<owner>"]` (was bare UUID); filter pins compare against
  `DbAccessResolver`; admin pin is `["org", "user:<admin>"]`.
- tests/test_documents_b4.py: filter-shape pins + metadata assertion flip
  to the prefixed grammar.

**New tests** (tests/security/test_access_resolver.py, red before the
module existed): audiences for 0/1/2 groups with pinned order
`["org", "group:<g1>", "group:<g2>", "user:<self>]`; revoke applies on the
very next call with the same live user object; member↔editor flips only
editor powers (C1); can_read mirrors + fails closed (dangling group
already impossible to STORE — FK RESTRICT is the schema-level fail-closed,
asserted); archived group in NO audience, readable by no one, re-activation
restores; FULL chat path group isolation (A=G1 vs B=G2: no cross-leak in
content/citations/trace, positive controls on both sides via captured
prompts); admin no-implicit-access until an audited self-join.

| Check | Expected | Actual | Result |
|---|---|---|---|
| resolver tests before implementation | red | collection error (no module) | RED (as designed) |
| touched ACL/B4 tests after update | green | all green | PASS |
| full offline suite | 0 fail | **279 passed / 0 failed** | PASS |

## G1.3 — operator group CLIs (2026-11-12)

Tests FIRST: tests/test_groups_cli.py (red before app/services/groups.py
and the CLI commands existed — 6 failed on import/ClickException).

**Delivered:**
- app/services/groups.py (the seam G1.4's endpoints reuse): create/update/
  delete_group, add/remove_member, set_member_role, get_active_by_name,
  groups_for_user (ACTIVE only — mirrors the resolver), members_of. Every
  change audits (group_created/group_updated/group_deleted/
  membership_added/membership_removed/membership_role_changed) with
  ids/roles only; actor_user_id None for CLI (operator), admin.id for API.
  delete_group REFUSES while documents reference the group (mirrors the
  FK RESTRICT with a human message; the DB constraint is the backstop).
- app/cli.py: `create-group [--kind] [--contact]`, `add-member <group>
  <user> --role member|editor`, `remove-member`, `ingest-group <group>
  <file>` (ownerless group doc per C2; vectors carry the strictly
  prefixed `group:<id>` token via the G1.2 add_documents signature).
  _ingest_one grew group_id/uploaded_by pass-throughs.

| Check | Expected | Actual | Result |
|---|---|---|---|
| group+CLI tests before implementation | red | 6 failed | RED (as designed) |
| after implementation | all green | 6 passed | PASS |
| full offline suite | 0 fail | **285 passed / 0 failed** | PASS |

Two test-side fixes after the red→green pass (assertion mismatches, no
assertion weakened): set_member_role takes role kw-only; the fake
add_documents receives doc.id as UUID (str() both sides).

## G1.4 — admin group endpoints + member-facing routes (2026-11-12)

Tests FIRST: tests/test_groups_api.py (11 failed before the routes
existed — every probe landed on missing-route 404s).

**Delivered (9 routes, all session-auth; the inventory pins them):**
- Admin (`require_admin`): GET/POST `/api/admin/groups`,
  PATCH/DELETE `/api/admin/groups/{id}` (archive = is_active false;
  delete gated on `confirm=true`, 409 while documents reference the group
  — the service refuses and the FK RESTRICT is the backstop),
  POST/PATCH/DELETE `/api/admin/groups/{id}/members[/{user_id}]`
  (duplicate membership → 409, unknown group/user → 404, no-op role
  change → 422, missing membership → 404). Every change audited via the
  G1.3 service with actor=admin.id; a self-join writes
  `membership_added` with actor==target (DECISION #4).
- Member-facing (`get_current_user`): GET `/api/me/groups` (ACTIVE
  memberships only — the resolver's audience rule, mirrored); GET
  `/api/groups/{id}/members` (editor of THAT group → 200; plain member →
  403; outsider, unknown group, or admin-without-membership → 404,
  indistinguishable from nonexistent — §A8 + DECISION #4).
- Route inventory extended CONSCIOUSLY (+9 reviewed entries); the B6
  forced-change gate walks the LIVE route table and covered all 9 new
  routes automatically (gated user → 403 password_change_required).

| Check | Expected | Actual | Result |
|---|---|---|---|
| group+API tests before routes | red | 11 failed | RED (as designed) |
| after implementation | all green | 14 passed (11 + 3 inventory/authz) | PASS |
| full suite, compose mode | 0 fail | **296 passed / 0 failed** | PASS |
| full suite, CI-mode (`ENABLE_WEB_FALLBACK=false`) | 0 fail | **296 passed / 0 failed** | PASS |

What failed first (all reported, none silenced): (1) removed-membership
probes returned 422 (validation mapping) instead of 404 — the endpoints
now check the membership row FIRST (a missing membership is a missing
resource, not a validation error); (2) my own test reused one cookie jar
for two identities (me-groups came back empty for the admin) — separate
per-identity clients, the established B6 rule; (3) the C2 endpoint test
initially tripped the identity map (expired instance attribute access
after an endpoint-side DB delete; UUID-vs-string compare on the row) —
column-level selects + ids captured before deletion; assertions
unchanged in strength.
