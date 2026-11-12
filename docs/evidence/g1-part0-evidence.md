# G1 PART 0 housekeeping — executed evidence

One section per step (H1–H6), command + expected + actual + PASS/FAIL,
appended by the step's own commit per CLAUDE.md.

## H1 — b5.sh namespace violation fixed (2026-11-12)

**Violation being fixed:** the committed b5.sh ran its throwaway container
with the DEFAULT namespace, so its legacy-ingest check wrote a real vector
into production-namespace `rag-v2` (documented in
docs/evidence/b5-evidence.md; the accumulated junk was purged by id the
same day). This step makes the script CLAUDE.md-compliant:
`PINECONE_NAMESPACE=multi-user-test` on the container + a cleanup trap that
deletes the script's vectors **by id** (this run's `document_id` from the
ingest response plus any prior crashed run's `b5-note.txt` leftovers,
selected via list → fetch → metadata match, never `delete_all`).

**Command:** `bash scripts/verify/b5.sh` (2026-11-12 07:31:14Z → 07:31:51Z)

| Check | Expected | Actual | Result |
|---|---|---|---|
| full script run | 12 PASS / 0 FAIL | 12 PASS / 0 FAIL / 0 SKIP | PASS |
| ingest returns document_id (trap input) | 36-char uuid | uuid | PASS |
| trap by-id cleanup | vectors deleted | `cleanup: deleted 1 vectors by id` | PASS |
| rag-v2 ids before run (external read-only probe) | 0 | 0 | PASS |
| rag-v2 ids after run | 0 (untouched) | 0 | PASS |
| multi-user-test ids after run | 0 (trap cleaned) | 0 | PASS |
| legacy `enterprise-agentic-rag` vector count | 27 (untouched) | 27 | PASS |
| leftover rag-verify-* containers | none | none | PASS |

The B5 subject checks all still hold on the re-run (writer unimportable,
AUDIT_DB_PATH setting gone, audit.db 4 rows before == after with mtime
untouched, chat marker absent from trace + logs, legacy ingest 200).
ONE real LLM call spent (the legacy chat); one embedding call for the
ingest (disclosed).

## H2 — B6 live gaps closed: private-document purge + self-delete (2026-11-12)

**Gap being closed:** b6.sh previously exercised admin purge and
`DELETE /api/me` only against users owning ZERO private documents, and
proved vector deletion with namespace STATS counters. Now: a REAL private
document per scenario via the `ingest-private` CLI (test namespace), and
deletion proved by EXACT vector ids (list → fetch → metadata.doc_id match
→ capture the concrete ids → after deletion `fetch(ids=...)` must return
none of them). Never stats, per CLAUDE.md. The b6 cleanup trap was also
upgraded from `delete_all` to by-id deletion of the script's tracked
document ids (CLAUDE.md: "clean up their own vectors by id in a trap").

**Command:** `bash scripts/verify/b6.sh` (final green run 2026-11-12 07:41:18Z)

| Proof | Expected | Actual | Result |
|---|---|---|---|
| full script run | 0 FAIL | 44 PASS / 0 FAIL / 0 SKIP | PASS |
| ingest-private CLI (purge target) | private doc indexed | `private document <uuid>` | PASS |
| purge-target doc has exact vector ids | ≥1 | ≥1 | PASS |
| admin bulk-purge → 200 | 200 | 200 | PASS |
| purged doc's exact ids absent after purge | 0 present | 0 | PASS |
| purged doc row gone | 0 rows | 0 | PASS |
| self-delete user owns a real private doc | indexed | `private document <uuid>` | PASS |
| `DELETE /api/me` no confirm → 422 / confirm → 200 | 422 / 200 | 422 / 200 | PASS |
| self-deleted user's exact ids absent | 0 present | 0 | PASS |
| self-deleted user's doc + user rows gone | 0 / 0 | 0 / 0 | PASS |
| CHECK 5 org delete-ANY now proved by exact ids | 0 present | 0 | PASS |
| legacy namespace untouched (read-only stats) | 27 = 27 | 27 = 27 | PASS |
| trap cleanup (by id) | namespace empty | `cleanup: namespace already empty` | PASS |
| rag-v2 ids after run | 0 (untouched) | 0 | PASS |

**What failed first:** the first run failed 8 checks with ONE root cause —
`MSYS_NO_PATHCONV=1 docker cp "$TMP/..."` also disabled path conversion for
the HOST argument, so docker.exe looked for `C:\tmp\...` and found nothing;
the CLI error text then flowed into the doc-id extraction and the SQL row
checks (the script's EMPTY/`>=1` guards failed loudly instead of false-
PASSING, as designed). Fix: host side passed as a native Windows path
(`cygpath -w`), conversion disabled only for the container side. No app
code involved. Real model usage: 3 small embedding calls (org + 2 private
notes), ZERO LLM calls.
