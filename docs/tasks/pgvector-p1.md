Read CLAUDE.md and docs/PROGRESS.md first and obey them. Update PROGRESS.md
after every step. Scope: PART 0 (design amendments) and PART 1 (phase P1 of
docs/pgvector-design.md) ONLY. Pinecone cap is exhausted: make NO Pinecone
calls. NEVER run docker compose down -v, delete volumes, or touch the
existing dev database volume; the operator performs all data moves.

DECISIONS (Farhad): #1 derived access, #2 ENABLE RLS without FORCE, #3 the
Protocol takes an AccessContext (user_id or ORG_ONLY), not a token list or a
Pinecone $in dict (the Pinecone adapter builds its own tokens via the
resolver; LangGraph state carries the AccessContext), #4 transitional
vector_store setting accepted, with P10 immediately after P7, #5 migrate
nothing from legacy namespaces, #6 exact search first, #7 keep
text-embedding-3-small, #8 hybrid OFF, #9 remove the A9.1 machinery at P7,
#10 CHANGED: the cutover gate is a golden-question check against pgvector
ALONE (expected source document known for each question; at least 20
questions; recorded in docs/evidence/pgvector-golden.md); parity against
Pinecone is optional and NOT a gate.

PART 0: amend docs/pgvector-design.md (documents only, one commit)
0.1 RLS function: parameters must have distinct names (p_group_id, p_uid,
...) so they can never shadow columns (as written, m.group_id = group_id
compares the column to itself and lets ANY group member read ALL group
documents). Mark it STABLE (not IMMUTABLE) and SECURITY INVOKER; use
NULLIF(current_setting('app.user_id', true), '')::uuid.
0.2 Correct the fail-closed claim: without a user context the app sees ONLY
org rows. Add bypass test: a member of group A cannot read group B's
chunks (this exact test must exist).
0.3 RLS scope: P4 applies RLS to document_chunks only. The documents table
stays under application rules until the admin paths (metadata listing of
org/group documents, delete-any, quota counters, sweeps) are designed.
Write policies (INSERT/UPDATE/DELETE) must be specified explicitly for
document_chunks, with tests that ingest, replace and delete work as the
app role and that cross-audience writes fail.
0.4 Replace token-list with AccessContext everywhere (Protocol, graph state,
legacy chat = ORG_ONLY, search session sets app.user_id when present).
0.5 Orphan files: a report-only CLI listing uploads/<doc_id> directories with
no documents row (never deletes).
0.6 Note the 2000-dimension HNSW limit of the vector type (halfvec if a
larger model is chosen) and the golden-question gate (decision #10).

PART 1: P1 (no application behavior change)
1.1 docker-compose.yml db image and the CI test service image ->
pgvector/pgvector:pg18 (record the tag and digest in the evidence).
1.2 docker/initdb: idempotent SQL that creates the vector extension and the
roles rag_owner and rag_app (no passwords in the repo; passwords come
from environment variables). Fresh volumes only.
1.3 A SEPARATE idempotent bootstrap SQL for an EXISTING volume, run by the
operator with psql, plus docs/db-image-swap.md: a non-destructive
runbook. Alpine and Debian images differ in collation, so do NOT reuse
the old data directory: pg_dump the rag database, point compose at a NEW
named volume, start the new image, restore the dump, then verify.
Include a before/after check of datcollate and datctype, a REINDEX
fallback, and a rollback (the old volume stays untouched).
1.4 Settings: ALEMBIC_DATABASE_URL (owner role) is already supported;
document the owner/app role split. Do not switch DATABASE_URL to rag_app
yet (that is P4).
1.5 Tests: the offline suite stays green in compose mode and CI mode with
the new image. Add a test that the vector extension exists and that its
version is at least 0.8.0 (iterative scans). Evidence in
docs/evidence/pgvector-p1-evidence.md: image tag and digest, extension
version, server version, collation, CI run result.
STOP after PART 1 and report. Do not start P2.
