#!/usr/bin/env bash
# =============================================================================
# scripts/verify/purge-multi-user-test.sh — one-off hygiene: purge the
# accumulated test-artifact vectors from the SHARED multi-user-test
# namespace (A5.4: the 55,597 leftovers a killed verify run's failed trap
# left behind — read-only census taken 2026-11-19).
#
# Guard rails (CLAUDE.md):
#   - ONLY the multi-user-test namespace is touched; the name is checked
#     against a literal allowlist before any write;
#   - vectors are deleted BY ID, never delete_all, never delete_index;
#   - CHUNK=100: Pinecone fetch is GET-based — a 500-id fetch already
#     414s (Request-URI Too Large, reproduced live 2026-11-19; the same
#     failure class that let the leftovers accumulate past the 2026-11-16
#     cleanup);
#   - a chunk is deleted ONLY after a fetch confirms EVERY id in it carries
#     a metadata doc_id (the test-artifact signature) — any metadata-less
#     vector aborts the purge with a report;
#   - per-chunk census (source_name counts) is aggregated for the report;
#   - a final describe_index_stats re-count closes the evidence.
#
# Usage: bash scripts/verify/purge-multi-user-test.sh
# =============================================================================
set -u
cd "$(dirname "$0")/../.."
MSYS_NO_PATHCONV=1 docker run --rm -i -v "$(cygpath -w "$PWD"):/app:ro" \
  --entrypoint python enterprise-agentic-rag-app:latest - <<'PYEOF'
import sys
from collections import Counter
from app.rag.vectorstore import ensure_index

NS = "multi-user-test"
ALLOWED = {"multi-user-test"}
assert NS in ALLOWED, f"namespace {NS!r} not allowlisted"
idx = ensure_index()
stats = idx.describe_index_stats()
before = (stats.namespaces or {}).get(NS)
before = before.vector_count if before else 0
print(f"census: {NS} holds {before} vectors — purging test artifacts by id")

ids = []
for page in idx.list(namespace=NS):
    ids.extend([v if isinstance(v, str) else v.id
                for v in (page if isinstance(page, list) else page.vectors)])
print(f"listed: {len(ids)} ids")

by_src = Counter()
by_doc = Counter()
refused = 0
purged = 0
CHUNK = 100   # fetch is GET-based: 500 ids already 414 (reproduced live)
for i in range(0, len(ids), CHUNK):
    batch = ids[i:i + CHUNK]
    fetched = idx.fetch(ids=batch, namespace=NS).vectors
    # fail-closed: delete a chunk only when EVERY id proves it is a
    # test artifact (metadata with a doc_id — the verify-script signature)
    missing = [vid for vid in batch if vid not in fetched
               or not (fetched[vid].metadata or {}).get("doc_id")]
    if missing:
        refused += len(missing)
        print(f"ABORT chunk at {i}: {len(missing)} ids without doc_id metadata "
              f"(first: {missing[:3]}) — nothing deleted from this chunk")
        continue
    for v in fetched.values():
        m = v.metadata or {}
        by_src[str(m.get("source_name"))] += 1
        by_doc[str(m.get("doc_id"))] += 1
    idx.delete(ids=batch, namespace=NS)
    purged += len(batch)
    print(f"purged {purged}/{len(ids)} (chunk of {len(batch)} deleted by id)")

print("--- census of the purged artifacts ---")
print("by source_name (top 20):", dict(by_src.most_common(20)))
print("distinct doc_ids:", len(by_doc))
print("refused (metadata-less, left in place):", refused)

stats = idx.describe_index_stats()
after = (stats.namespaces or {}).get(NS)
after = after.vector_count if after else 0
print(f"final count: {before} -> {after}")
PYEOF
echo "done."
