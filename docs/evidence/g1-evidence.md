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
