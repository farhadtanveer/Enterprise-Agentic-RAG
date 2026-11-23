Read CLAUDE.md and docs/PROGRESS.md first and obey them. Update PROGRESS.md
after every step.

TASK: pgvector DESIGN (documents only, no code, no migrations, no data
changes). The Pinecone usage cap is exhausted until 2026-12-01: DO NOT call
Pinecone at all in this task (read the code only). Deliver
docs/pgvector-design.md and commit it.

CONTEXT
We want to replace Pinecone with PostgreSQL + pgvector, because: no external
vendor or cost, data stays in our database, access control can be expressed
in SQL, delete/copy/version operations become single transactions, and live
tests become local and free. The behavior tests (tests/security, the A14.8
pins, B4/G1/G2 behaviors) are backend-agnostic and must run unchanged
against the new backend. Keep the LangGraph topology unchanged.

CONTENTS OF THE DESIGN (each with file:line references where it describes
current code)

1. Inventory of everything that touches Pinecone: vectorstore.py,
   ingestion, retrieval/resolver, add_documents/delete paths, namespaces,
   CLIs, scripts/verify/\*, tests and fakes, settings, Docker, CI. Count what
   changes.
2. Target architecture: a VectorStore Protocol (add chunks for a document,
   delete a document, search by embedding with the user's audiences,
   copy a document's chunks to a new document, replace a document's version)
   and a PgVectorStore implementation. Keep the Pinecone implementation
   until parity is proven, then remove it. Contract tests run against both
   (Pinecone via the existing fakes, pgvector against real Postgres).
3. Schema: document_chunks (document_id FK ON DELETE CASCADE, chunk_index,
   content, page nullable, embedding vector(N), embedding_model,
   embedding_version, generated tsvector, created_at). Decide whether access
   is derived by joining documents/groups/memberships at query time
   (recommended: nothing duplicated, a membership or visibility change is
   instant) or denormalized, with trade-offs.
4. Search and indexes: exact search first (default); when and how to add
   HNSW; recall under restrictive access filters (pgvector iterative scans,
   hnsw.iterative_scan and max_scan_tuples); a measurement plan on synthetic
   data with filters matching 1% and 10% of rows, and a decision rule.
5. Access control in layers: the resolver as SQL, PLUS Postgres Row Level
   Security as a second layer (a non-owner application role, FORCE ROW LEVEL
   SECURITY, SET LOCAL app.user_id per transaction, what that means for the
   connection pool, fail closed when the setting is missing, how migrations
   run with a separate owner role). Include bypass-attempt tests.
6. Hybrid search design: Postgres full-text (German and simple configs) plus
   vector with reciprocal rank fusion; default OFF until an evaluation shows
   a gain.
7. Embedding/provider abstraction: chat and embeddings behind an
   OpenAI-compatible client with base_url, model and dimension in settings;
   embedding_model and version stored per chunk; the procedure for changing
   model or dimension (new column or table, re-embed job from the stored
   originals, dual-read period); a short list of self-hosted candidates
   (BGE-M3, Qwen3-Embedding) with license checks as a TODO. No model switch
   in this task.
8. Migration plan: re-embed from the stored originals (uploads/<doc_id>),
   never from Pinecone; cutover steps; a parity check on a fixed question set
   (top-k overlap) before Pinecone is removed; rollback; what happens to the
   27 legacy vectors and the rag-v2 namespace (operator decision, listed).
9. Operations: pgvector image (pgvector/pgvector:pg18) in compose and CI;
   creating the extension (privileges); maintenance_work_mem and shm-size
   for index builds; backups; sizing for 100k and 1M chunks; connection pool
   sizing; vacuum; monitoring.
10. Effect on G2 Part B: show how share-copy (SQL copy of chunk rows, no
    re-embedding), versions (one transaction swap), review dates and
    citations (page stored per chunk) simplify, and list the edits needed to
    docs/tasks/g2.md Part B. Effect on delete consistency (A9.1), quotas and
    admin listings.
11. Test plan: the contract tests, the recall and latency smoke test on
    100k synthetic vectors with restrictive filters, RLS tests, mutation
    targets. All live checks use the local Postgres only (zero external
    cost).
12. Risks, alternatives (Qdrant) and why not, a phased implementation plan
    P1..Pn with commits and review checkpoints, and a numbered list of
    DECISIONS FOR FARHAD with your recommendation for each.
    STOP after the design and report.
