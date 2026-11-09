# Task: Phase B5 + B6 (with design corrections and small fixes)

Assigned 2026-11-09. Branch: `backend/multi-user`. Resumable session record —
combine this file with `docs/PROGRESS.md` and `git log`.

Read CLAUDE.md first and obey it. Keep docs/PROGRESS.md updated.

## PART 1: DESIGN CORRECTIONS (documents only, one commit)

Update docs/backend-design.md (section A14, A8, A12, A13) with these
DECISIONS and CORRECTIONS. Do not change code in this part.

Decisions (closing §A14.11 #1–#10):

1. Hide private filenames (admin listings: no filenames for private docs).
2. Share = **COPY**: an approved share creates a NEW group document from the
   same stored bytes; the private original stays; the user must give
   explicit consent text.
3. No audience moves in v1 (if ever: delete + re-ingest).
4. No implicit admin access; admin self-join to a group is audited AND
   visible to that group's editors.
5. Admin-managed membership.
6. GROUP_DOC_REVIEW_DAYS=180, per-group override, overdue marker in
   answers, CLI report.
7. Quotas 25/250/50/10 (per-file MB / total MB / doc count / per hour),
   admin exempt, plus a global ingest concurrency limit.
8. In-request ingestion with the parser run in a subprocess with timeout
   and memory limit.
9. Vector token migration: first check the rag-v2 namespace for vectors
   whose read_access holds a bare UUID; if none exist, drop the migration
   and the bare-UUID transition acceptance; if some exist, delete and
   re-ingest.
10. Contact = nullable text, plus ORG_CONTACT fallback for users without
    groups.

Corrections:

- **C1** per-group editors via `group_memberships.role` ('member'|'editor');
  `users.role` stays `user|admin` (no group_editor role).
- **C2** org AND group documents are ownerless (`owner_id NULL`,
  CHECK-enforced); add `uploaded_by` (nullable FK, ON DELETE SET NULL);
  deleting a user never deletes group/org documents.
- **C3** admin may delete ANY document (audited, no content access) but
  cannot update group documents; make A8, A14.7 and the matrix consistent.
- **C4** admin aggregates: system-wide totals (k>=5) plus a list of users
  over or near quota; private documents never appear per-document in admin
  APIs; add a bulk "delete all private documents of user X" endpoint.
- **C5** citations v1 = document name + PDF page + last-updated; section
  citations deferred until heading-aware chunking; review/updated dates are
  read from Postgres in TX2 by doc_id, never frozen in vector metadata.
- **C6** document content update (new version): same doc_id, add new chunks
  first, then delete old ones, audited.
- **C7** step order: B5, B6, G1 (groups, roles, resolver, scopes, admin
  group endpoints), G2 (uploads, quotas, share-copy, versions), B8, B9;
  review checkpoints after G1 and G2.
- **C8** the D2 drop script is obsolete (data/audit.db was lost); remove
  the legacy audit writer and init_db instead.

## PART 2: SMALL FIXES (tests first, one commit each)

- **S1** delete-during-send returns 409 `{"code":"conversation_deleted"}`
  (not 502) and writes the chat_failed audit event.
- **S2** a corrupt/unparseable upload returns 422 with a generic message,
  never 500 (change the test that pins 500).
- **S3** re-run mutation testing on the F1 (symlink), F2 and F6 (magic
  bytes) code; report survivors.

## PART 3: B5

Remove the legacy audit writer (app/services/audit.py usage, init_db,
AUDIT_DB_PATH), so nothing creates data/audit.db. Legacy chat/ingest write
no question/trace text anywhere. If data/audit.db exists, do NOT delete it:
print its row count and tell Farhad. Remove the Semgrep suppressions that
belonged to the old writer. Add a log-content test that forces a DB error
carrying a marker string and proves the marker is absent from logs.

## PART 4: B6

Admin users: create (temp password), disable/enable, role change,
reset-password, delete, and DELETE /api/me. Last-admin guard on EVERY path
(409, including self). Admin documents: metadata-only listing of ORG
documents, org upload, delete any document, bulk delete of one user's
private documents; no per-document view of private documents.
Forced-password-change gate must cover every new route (add to the
route-inventory test). 404 rule everywhere. All admin routes covered by
authorization tests (user gets 403, anonymous 401, admin cannot read any
chat). Audit events for every admin action, no content.

## EVIDENCE AND RULES

One commit per step, push after each green step, offline suite green.
Write scripts/verify/b5.sh and b6.sh, run them live with raw evidence in
docs/evidence/b5-evidence.md and b6-evidence.md (at most 3 real LLM calls).
Never weaken a test. Report what failed first. STOP after B6. Do not start
G1.
