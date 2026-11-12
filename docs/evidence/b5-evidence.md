# B5 live verification evidence (legacy audit writer removal, C8)

- **Commits:** B5 feature `797c494`; verify script + this evidence in the
  final B5/B6 evidence commit
- **Timestamp:** 2026-11-12 06:53:41Z (UTC, green run)
- **Command:** `bash scripts/verify/b5.sh`
- **Environment:** disposable Postgres DB `rag_verify_b5` (dropped after) +
  throwaway app container (compose app image + repo bind mount) on
  `127.0.0.1:8086`, `RUN_MIGRATIONS_ON_STARTUP=true`, `COOKIE_SECURE=false`,
  dev login limit raised. NO `PINECONE_NAMESPACE` override — the script
  proves the legacy paths behave exactly as production-configured (default
  namespace `rag-v2` since B4).
- **Real model usage:** ONE real LLM call (legacy `POST /api/chat`,
  "How do I connect to the company VPN? B5LEAKMARKER-77") + ~2 small
  embedding requests (1 chat-retrieval query, 1 legacy-ingest note).
  LLM-call total for B5: **1** — within the "≤3 real calls" budget for the
  B5+B6 evidence step (B6 uses zero).
- **`data/audit.db` preservation (C8: report, never delete):** the host
  file was independently checked from the host BEFORE the run
  (`sqlite3` row count = **4**, latest `2026-11-09T08:42:11Z`, mtime
  2026-11-09 09:42) and again AFTER: still **4 rows**, latest unchanged,
  mtime unchanged. The in-container view (same file via the repo bind
  mount) agreed at every checkpoint (before chat / after chat / after
  ingest). The script printed the preserved count at start and end.
- **Result: PASS=11 FAIL=0 SKIP=0** — full raw output below.

## What the run proves

1. `app.services.audit` is no longer importable and the `AUDIT_DB_PATH`
   setting is gone — the legacy writer is fully removed, not just disabled.
2. Serving traffic (startup, health, bootstrap-admin, login, one real
   chat, one legacy ingest) creates/writes NOTHING in `data/audit.db`.
3. The real chat's marker (`B5LEAKMARKER-77`) is absent from the response
   trace AND from the app's container logs (F4 log-content reduction holds
   on the live path).
4. The legacy `/api/ingest` still answers 200 end-to-end (parse → chunk →
   embed → Pinecone upsert → document row) while writing no audit rows.

## Pinecone side effects — discovered and cleaned (disclosed per b4 style)

The script's legacy-ingest check intentionally exercises the REAL ingest
path, which upserts into the default namespace `rag-v2` (B5's cleanup does
not touch Pinecone — its subject is the audit DB). Post-run inspection
found `rag-v2` holding 4 vectors, ALL provable test artifacts accumulated
across runs whose scratch DBs died but whose vectors persisted:
- `b5-note.txt` × 3 — from b5.sh runs on 2026-11-09 11:19Z, 2026-11-09
  11:27Z (previous session's attempts) and 2026-11-12 06:53Z (this run;
  the uploads/ tree confirms exactly these three ingest events);
- `bad%00name.txt` × 1 — leftover from F3-era NUL-filename live testing.

All 4 were deleted **by ID** (surgical, never `delete_all`) on
2026-11-12; post-delete `describe_index_stats`: `rag-v2` = **0** vectors
(pristine for the operator's future §A7 re-ingest), legacy namespace
`enterprise-agentic-rag` = **27** vectors (untouched, as mandated).

## Raw output (final green run)

```
==============================================================
B5 LIVE VERIFICATION — 2026-11-12 06:52:51Z — disposable db rag_verify_b5
==============================================================
PASS  disposable app healthy  (expected=200 actual=200)
---- CHECK 1: legacy writer removed
PASS  app.services.audit no longer importable  (expected=GONE actual=GONE)
PASS  AUDIT_DB_PATH setting removed  (expected=ABSENT actual=ABSENT)
---- CHECK 2: nothing (re)creates data/audit.db
      host data/audit.db rows before: 4 (existing file preserved, never deleted)
PASS  no init_db/query_audit activity in app logs  (expected=absent actual=absent)
---- CHECK 3: legacy chat writes no question/trace text anywhere
PASS  bootstrap-admin works (app still healthy)  (found: created)
PASS  legacy chat answered end-to-end (1 real LLM call)  (found: "answer")
PASS  marker absent from the response trace  (absent: B5LEAKMARKER-77)
PASS  marker absent from app logs  (absent: B5LEAKMARKER-77)
PASS  data/audit.db row count UNCHANGED after chat  (expected=4 actual=4)
---- CHECK 4: legacy ingest leaves no audit db write
PASS  legacy ingest succeeded  (expected=200 actual=200)
PASS  data/audit.db row count STILL unchanged after ingest  (expected=4 actual=4)
==============================================================
RESULT: PASS=11 FAIL=0 SKIP=0  (2026-11-12 06:53:41Z)
NOTE: host data/audit.db preserved (4 rows) — report, never delete.
==============================================================
```
