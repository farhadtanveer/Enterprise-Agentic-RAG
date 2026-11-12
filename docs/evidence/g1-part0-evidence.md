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
