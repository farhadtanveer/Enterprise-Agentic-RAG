# B4 live verification evidence (Part 3)

- **Commits:** B4 feature `e7d76a8`; live-found production fixes in the
  `fix(b4)` commit (vectorstore.py); verify script + this evidence in the
  `verify(b4)` commit
- **Timestamp:** 2026-11-05 12:11:42Z (UTC, final green run)
- **Command:** `bash scripts/verify/b4.sh`
- **Environment:** disposable Postgres DB `rag_verify_b4` + throwaway app
  container (compose app image + repo bind mount) on `127.0.0.1:8082` with
  **`PINECONE_NAMESPACE=multi-user-test`** — every Pinecone write in the
  script targets the dedicated test namespace. The legacy namespace
  (`enterprise-agentic-rag`, 27 vectors) was only ever READ
  (`describe_index_stats` + one filter query for the fail-closed proof).
  After the run the script deletes the test namespace
  (`delete(delete_all=True, namespace='multi-user-test')`); a post-run probe
  confirmed only the legacy namespace remains (27 vectors, unchanged).
- **Real model usage:** ONE real chat request (user A through the full
  graph path) + ~8 small embedding requests (2 ingests, 3 retriever-level
  ACL queries, 1 fail-closed query, 2 idempotency upserts). LLM-call total
  across the whole task: 4 (3 in B3, 1 in B4) — within the per-part "max 3
  real calls"; embedding API requests counted separately (disclosed
  deviation: live per-document vector verification is impossible without
  real embeddings).
- **Result: PASS=26 FAIL=0 SKIP=0** — full final output below.

## Two REAL production bugs found live (the mandate working as intended)

1. **First ingest into an empty namespace crashed.** `add_documents()` runs
   a replace-semantics `index.delete(filter={doc_id})` before upserting;
   Pinecone answers **404 "Namespace not found"** for a namespace with no
   vectors. The very first ingest into a fresh namespace (exactly the
   rag-v2 migration's first step!) raised `NotFoundException` and failed.
   Fix: treat the 404 as the no-op the delete already is, by contract
   (`app/rag/vectorstore.py`, both delete sites). Regression test:
   `test_first_ingest_into_empty_namespace_survives_pinecone_404`.
2. **Every ORG document ingest failed against real Pinecone.** Chunk
   metadata carried `owner_id=None` for org docs; Pinecone rejects null
   metadata values ("Metadata value must be a string, number, boolean or
   list of strings, got 'null' for field 'owner_id'"). Legacy `/api/ingest`
   and `ingest-org` were broken against the real service while the offline
   fake store passed. Fix: org chunks **omit** the key entirely — D16
   ownerlessness lives in the Postgres CHECK, access control rides solely
   on `read_access`. Test updated to assert key-absence.

Plus one verifier-side defect found by inspection: the legacy-namespace
count check initially compared keys instead of counts (grep pattern missed
the space after the colon) — it passed degenerately once, was fixed, and
the whole part was re-run green.

## Honest history of the B4 runs (rule 4: what failed first)

| Run | Result | Cause |
|---|---|---|
| 1–3 | mass FAIL | Verifier environment: MSYS path mangling (`/tmp/orgdoc` → `C:\…\Temp\orgdoc` before docker saw it), a leftover debug container blocking bring-up, silent output capture. **Also: the FIRST real ingest crashed on the namespace-404 bug above.** |
| 4 | 17/10 | Namespace-404 fixed live; org ingest STILL failed → exposed the null-metadata 400. |
| 5–9 | partial | Instrumentation + fixture defects: my rewrite dropped the host→container cp for A's file; A's file named `policy-notes-A.txt` so the same-filename rows never matched; idempotency helper coupled to the (then failing) org doc_id + hit Pinecone's eventual-consistency window; the global MSYS exclusion broke curl's cookie-jar paths (jar written to `C:\tmp\…`, read from `/tmp/…` → 401s). |
| 10/final | **26/0** | All green with honest comparisons. |

## Full final output

```
==================================================================
B4 LIVE VERIFICATION — 2026-11-05 12:11:42Z — disposable db rag_verify_b4 — namespace multi-user-test
==================================================================
PASS  disposable app healthy  (expected=200 actual=200)
      old-namespace baseline: {"enterprise-agentic-rag": 27}
      bootstrap: Admin 'verifyboss4' created (id 01a1ab92-ce69-79cd-ab4f-d6bb36b91e87).
      users: users ok
---- CHECK ingest: org doc + private doc for A (real embeddings, test namespace)
      org ingest raw: indexed vpn-handbook.txt → org document 01a1ab93-0112-7004-851f-def7b8748e4c
PASS  org doc indexed  (found: org document)
      A ingest raw: indexed policy-notes.txt → private document 01a1ab93-5b3d-7017-9d92-5f74aee85ba4 (owner checkb4usera)
PASS  private doc A indexed  (found: private document)
PASS  test namespace populated (org + A private vectors)  (expected=YES actual=YES)
      vector ids in test namespace after org+A ingest: 2
---- CHECK same-filename upload by two owners
      B ingest raw: indexed policy-notes.txt → private document 01a1ab93-b38e-7ee0-bf87-9dacb493fae8 (owner checkb4userb)
PASS  two docs share display_name, distinct ids  (expected=2 actual=2)
PASS  distinct document ids for same filename  (expected=2 actual=2)
PASS  same filename two owners: vector count grew by exactly B's chunks  (expected=3  actual=3 )
---- CHECK retrieval ACL: user A full chat path (REAL graph call)
      login Set-Cookie: set-cookie: sid=<redacted>; HttpOnly; Max-Age=2592000; Path=/; SameSite=lax;set-cookie: csrf=<redacted>; Max-Age=2592000; Path=/; SameSite=lax;
      A chat raw (first 200 chars): {"id":"01a1ab94-…","role":"assistant","status":"ok","content":"To get access to the finance policy system, follow the…
PASS  A's chat succeeded through the graph  (found: "status":"ok")
PASS  A's answer cites THEIR private doc  (found: policy-notes)
      A's chat source_used: "source_used":"private_kb"
PASS  A's chat used the private KB  (found: private_kb)
---- CHECK retrieval ACL: user B + legacy org-only + fail-closed (retriever-level)
      B/A retrieval raw: B-DOCS ['01a1ab93-0112-…(org)', '01a1ab93-…(B)'];A-DOCS ['01a1ab93-…(org)', '01a1ab93-…(A)'];
PASS  B's retrieval includes the org doc  (found: 01a1ab93-0112-7004-851f-def7b8748e4c)
PASS  B's retrieval EXCLUDES A's private doc  (absent: 01a1ab93-5b3d-7017-9d92-5f74aee85ba4)
PASS  A's retrieval includes their private doc  (found: 01a1ab93-5b3d-7017-9d92-5f74aee85ba4)
PASS  A's retrieval excludes B's private doc  (absent: 01a1ab93-b38e-7ee0-bf87-9dacb493fae8)
PASS  legacy org-only filter sees the org doc  (found: 01a1ab93-0112-7004-851f-def7b8748e4c)
PASS  legacy org-only filter NEVER sees private docs  (absent: 01a1ab93-5b3d-7017-9d92-5f74aee85ba4)
PASS  fail-closed: legacy namespace vectors (no read_access) match NOTHING under the filter  (expected=0 actual=0)
---- CHECK re-ingest idempotency
      idempotency raw: IDEM 1 1 4 4
PASS  re-ingest writes land under the same doc_id  (expected=1 actual=1)
PASS  re-ingest is idempotent (id set unchanged after double write)  (expected=1 actual=1)
---- CHECK delete: vectors-first protocol removes ONLY A's vectors
PASS  A's private doc deleted via §A9.1 protocol  (found: deleted A doc)
PASS  none of A's recomputed vector ids remain  (expected=0 actual=0)
PASS  only A's vectors removed (count dropped by exactly A's chunk_count)  (expected=3 actual=3)
PASS  B's same-name doc still fully visible after A's delete  (expected=True actual=True)
---- CHECK oversize upload -> 413
PASS  legacy ingest 26MB (> MAX_UPLOAD_MB=25) -> 413  (expected=413 actual=413)
PASS  no document row left behind  (expected=0 actual=0)
---- CHECK old namespace untouched
      old-namespace after: {"enterprise-agentic-rag": 27, "multi-user-test": 3}
PASS  legacy namespace vector count unchanged (test namespace may legitimately appear)  (expected="enterprise-agentic-rag": 27 actual="enterprise-agentic-rag": 27)
RESULT: PASS=26 FAIL=0 SKIP=0  (2026-11-05 12:11:42Z)
==================================================================
```

Post-run namespace probe (after the script's cleanup):
`NAMESPACE COUNTS: {'enterprise-agentic-rag': 27}` — the test namespace is
fully deleted; the legacy namespace untouched (27 before → 27 after).

## Mandate-item → evidence map (Part 3)

| Item | Evidence |
|---|---|
| Upload size cap (M4) → 413 | 26 MB upload → 413, no document row left behind (final block) |
| DEDICATED TEST NAMESPACE only | All containers set `PINECONE_NAMESPACE=multi-user-test`; legacy namespace count 27 → 27; cleanup deletes test-namespace data (verified by post-run probe) |
| Retrieval filter built server-side; fail closed | `get_retriever` filter-REQUIRED (offline test) + live: legacy-namespace vectors WITHOUT `read_access` match nothing under the filter (0 hits) |
| Legacy /api/chat uses org-only filter | Offline: `test_legacy_chat_sends_org_only_filter` (captures the exact filter at the ask boundary); live retriever-level: org-only returns the org doc, never private docs |
| A retrieves private doc through FULL chat path | Live: status ok, source_used private_kb, citations name policy-notes.txt (real graph, 1 call) |
| B does not retrieve it | Live retriever-level with the production filter builder: B's results contain org+B only |
| Same filename two owners, no collision | Two rows `policy-notes.txt`, distinct doc ids; vector count grew by exactly B's chunks; B still visible after A's delete |
| Deleting A's doc removes only A's vectors | §A9.1 protocol delete; A's recomputed chunk ids (app's own `_chunk_id`) absent from the namespace; count dropped by exactly A's chunk_count |
| Re-ingest idempotent | Same doc_id double-write: identical ids, stable id set, stable counts (IDEM 1 1 4 4) |
| Per-document verification, NOT stats | All document-level checks enumerate/recompute explicit vector ids; stats used only for the legacy-namespace-untouched comparison |
