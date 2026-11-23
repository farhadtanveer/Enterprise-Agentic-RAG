# pgvector Migration Design — replacing Pinecone with PostgreSQL + pgvector

Task: docs/tasks/pgvector-a.md (design only — no code, no migrations, no data
changes, **zero Pinecone calls**; the usage cap is exhausted until
2026-12-01, so this document was written from code reading only).
Amends docs/backend-design.md (§A7, §A9.1) and docs/tasks/g2.md (Part B).
Date: 2026-11-23 (PART 0 amendments applied same day — Farhad's DECISIONS
#1–#10 recorded in §12.4; #3 became the AccessContext seam, #10 CHANGED the
cutover gate to a golden-question check against pgvector alone).

**Why (from the task, with what the code confirms):** no external vendor or
cost; data stays in our database; access control becomes SQL; delete/copy/
version operations become single transactions (today §A9.1 exists *only*
because deletion spans two systems — docs/backend-design.md:741-746); live
tests become local and free (today every verify script spends Pinecone
reads/writes and the account egress cap is exhausted — g2a5-evidence.md
§A5.4). The A14.8 behavior tests are backend-agnostic and must run
unchanged; the LangGraph topology stays.

A security win the task doesn't name but D12 does: private-scope isolation
is currently *metadata on a shared Pinecone index* — "anyone with the
Pinecone API key can read chunk text of private docs"
(docs/backend-design.md:589-593). Under pgvector, private chunk text lives
in our Postgres behind the §5 controls; the Pinecone credential class of
leak disappears.

---

## 1. Inventory of everything that touches Pinecone

Only ONE file imports the Pinecone SDK. Everything else reaches Pinecone
through its four functions. Counts at the end.

### 1.1 Production code

| File | Refs | What it does |
|---|---|---|
| `app/rag/vectorstore.py` | 24 | THE hub. Imports `Pinecone, ServerlessSpec, NotFoundException` (:3-5), `OpenAIEmbeddings` (:6), `PineconeVectorStore` (:7). `EMBEDDING_DIMENSIONS` map (:18-23) + `get_embedding_dimension()` (:27-47). `get_embeddings()` — OpenAIEmbeddings singleton (:51-60). `ensure_index()` (:65-104) — creates the serverless index (cosine, aws us-east-1 :93), the NEVER-auto-delete dimension guard (:79-86), ready-wait with `index_ready_timeout_seconds` (:95-102). `get_vectorstore()` (:110-119) — PineconeVectorStore bound to `pinecone_namespace`. `get_retriever(filter)` (:123-136) — filter-REQUIRED (§A7), `as_retriever(search_kwargs={"k": settings.top_k, "filter": ...})`. `_chunk_id()` (:140-148) — deterministic `sha256(doc_id \0 start_index \0 content)`. `add_documents()` (:151-199) — builds the strictly-prefixed `read_access` tokens (:164-173), stamps `doc_id/scope/read_access/source` on every chunk (:174-180), omits `owner_id` for org (Pinecone rejects nulls — live-found 2026-11-05, :182-186), replace = delete-by-doc_id BEFORE add (:187-198), then `store.add_documents(chunks, ids=...)` (:199). `delete_document_vectors()` (:202-211) — idempotent delete-by-filter `{"doc_id": {"$eq": ...}}`. |
| `app/services/retrieval.py` | 1 | `ORG_ONLY_FILTER = {"read_access": {"$in": ["org"]}}` (:14) — the legacy-chat filter (D8). The whole module exists to hold this Pinecone-shaped dict. |
| `app/services/access.py` | 1+ | `AccessResolver` Protocol (:27-47) — ALREADY backend-agnostic ("the same rules run against Pinecone now and pgvector later"). `DbAccessResolver.filter_for()` (:68-69) renders the Pinecone `$in` dict from `audiences_for()` (:56-66). `can_read()` (:71-91) is pure-Postgres already. |
| `app/core/config.py` | 3 | `pinecone_api_key` (:19), `pinecone_index_name` (:20), `pinecone_namespace` default `rag-v2` (:24). Related: `embedding_model` (:25), `openai_model` (:26), `top_k` (:27), `index_ready_timeout_seconds` (:29). The settings-audit reference test (tests/test_settings_audit.py, empty allowlist) requires every setting to be READ by production code — removals and additions must keep it green. |
| `app/rag/workflow.py` | 2 | `retrieve_kb` node calls `get_retriever(state["retrieval_filter"])` (:12, :82) — the ONLY graph node touching vectors; topology unchanged. `ask()` threads the filter through state (:289-310); `app/rag/state.py:27` declares `retrieval_filter: Optional[dict]`. Citations read `metadata["source"]` (:185-193) — that metadata contract is what the new store must reproduce. |
| `app/api/routes.py` | 2 | Legacy chat: `ORG_ONLY_FILTER` → `ask()` (:10, :23, :58). Legacy ingest: `add_documents(...)` under `ingest_gate` (:15, :160, :179). Dies at B9 anyway. |
| `app/api/conversations.py` | 1 | `retrieval_filter = DbAccessResolver(db).filter_for(user)` built INSIDE TX1 (:226), commit (:227), then `ask(...)` (:233) — §A8 connection discipline. |
| `app/api/documents.py` | 3 | Imports (:38), user upload `add_documents` under `ingest_gate` (:272, :288), delete: TX1 begin (:383-384) → `delete_document_vectors` outside TX (:385) → TX2 (:386-387). |
| `app/api/admin.py` | 4 | Imports (:34, :44), purge-private loop deletes vectors per doc (:244, :270), admin org upload: TX1 comment + commit (:450-465) → `ingest_gate` + `add_documents` (:471, :487) → TX2 (:500-507), admin delete-ANY (:524-531). |
| `app/api/auth.py` | 2 | `DELETE /api/me`: `delete_document_vectors` per private doc before the user row goes (:214, :226). |
| `app/cli.py` | 3 | `_ingest_one` → `add_documents` (:109, :155) serving ingest-org/ingest-private/ingest-group; `documents-retry-deleting` (:297, :302); `delete-document` (:334, :349). Docstring "Pinecone outage" (:296, :331). |
| `app/services/documents.py` | 3 | §A9.1 machinery whose REASON is the two-system split: `begin_delete` (:205-208), `complete_delete` (:268-279), `list_stuck_deletes`/`retry_stuck_deletes` (:282-298, takes a `delete_vectors` CALLABLE — already dependency-injected). Docstrings name the Pinecone-outage failure mode (:9-12, :289-291). Quota accounting (`QUOTA_COUNTED_STATUSES` :53, `enforce_private_quota` :56-85, `sweep_stale_pending` :301-331) does NOT touch vectors. |
| `Dockerfile` | 0 | Nothing Pinecone-specific (pip install of requirements only). |
| `templates/index.html` | 1 | UI copy "and indexed into Pinecone" (:103) — cosmetic, dies with the docs sweep. |
| `Agentic-RAG-demo/` | 24+ | Standalone demo notebook (out of project scope) — UNTOUCHED, listed for completeness. |

### 1.2 Tests and fakes (the offline suite is already Pinecone-free)

`tests/conftest.py` pins `PINECONE_API_KEY=""` (:14) and
`PINECONE_NAMESPACE=multi-user-test` (:31 — belt-over-suspenders so even a
bug cannot write rag-v2). No test talks to the real service. Three fake
SEAMS are in use:

| Seam (what tests monkeypatch) | Files | Fate under the Protocol |
|---|---|---|
| Route/CLI-level `add_documents` / `delete_document_vectors` | test_admin_key.py:43, test_admin_api.py:364/429/481, test_documents_api.py:70/332/351, test_parser_subprocess.py:201, test_upload_quota_race.py:57, test_upload_pool_discipline.py:109/138, test_quota_accounting.py:56/169, test_user_upload_storage_rules.py:42/127/151, test_upload_quotas.py:45/151, test_groups_api.py:356, test_groups_cli.py:177, test_cli_delete_document.py:45/97 (12 files) | **UNTOUCHED** if the compatibility function names survive (they do — §2.3). |
| `wf.get_retriever` (graph seam) | test_graph_routing.py ×6, test_prompt_injection.py:16, test_retrieval_acl.py:154 (3 files) | Survives if `get_retriever` keeps its name and call shape (§2.3). |
| `vs.get_vectorstore` (store seam, fake INDEX) | test_documents_b4.py:150, test_ingest_ids.py:38, test_access_resolver.py:260, test_retrieval_acl.py:145/229/293 (4 files) | These pin Pinecone-impl behavior ($in grammar, metadata stamping, chunk ids) — they become the **contract tests** of §11, run against BOTH stores. |
| `ensure_index` internals | test_vectorstore_index.py (2 tests, `_FakePinecone`) | Pinecone-only concern (dimension guard, ready-wait) — dies with the Pinecone impl; the pgvector analog (dimension mismatch refusal) gets its own contract test. |

### 1.3 Verify scripts and mutation harness

All 8 live scripts touch Pinecone (per-run namespace discipline, A5.4):
b2.sh (9 refs), b3.sh (9), b4.sh (12 — the namespace harness: header :5-14,
prefix-guard :45-46, .env key check :110-111, `PINECONE_NAMESPACE=$NS`
env pin :125, by-id cleanup), b5.sh (7), b6.sh (3), g1.sh (6), g2a.sh (6),
purge-multi-user-test.sh (1). b2/b3 exercise the legacy routes that die at
B9 regardless. scripts/mutation/* patch app/ files (vectorstore.py is a
g2a mutation target) — targets updated per-phase.

### 1.4 Infra, env, docs

- `requirements.txt`: `pinecone==7.3.0` (:51), `langchain-pinecone==0.2.13`
  (:33) — removed at the Pinecone-removal phase. Kept: `langchain-openai`
  (:32), `openai==3.3.1` (:46), `langchain-community==0.4.2` (:30 — the
  parsers), `psycopg[binary]==3.3.6` (:54). The CI audit job
  (.github/workflows/ci.yml:37-47) scans this file — the removal changes
  the scanned surface; re-run and record.
- `.env.example` (:5 PINECONE_API_KEY, :11 dimension note), `README.md`
  (11 refs: env table :101-110, tree :162, dimension warning :174),
  `docs/dev-testing.md` (7), CLAUDE.md (1), design history docs — swept in
  the removal phase.
- `.github/workflows/ci.yml`: the `test` job's `postgres:18-alpine` service
  (:12-23) must become `pgvector/pgvector:pg18` for the contract/RLS tests
  to run in CI; `sast` (:49-70) unchanged.
- `docker-compose.yml`: `db` service image `postgres:18-alpine` must become
  `pgvector/pgvector:pg18` (same volume layout, same initdb mount, same
  healthcheck). `test` service unchanged.

### 1.5 Count of what changes

- **Production: 13 files.** Rewrite 1 (vectorstore.py → Protocol + two
  impls, §2); moderate 5 (retrieval.py, access.py, config.py, workflow.py +
  state.py, conversations.py); small 5 (routes.py, documents.py, admin.py,
  auth.py, cli.py — import paths and call sites only); docstring/sweep 2
  (documents.py service, templates/index.html).
- **Tests: ~20 files touch the three seams.** 12 route/CLI-level files
  unchanged (names preserved); 3 graph-seam files unchanged (same); 4
  store-seam files + test_vectorstore_index.py become contract tests (§11);
  new: contract suite, RLS suite, recall benchmark, golden-question gate.
- **Scripts: 8** verify scripts lose the Pinecone machinery and gain the
  scratch-DB pattern they already use (rag_verify_b5/b6/g1 —
  docs/PROGRESS.md 2026-11-12).
- **Infra: 6** (compose image, CI service image, requirements ×2 packages,
  .env.example, README).

---

## 2. Target architecture

### 2.1 The VectorStore Protocol

**The access seam is an AccessContext (Farhad's DECISION #3 — replaced the
draft's audience-token list and the Pinecone `$in` dict alike):**

```python
@dataclass(frozen=True)
class AccessContext:
    """Who is asking. Exactly two shapes:
    - AccessContext(user=<uuid>) — an authenticated user; retrieval and RLS
      run in THEIR identity (audiences derived in SQL, §3; RLS SET LOCAL,
      §5).
    - ORG_ONLY — a module constant; legacy /api/chat (D8) and any future
      context-free path see ONLY org documents (the corrected fail-closed
      semantics, §5.3).
    The Pinecone adapter renders its own prefixed tokens from the context
    via the resolver; nothing Pinecone-shaped crosses the seam."""
    user: UUID | None
    # ORG_ONLY = AccessContext(user=None)

class VectorStore(Protocol):
    def add_chunks(self, chunks: list[Document], *, doc_id: UUID,
                   scope: str, owner_id: UUID | None, source_name: str,
                   group_id: UUID | None) -> int: ...
    """Stamp ACL metadata (today vectorstore.py:151-199), embed, persist.
    Returns the chunk count. Replace semantics INCLUDED: whatever the
    backend, chunks of doc_id are fully replaced."""

    def delete_document(self, doc_id: UUID) -> None: ...
    """Idempotent delete of every chunk of doc_id (today :202-211)."""

    def search(self, query_text: str, *, context: AccessContext,
               k: int | None = None) -> list[Document]: ...
    """Embed query_text (§7 provider), k-NN restricted to the caller's
    audience (§3/§4 SQL, derived from the context) AND the RLS layer (§5).
    Returns LangChain Documents whose metadata carries at least:
    doc_id, scope, source (display name — citations, workflow.py:188-193),
    page (int | None), created_at. Refuses a missing context (the §A7
    fail-closed rule, today :131-135)."""

    def copy_chunks(self, source_doc_id: UUID, target_doc_id: UUID) -> int: ...
    """Chunk rows copied to a new document WITHOUT re-embedding (Part B
    share-copy, §10). Pinecone impl: re-embed (no copy primitive)."""

    def replace_document(self, doc_id: UUID, chunks: list[Document], *,
                         scope: str, owner_id: UUID | None,
                         source_name: str, group_id: UUID | None) -> int: ...
    """New chunks BEFORE old deleted (C6 order — provable). One
    transaction on pgvector; the Pinecone impl keeps delete-then-add."""

    def count_chunks(self, doc_id: UUID) -> int: ...
    """Ops/parity helper (per-document probes — the A5.4 lesson: never
    stats counters)."""
```

`add_chunks`/`delete_document`/`search` reproduce today's three public
functions; `copy_chunks`/`replace_document` are the Part B operations the
Protocol must own; `count_chunks` exists for evidence probes. **Access
inputs are the audience TOKEN LIST + user_id** (the RLS layer needs the
user, §5) — not the `$in` dict.

### 2.2 Two implementations, one protocol

- `PineconeVectorStoreAdapter` — wraps the existing functions verbatim
  (get_vectorstore/get_retriever internals stay until removal). Its
  `filter_for` dict shape (`{"read_access": {"$in": [...]}}`,
  access.py:68-69, retrieval.py:14) remains Pinecone-internal.
- `PgVectorStore` — SQLAlchemy + raw SQL (`text()`), sessions from
  `get_sessionmaker()`. **No new Python dependency**: the embedding vector
  binds as a `'[0.1,0.2,...]'` string cast `::vector` in SQL — floats
  only, no injection surface, so `pgvector-python` is NOT added
  (CLAUDE.md: no new dependency without justification).

A transitional settings field `vector_store: str = "pinecone"` selects the
adapter (factory in app/rag/vectorstore.py). This is a MIGRATION DEVICE,
not a permanent mode flag: the Pinecone implementation, the setting, and
the `$in` dict all die in the removal phase (P10, §12) once the
   golden-question gate has passed — the same end-state discipline as the
   B9 legacy-route removal.
Flagged for Farhad because it touches the no-flag rule (DECISION #4).

### 2.3 The seams the graph and callers see (compatibility)

The module `app/rag/vectorstore.py` keeps function-level entry points so
the 15 seam-mocking test files do not move:

- `get_retriever(context)` stays the graph's retrieval seam (workflow.py:82
  unchanged in shape; the argument becomes the AccessContext — legacy chat
  passes `ORG_ONLY` where it passed `ORG_ONLY_FILTER`, routes.py:58).
- `add_documents(...)`, `delete_document_vectors(doc_id)` keep their exact
  signatures and dispatch to the configured adapter.
- `AgentState.retrieval_filter` (state.py:27) becomes
  `access_context: AccessContext` (the field is renamed to say what it
  is); conversations.py:226 builds `AccessContext(user=user.id)` inside
  TX1 — the resolver's `audiences_for` remains the resolver-level source
  of truth for what that context resolves to.

**Contract change to declare, none to hide:** the `$in`-dict pins in
tests/security/test_access_resolver.py and test_retrieval_acl.py become
AccessContext pins (same assertions, same strength — the dict was
Pinecone's rendering of exactly the audiences the context resolves to).
Everything else in the A14.8 suite runs unchanged (A14.8's own header:
"backend-agnostic — run unchanged against pgvector",
docs/backend-design.md:1387).

Connection discipline (§A8): today the graph run holds ZERO DB resources
(conversations.py:230-233). Under pgvector the search is itself a DB call
inside `retrieve_kb` — the store opens its OWN short-lived session
(checkout → SET LOCAL app.user_id → one query → close), never the request
session (which committed at TX1, conversations.py:227) and never held
across LLM/Tavily calls. The pool-discipline tests extend to the search
path (§11).

---

## 3. Schema (Alembic 0006 `document_chunks`)

```sql
CREATE TABLE document_chunks (
    document_id       uuid        NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index       integer     NOT NULL,
    content           text        NOT NULL,
    page              integer,             -- PDF page at chunk time (B4 citations); NULL for txt/md/docx
    embedding         vector(1536) NOT NULL,   -- dimension = settings.embedding_dimensions AT MIGRATION TIME, written literally with a comment
    embedding_model   text        NOT NULL,    -- e.g. 'text-embedding-3-small'
    embedding_version integer     NOT NULL DEFAULT 1,   -- pipeline version, bumped on re-embed procedures
    generated         tsvector    GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, chunk_index)
);
CREATE INDEX ix_document_chunks_document_id ON document_chunks (document_id);  -- covered by the PK, kept explicit for CASCADE speed
```

- The deterministic sha256 `_chunk_id` grammar (vectorstore.py:140-148)
  DIES: its job (doc-scoped replace + delete) becomes the PK + CASCADE.
  Re-ingest upserts over `(document_id, chunk_index)` exactly as before.
  `test_ingest_ids.py`/`test_documents_b4.py` chunk-id pins move into the
  contract suite (§11).
- `vector(1536)` is dimension-LOCKED per column. A model change needs the
  §7 procedure, never an in-place rewrite. The migration writes the
  literal dimension from `settings.embedding_dimensions` (new setting,
  default 1536 — read by the store's validation and proven by a test per
  the settings-audit rule).
- `'simple'` text config (language-agnostic) in the generated column;
  'german' is the §6 experiment. Renaming/parametrizing that expression
  later is a table rewrite — decide by measurement, not by guess (§6).
- **Access control: DERIVED AT QUERY TIME (recommended).** Nothing is
  duplicated onto chunks:

```sql
-- THE one access predicate (single source of truth in SQL, §5 reuses it).
-- p_uid comes from the AccessContext (NULL for ORG_ONLY):
AND (
       d.scope = 'org'
    OR (d.scope = 'private' AND d.owner_id = :p_uid)
    OR (d.scope = 'group' AND :p_uid IS NOT NULL AND EXISTS (
          SELECT 1 FROM group_memberships m
          JOIN groups g ON g.id = m.group_id
          WHERE m.group_id = d.group_id
            AND m.user_id  = :p_uid
            AND g.is_active))              -- archived groups in NO audience (H13)
    )
AND d.status = 'indexed'                   -- pending/failed/deleting never retrieve
```

  Trade-offs, honestly: a denormalized `read_access` token column (the
  Pinecone scheme transplanted) would make queries join-free and grant
  latency zero, but every membership change/archive/role flip must rewrite
  chunk rows for every affected document — a background-rewrite subsystem
  with drift risk, and "revoke applies to the very next request"
  (access.py:11-14, G1.2's proven invariant) becomes EVENTUALLY consistent.
  Derived access costs one join on a small, PK-indexed membership table and
  is ALWAYS exactly consistent with `can_read` (access.py:71-91), the
  listings (documents.py:126-138) and the admin rules. Recommendation:
  **derived** (DECISION #1). The Python-vs-SQL drift risk is closed by a
  parity test (§11).

---

## 4. Search and indexes

**Exact search first (the default).** `ORDER BY embedding <=> :q LIMIT k`
with the §3 predicate — pgvector does exact (sequential-scan) KNN when no
ANN index exists; recall is perfect by construction. Metric: cosine
(`<=>`, `vector_cosine_ops`) — matches Pinecone `metric="cosine"`
(vectorstore.py:93). top_k stays `settings.top_k` = 4.

**When HNSW:** only after the measurement below shows exact p95 latency
above target (~150 ms — retrieval must stay invisible inside a
multi-second chat). Then:

```sql
SET maintenance_work_mem = '1GB';          -- session-level, index builds only
CREATE INDEX ix_chunks_hnsw ON document_chunks
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

**Dimension ceiling (PART 0 0.6):** the `vector` type indexes at most
**2000 dimensions** per vector (storage goes higher; indexes do not).
`text-embedding-3-small` (1536) indexes fine; `text-embedding-3-large`
(3072) would NOT be indexable as `vector` — a larger-dimension model
means `halfvec` (up to 4000 indexable, half precision) or a
dimension-reducing model choice. This is recorded in the §7 change
procedure so a future model switch discovers the constraint in the design,
not in a failed CREATE INDEX.

**Recall under restrictive filters (the known pgvector failure mode):**
HNSW + WHERE is post-filtering — a selective predicate can return < k rows
from the probe. pgvector ≥ 0.8 (the pg18 image ships it) adds iterative
scans: `hnsw.iterative_scan = strict_order` (exact-order, slower),
`hnsw.max_scan_tuples`, and per-query `hnsw.iterative_scan_offset`.
Design: strict_order (correctness first — access control must not trade
rank order), `max_scan_tuples` starting at 20k, tuned by the benchmark.

**Measurement plan (synthetic, local, zero external cost):**
- Dataset: 100k chunks, dim = `embedding_dimensions`, seeded PRNG; access
  split mirrors production intent: ~70% org, ~20% group (2 groups),
  ~10% private (100 users), `status='indexed'` except 5% poisoned rows.
- Filters measured: (a) REALISTIC audiences — a user with 1 group matches
  ~90% of rows (org is in EVERY audience — access.py:66 — so the access
  predicate's selectivity floor is the org share); (b) SYNTHETIC narrow
  filters matching 1% and 10% of rows — the worst case a future
  feature (per-document / group-only search) could produce, and the
  scenario the task names.
- For each: exact vs HNSW vs HNSW+iterative_scan; measure p50/p95 latency
  and recall@4 against exact ground truth (same seed).
- **Decision rule (recorded in docs/evidence/pgvector-benchmark.md):**
  1. exact p95 ≤ 150 ms at 100k → SHIP EXACT, no ANN index (current real
     corpus is 5 docs; the benchmark bounds the future).
  2. else HNSW + iterative_scan=strict_order IF recall@4 ≥ 0.99 at 10%
     AND ≥ 0.95 at 1% AND p95 ≤ 150 ms; tune max_scan_tuples/offset first.
  3. else exact-with-index-later, re-evaluate at the real corpus scale
     that makes latency matter (data-driven, not premature).

---

## 5. Access control in layers: resolver-as-SQL **plus** RLS

Two independent layers, both fail closed. Layer 1 is the §3 predicate in
every search query (and `can_read` for listings). Layer 2 is Postgres Row
Level Security — even a query with a forgotten/buggy WHERE cannot cross
audiences.

### 5.1 Roles

- `rag_owner` — migration role: owns tables/extensions, runs Alembic
  (migrations/env.py resolves `ALEMBIC_DATABASE_URL`), runs backfill/
  maintenance jobs. NOT the app's runtime credential.
- `rag_app` — runtime DML role: SELECT/INSERT/UPDATE/DELETE on the app
  tables, NO DDL, NO owner membership. `DATABASE_URL` switches to it
  (compose app + test services); `ALEMBIC_DATABASE_URL` keeps the owner.
  Created by an initdb script (docker/initdb/, the mount already exists —
  docker-compose.yml `db.volumes`) alongside `CREATE EXTENSION vector`.

### 5.2 Policies — document_chunks ONLY at P4 (PART 0 amendment 0.3)

**Scope:** RLS lands on `document_chunks` FIRST (the crown-jewel content
table). The `documents` table stays under application rules (can_read,
listings, admin surfaces) until the admin paths that touch it (metadata
listing of org/group documents, delete-any, quota counters, sweeps) are
re-designed for a policy-filtered world — a documents policy too early
would silently break the C4 aggregates and the §A9.1-legacy admin sweeps.
Extending RLS to `documents` (and later conversations/messages) is its own
reviewed phase.

**Read predicate — ONE function, distinct parameter names.** Parameters
are prefixed `p_` so they can NEVER shadow column names (the draft's
`m.group_id = group_id` compared the column to itself — under PL/pgSQL/SQL
name resolution the column wins, making EVERY group member able to read
EVERY group document; caught in review, PART 0 0.1). `STABLE` (not
IMMUTABLE — it reads `group_memberships`, which changes; an IMMUTABLE
marker would let the planner cache results across statements).
`SECURITY INVOKER` (the default, stated explicitly so no later
`SECURITY DEFINER` "convenience" sneaks in). The user id is unwrapped with
`NULLIF(current_setting('app.user_id', true), '')::uuid` — an EMPTY string
setting becomes NULL, not a cast error (a cast error would 500 queries;
NULL fail-closes them instead):

```sql
CREATE FUNCTION app_can_read_chunk(p_scope text, p_owner_id uuid,
                                   p_group_id uuid, p_uid uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER PARALLEL SAFE AS $$
  SELECT p_scope = 'org'
      OR (p_scope = 'private' AND p_owner_id = p_uid)
      OR (p_scope = 'group' AND p_uid IS NOT NULL AND EXISTS (
            SELECT 1 FROM group_memberships m
            JOIN groups g ON g.id = m.group_id
            WHERE m.group_id = p_group_id AND m.user_id = p_uid
              AND g.is_active));
$$;

ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_chunks_read ON document_chunks FOR SELECT TO rag_app
  USING (EXISTS (SELECT 1 FROM documents d
                 WHERE d.id = document_id
                   AND app_can_read_chunk(d.scope, d.owner_id, d.group_id,
                         NULLIF(current_setting('app.user_id', true), '')::uuid)));
```

**Write policies — explicit (PART 0 0.3).** Chunks follow their document's
manage rules (the `_can_manage` matrix, app/api/documents.py:91-98: private
→ owner, group → editor of THAT group, org → admin; C3 admin delete-any):

```sql
CREATE FUNCTION app_can_manage_chunk(p_document_id uuid, p_uid uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER PARALLEL SAFE AS $$
  SELECT EXISTS (
    SELECT 1 FROM documents d
    WHERE d.id = p_document_id
      AND ( (d.scope = 'private' AND d.owner_id = p_uid)
         OR (d.scope = 'group' AND p_uid IS NOT NULL AND EXISTS (
              SELECT 1 FROM group_memberships m
              WHERE m.group_id = d.group_id AND m.user_id = p_uid
                AND m.role = 'editor'))          -- C1: editor-ness per group
         OR (p_uid IS NOT NULL AND EXISTS (
              SELECT 1 FROM users u
              WHERE u.id = p_uid AND u.role = 'admin' AND u.is_active)) ));
$$;

CREATE POLICY p_chunks_insert ON document_chunks FOR INSERT TO rag_app
  WITH CHECK (app_can_manage_chunk(document_id,
                 NULLIF(current_setting('app.user_id', true), '')::uuid));
CREATE POLICY p_chunks_update ON document_chunks FOR UPDATE TO rag_app
  USING      (app_can_manage_chunk(document_id,
                 NULLIF(current_setting('app.user_id', true), '')::uuid))
  WITH CHECK (app_can_manage_chunk(document_id,
                 NULLIF(current_setting('app.user_id', true), '')::uuid));
CREATE POLICY p_chunks_delete ON document_chunks FOR DELETE TO rag_app
  USING      (app_can_manage_chunk(document_id,
                 NULLIF(current_setting('app.user_id', true), '')::uuid));
```

Tests (§11): ingest, replace and delete WORK as the app role with the right
context; cross-audience writes FAIL (user B cannot insert/delete user A's
private chunks; a plain member cannot write their group's chunks — editor
only; nobody writes org chunks without the admin context). RLS write
policies are DEFENSE IN DEPTH — the application never relies on them for
its normal authorization (can_read/is_editor stay the app layer); they
exist so a forgotten WHERE cannot corrupt another audience's chunks.

### 5.3 The user context, the pool, and fail-closed

- `SET LOCAL app.user_id = '<uid>'` as the FIRST statement of every
  transaction that touches document_chunks under a USER context (the
  search session of §2.3, and any other content-touching session). With
  the `ORG_ONLY` context the setting is simply NOT set — org rows are the
  org-only path, by policy. `SET LOCAL` is transaction-scoped: with the
  SHARED pool, a connection returning to the pool loses the setting at
  commit — **no per-connection `SET`** (that would leak identity across
  checkouts; the pool-discipline tests would catch it).
- **Fail closed when the setting is missing (CORRECTED — PART 0 0.2):**
  `current_setting('app.user_id', true)` returns NULL → `NULLIF(..., '')`
  → NULL uuid → every user-dependent branch of the policy evaluates NULL
  → the policy passes ONLY rows whose visibility does not depend on the
  user at all: **the app sees ONLY org rows.** It cannot silently see
  everything, and it cannot silently see another user's private or group
  chunks — but "org-only" IS the degraded result, which is exactly why
  ORG_ONLY is an honest, named context rather than an accident.
- **Migrations vs policies:** Alembic runs as `rag_owner`, which BYPASSES
  RLS on tables it owns unless `FORCE ROW LEVEL SECURITY` is set. Design
  (Farhad's DECISION #2): **ENABLE without FORCE** — the app role is
  non-owner and fully filtered; the owner bypass is exactly what backfills/
  re-embed jobs and the §A9.1 sweeps need (they touch private chunks by
  design, operator-initiated). FORCE would break those jobs or force a
  policy bypass-flag, i.e. theater with extra steps. The bypass surface is
  the owner credential, which the app never holds.

### 5.4 Admin aggregates (deferred with the documents policy)

C4/D3 aggregates (private count + bytes, k≥5 suppression) run today against
the `documents` table WITHOUT RLS (PART 0 0.3) — unchanged behavior. When
RLS extends to `documents`, the aggregate queries must move to SECURITY
DEFINER functions owned by `rag_owner` that return ONLY numbers — no
names, no content:

```sql
CREATE FUNCTION fn_admin_private_stats(p_uid uuid)
RETURNS table(docs int, bytes bigint)
  LANGUAGE sql SECURITY DEFINER STABLE SET search_path = public AS $$ ... $$;
```

The API layer keeps the k≥5 suppression (unchanged admin listing code).
This keeps "admins never see content or filenames of private documents"
true at EVERY layer — that guarantee is part of the documents-RLS phase's
review, not P4.

### 5.5 Bypass-attempt tests (mandated)

1. rag_app, no SET LOCAL → sees ONLY org chunks; private/group chunks
   invisible (fail-closed, corrected semantics — PART 0 0.2).
2. rag_app, `RESET app.user_id` mid-transaction → user context gone →
   back to org-only; their private/group chunks vanish.
3. **A member of group A CANNOT read group B's chunks** (the exact mandated
   test — the 0.1 shadowing bug would have let every group member read
   every group document; this test is what kills that class for good).
4. rag_app, `SET ROLE rag_owner` → permission denied.
5. rag_app, any DDL → denied.
6. Write-policy matrix: ingest/replace/delete WORK as the app role with
   the owner/editor/admin context; cross-audience writes FAIL (user B →
   user A's private chunks; plain member → their group's chunks; anyone →
   org chunks without the admin context).
7. A user's chunk rows are invisible to a second user's SET LOCAL context.

---

## 6. Hybrid search (default OFF until proven)

- Postgres FTS rides the `generated` tsvector column (§3). 'simple' today
  (language-agnostic, zero stemming surprises); 'german' is the experiment
  arm — swapping the generated expression is a rewrite, so the German
  config is evaluated on the §11 question set BEFORE any schema change.
- Fusion: reciprocal rank fusion — vector top-50 ∪ FTS top-50 (`websearch`
  or 'simple' plainto query), `score = Σ 1/(60 + rank)`, access predicate
  inside BOTH subqueries, final ORDER BY fused score LIMIT k.
- Gated OFF by a `enable_hybrid_search: bool = False` setting (settings-
  audit rule: the flag must gate real code — the fused query path — and be
  proven by tests). An evaluation on a graded question set (nDCG@4 vs pure
  vector) decides ON or deletion. No ON by default in any environment
  until that evaluation exists in docs/evidence/.

---

## 7. Embedding/provider abstraction

- Today: `OpenAIEmbeddings` hardcoded to `settings.openai_api_key` +
  `embedding_model` (vectorstore.py:51-60); `ChatOpenAI` similarly
  (workflow.py:20-31); dimensions via the hardcoded `EMBEDDING_DIMENSIONS`
  map (:18-23).
- Design: both behind the OpenAI-compatible client shape they ALREADY use
  — new settings `embedding_base_url` (default empty = OpenAI), `embedding_model`,
  `embedding_dimensions: int = 1536` (replaces the map; unknown-model
  startup validation instead of the substring-guessing at :35-42).
  langchain-openai accepts `base_url` on both classes — **no new
  dependency**. Self-hosted candidates (license checks TODO before any
  switch — DECISION #7): BGE-M3 (MIT), Qwen3-Embedding (Apache-2.0),
  served via vLLM/Ollama/TEI OpenAI-compatible endpoints.
- Per-chunk provenance: `embedding_model` + `embedding_version` stored on
  every chunk row (§3) — the store refuses to mix models in one ranking
  (search restricts to ONE model+version; §8 dual-read).
- **Model/dimension change procedure (no switch in this task):**
  1. New column (`embedding_v2 vector(M)`) or twin table, per decision;
     **if M > 2000 the column must be `halfvec(M)`** — the `vector` type
     indexes only to 2000 dims (§4);
  2. Re-embed job reads ORIGINAL TEXT from `document_chunks.content`
     (chunks store their own text — no dependence on any external store),
     batches, writes the new column, bumps `embedding_version`;
  3. Dual-read period: search targets the new column once 100% of
     indexed chunks are migrated (per-document all-or-nothing — a document
     mixes versions only during its own re-embed, which is atomic);
  4. Old column dropped after the golden-question gate (§8.2) passes on
     the new column.
  The vector column is dimension-locked; nothing ever rewrites embeddings
  in place.

---

## 8. Migration plan

### 8.1 What is migrated, from what source

- **Source of truth: the stored originals `uploads/<doc_id>/` (+ the
  `documents` rows), NEVER Pinecone** — re-parse and re-chunk with the
  SAME pipeline (ingestion.py `load_file`/`chunk_documents`), re-embed,
  insert. This is also why the egress cap cannot touch the migration.
- `rag-v2` (5 vectors = 2 org sample-KB docs, 3+2 chunks — PROGRESS.md H6):
  both docs have documents rows + stored files → re-embedded by the job.
- The **27 legacy vectors** (namespace `enterprise-agentic-rag`): NO
  documents rows, pre-B4 metadata, read-only since B4 (no code path reads
  that namespace — config.py:24 default is rag-v2). They migrate to
  NOTHING. Disposition is the operator's (DECISION #5): archive-and-freeze,
  or namespace-scoped delete (`delete(delete_all=True, namespace=...)` —
  never `delete_index`) after the §8.2 cutover passes the golden gate, or
  leave
  frozen indefinitely. Listed, not decided here.
- `multi-user-test` family: already 0 (A5.4 purge); per-run namespaces end
  with the Pinecone impl.

### 8.2 Cutover steps (one per phase, §12)

1. P1: compose + CI images swap (pgvector/pgvector:pg18), extension via
   initdb, roles per §5.1 — no app change, suite green.
2. P2: Alembic 0006 `document_chunks` (§3) + `embedding_dimensions`
   setting.
3. P3: Protocol + `PgVectorStore` (add/delete/search exact) + contract
   tests green against BOTH impls (§11) — Pinecone still the default.
4. P4: RLS layer (document_chunks) + bypass/write-policy tests (§5).
5. P5: seam switch (§2.3): state carries the AccessContext; A14.8 suite
   green.
6. P6: backfill job (re-embed from uploads/; ~7 embeddings for the 2 real
   docs; the SYNTHETIC benchmark corpus needs none) + **the golden-question
   gate (Farhad's DECISION #10 — CHANGED from the draft's Pinecone-parity
   gate)**: a fixed question set of AT LEAST 20 questions, each with its
   EXPECTED SOURCE DOCUMENT known in advance, run against pgvector ALONE;
   every question must retrieve its expected document in the top-k (top_k
   = 4); results recorded in docs/evidence/pgvector-golden.md. **Parity
   against Pinecone is optional and NOT a gate** — fully local, so P6 is
   NOT blocked by the egress cap.
7. P7: cutover — `vector_store: "pgvector"` default flips AFTER the
   golden-question gate passes; Pinecone code stays (rollback §8.3); A14.8
   + contract suites green both modes.
8. P8: `copy_chunks`/`replace_document` land (Part B unblocked, §10) + the
   orphan-files report-only CLI (§9, PART 0 0.5).
9. P9: hybrid search arm (§6) behind OFF; evaluation optional/deferred.
10. P10 (immediately after P7 per DECISION #4): **removal** — Pinecone
    adapter, `pinecone_*` settings, requirements pins (pinecone,
    langchain-pinecone), .env.example/README sweep, the transitional
    setting deleted, audit job re-run (the scanned surface changes),
    namespace disposition executed (DECISION #5).

### 8.3 Rollback

- Before P7: nothing to roll back (no behavior change; migration is
  additive).
- At P7: flip the setting back; Pinecone data + code intact until P10.
  The rollback window CLOSES at P10 by design (the golden gate passed
  first).
- After P10: rollback = git revert + re-embed from uploads/ into a fresh
  Pinecone namespace — possible as long as stored files exist (document
  deletes remove files; the corpus is re-derivable from git-tracked
  sample_kb plus re-uploads). Honest cost: re-embedding spend.

### 8.4 What each legacy surface becomes

- §A9.1 two-system consistency (backend-design.md:741-786): the ordering
  dance (begin_delete → external delete → complete_delete, the
  `deleting` status, `retry_stuck_deletes`, `documents-retry-deleting`
  CLI) collapses to ONE transaction: `DELETE FROM documents WHERE id = :id`
  and CASCADE removes the chunks atomically. The `deleting` status, the
  sweeps and the CLI are REMOVED at P7 (their failure mode ceases to
  exist). DECISION #9.
- The A9.1 documentation section is rewritten (delete = one transaction;
  the "vectors first, rows last" principle is recorded as history).

---

## 9. Operations

- **Image:** `pgvector/pgvector:pg18` in compose `db` (docker-compose.yml)
  and the CI `test` service (ci.yml:12-23). Same volume layout
  (`pgdata:/var/lib/postgresql`), same healthcheck, same initdb mount.
  Pin the concrete tag (e.g. `pgvector/pgvector:pg18` → record the digest
  in the P1 commit) — extension and PG minor versions move together.
- **Extension privileges:** `vector` is not a trusted extension — CREATE
  needs superuser. The compose bootstrap user (`POSTGRES_USER`, the
  container's bootstrap superuser) creates it via
  `docker/initdb/01-extensions.sql`; ownership of the app tables stays
  with `rag_owner`; `rag_app` gets USAGE on the extension's types via the
  public schema grants. Self-hosted prod = we own the superuser; a managed
  PG would need a provider that allows the extension (noted for the
  future, not a constraint today).
- **Index builds:** `SET maintenance_work_mem = '1GB'` (session-level);
  compose `shm_size: '1gb'` on `db` for parallel builds;
  `max_parallel_maintenance_workers` default is fine at our scale.
- **Backups:** `pg_dump` as `rag_owner`. Chunk content is document content
  — backups inherit the same access discipline as `uploads/` (operator
  storage, not repo, not admin-readable through the app). The chunks are
  re-derivable (re-embed from uploads/) so the CRITICAL set is
  documents rows + stored files; chunks included anyway (cheap).
- **Sizing:** 1536-dim float4 ≈ 6.3 KB/vector. 100k chunks ≈ 0.6 GB
  embedding data + HNSW ≈ 0.8-1 GB. 1M chunks ≈ 6.3 GB + ≈ 8-9 GB HNSW.
  Exact search at 1M is not viable (§4 rule forces the index). Text
  content dominates tables at real corpus sizes for years to come.
- **Connection pool:** the search session checks out ONE connection for
  the duration of one query (§2.3); chat holds nothing across LLM calls
  (§A8 unchanged); ingest gate caps concurrent ingest (ingestion.py:228-247).
  SQLAlchemy defaults (pool 5 + overflow 10) cover 20/min/user chat +
  bounded ingest; the pool-discipline tests (§11) prove it.
- **Vacuum:** `document_chunks` gets dead tuples from replaces (P8) and
  Part B swaps — autovacuum defaults suffice at our scale; monitor
  `pg_stat_user_tables.n_dead_tup` and bloat after the first version-swap
  exercise. No full-vacuum automation in this design.
- **Orphan files (PART 0 0.5):** uploads whose process died between TX1
  and TX2 (or any historical artifact) can leave `uploads/<doc_id>/`
  directories with NO documents row. A REPORT-ONLY operator CLI
  (`orphans-list`) lists them (directory name, file count, bytes) — it
  NEVER deletes; disposition stays with the operator, exactly like the
  800 root-owned `uploads/` entries reported in A5.5. Lands with P8.
- **Monitoring:** `pg_stat_statements` (latency of the search query),
  `log_min_duration_statement` above search budget (150 ms), pool
  `checkedout()` already exposed in tests — a /api/health-adjacent metric
  is NOT added (keeps the endpoint anonymous per §A8). Operator queries
  via psql, documented in dev-testing.md at P1.

---

## 10. Effect on G2 Part B (and A9.1 / quotas / listings)

| Part B item | Today (Pinecone) | Under pgvector | g2.md edit needed |
|---|---|---|---|
| B1 share = COPY (§A14.5) | New group doc created by re-parsing the stored bytes + RE-EMBEDDING through the ingest path | `copy_chunks(source, new_doc)`: one SQL INSERT…SELECT copying chunk rows (content, page, embedding) — zero embeddings spend, one transaction with the documents row | B1: "from the same stored bytes through the normal ingest path" → "chunk rows copied in SQL (`copy_chunks`), no re-embedding"; migration number 0006 → **0007** (pgvector takes 0006, §8.2 P2) |
| B2 versions (C6) | Add new chunks, THEN delete old (two external calls, failure window, sweep-resumable) | One transaction: INSERT new chunks + DELETE old — the C6 add-first order is provable INSIDE the transaction; a crash leaves old or new, never neither | B2: "add the new chunks first, then delete the old ones" → "single-transaction swap (`replace_document`)"; A14.8.6 call-order test becomes a transaction-boundary test (same strength) |
| B3 governance dates | `last_reviewed_at`/`review_due_at` on documents (org+group) — unchanged by this design | unchanged (they are already Postgres rows) | none |
| B4 citations | Page stored in vector metadata at chunk time; dates read from Postgres by doc_id in TX2 (never frozen — A14.8.9) | `page` is a REAL column from 0006 (§3); dates still read from documents in TX2; the citation join reads chunks+documents in one query | B4: "PDF page (stored at chunk time)" → "page column (0006)"; the "never frozen into vector metadata" pin becomes "page/dates live in relational columns, never duplicated into embeddings" (same guarantee, different mechanism) |
| A9.1 delete consistency | Two-system dance + sweeps + CLI (§8.4) | One transaction; sweeps/CLI/`deleting` status REMOVED at P7 | §A9.1 section rewrite at P7; g2a/g1 scripts' vectors-first CHECKs become CASCADE proofs |
| Quotas (A2/A5.2) | documents-table accounting, vectors untouched | unchanged (no vector interaction) | none |
| Admin listings (A5) | org+group rows, private aggregates, near-quota | unchanged + §5.4 SD counters for aggregates under RLS | A14.8.11 unchanged; RLS parity tests added (§11) |
| g2b.sh (live) | Pinecone per-run namespace + key checks | scratch DATABASE (rag_verify_g2b pattern), zero external cost | Part B test-mandate line: live checks use local Postgres only |

Part B becomes strictly SIMPLER: no external calls in the share/version
paths, transactional guarantees instead of ordering protocols, citations
from columns instead of metadata hygiene.

---

## 11. Test plan (all local — zero external cost)

1. **Contract tests, both backends** (the §2 Protocol behaviors): add →
   metadata/ACL stamping; replace semantics; delete idempotent; search
   context matrix (ORG_ONLY / user in 0/1/2 groups / archived group /
   unknown scope — mirrors access.py:71-91 exactly); count_chunks;
   copy_chunks; missing-context refusal (§A7 fail-closed,
   vectorstore.py:131-135). Run the SAME suite against (a) the Pinecone
   adapter via the existing fakes (offline, keyless) and (b)
   `PgVectorStore` against REAL Postgres (`rag_test`, the pgvector image —
   offline in the "no external service" sense). Inherits:
   test_documents_b4.py, test_ingest_ids.py, test_access_resolver.py,
   test_retrieval_acl.py store-seam pins (§1.2 table).
2. **A14.8 behavior suite** — runs unchanged (backend-agnostic by design,
   docs/backend-design.md:1387); the two declared $in→AccessContext pin
   updates (§2.3) are strength-preserving and stated in the commit.
3. **Recall + latency benchmark** — the §4 measurement plan on 100k
   synthetic vectors (1% / 10% / realistic filters), decision rule
   applied, recorded in docs/evidence/pgvector-benchmark.md. Generator is
   seeded/deterministic; runs in the test profile only (never CI-per-push
   — a manual evidence step).
4. **RLS suite** — the §5.5 bypass attempts (incl. the MANDATED
   group-A-member-cannot-read-group-B test and the write-policy matrix) +
   the Python-vs-SQL parity matrix (same doc/user fixtures through
   `can_read` AND the policy).
5. **Golden-question suite** (DECISION #10) — ≥20 fixed questions, each
   with a known expected source document, run against pgvector; every
   question must retrieve its expected document in the top-k; recorded in
   docs/evidence/pgvector-golden.md. This is the P7 CUTOVER GATE.
   Pinecone parity is optional and not a gate.
6. **Pool discipline extended** — `checkedout()==0` after the graph run
   INCLUDING a slow fake search (the §2.3 session-per-search discipline);
   the A5.1 upload probes unchanged.
7. **Mutation targets** (after Part-A practice, scripts/mutation/):
   M-V1 org branch dropped from the read predicate; M-V2 owner compare
   inverted; M-V3 `g.is_active` dropped (archived group leaks — H13);
   M-V4 `status='indexed'` dropped (failed/deleting docs retrieve);
   M-V5 SET LOCAL skipped (killed by bypass test 1); M-V6 RLS parameter
   shadowing reintroduced (`p_group_id` → `group_id` — killed by the
   group-A/B test, the exact regression that motivated PART 0 0.1);
   M-V7 write policy WITH CHECK dropped (killed by the cross-audience
   write tests); M-V8 copy_chunks loses page/embedding columns (killed by
   contract test); M-V9 replace_document delete-before-insert (killed by
   the transaction-boundary test); M-V10 golden-gate threshold weakened
   (killed by the gate's own red-run). All live checks run against LOCAL
   Postgres only.

---

## 12. Risks, alternatives, phased plan, DECISIONS FOR FARHAD

### 12.1 Risks (and the counter in this design)

- **Recall collapse under restrictive filters** — the classic ANN+filter
  trap. Counter: exact-first (§4), iterative-scan plan, measured decision
  rule; access predicates are realistically ~70-90% selective anyway.
- **RLS misconfiguration silently hiding data** — a bad policy reads as
  "no results" for everyone. Counter: fail-closed is the DESIGN (§5.3),
  the bypass suite proves both directions (no context → nothing; user
  context → exactly their audience), and the golden-question gate (§8.2 P6)
  catches pre-cutover.
- **Python vs SQL access drift** — `can_read` (listings) vs the policy.
  Counter: one predicate function (§5.2) + the parity matrix test (§11.4).
- **Search-in-graph changes pool behavior** — counter: session-per-search
  + extended pool tests (§2.3, §11.5).
- **Migration correctness** — counter: additive migration, the
  golden-question GATE before cutover, rollback window until P10 (§8.3).
- **1M-chunk future** — sizing §9; the §4 rule triggers the index when
  real data makes it matter.
- **Backups grow** — chunk content doubles stored bytes vs documents-only;
  §9 notes the re-derivability escape hatch.

### 12.2 Alternative: Qdrant — and why not

Qdrant is the strongest dedicated alternative (real filtering inside the
engine, self-hostable). Rejected: it is a SECOND stateful system to run,
backup, secure and monitor — the exact cost pgvector removes; access
filters would live in Qdrant's filter DSL instead of SQL, so the §3/§5
single-source-of-truth (can_read ≡ SQL predicate ≡ RLS) would not exist;
RLS synergy is unique to Postgres; our scale (real corpus today: 5
vectors; benchmarked target: 100k) is far below Qdrant's justification
threshold. Keeping Pinecone is not an alternative — the task's premise
(vendor cost/cap) plus D12's credential-leak note.

### 12.3 Phased implementation plan (commits + review checkpoints)

| Phase | Commit subject (planned) | Contents | Checkpoint |
|---|---|---|---|
| P1 | feat(pgvector/p1): pgvector image + roles in compose and CI | §8.2 step 1, zero app change; task file docs/tasks/pgvector-p1.md PART 1 | suite green both modes |
| P2 | feat(pgvector/p2): migration 0006 document_chunks | §3, settings, schema tests first | **Farhad reviews schema** |
| P3 | feat(pgvector/p3): VectorStore Protocol + PgVectorStore (exact) | §2, contract tests both backends, Pinecone still default | suite green |
| P4 | feat(pgvector/p4): RLS layer (document_chunks) | §5, bypass + write-policy suites | **Farhad reviews RLS** |
| P5 | feat(pgvector/p5): seam switch (AccessContext in state) | §2.3, A14.8 green, declared pin changes | suite green both modes |
| P6 | feat(pgvector/p6): backfill + golden-question gate | §8.2 step 6 — fully LOCAL (Pinecone parity optional, not a gate — DECISION #10): NOT blocked by the egress cap | **Farhad reviews the golden report** |
| P7 | feat(pgvector/p7): cutover default → pgvector | flip setting; A9.1 machinery removal (§8.4); both modes green | **Farhad approves cutover** |
| P8 | feat(pgvector/p8): copy/replace ops + orphan-list CLI | §10 Part B unblock; §9 orphan CLI (report-only) | suite green |
| P9 | feat(pgvector/p9): hybrid search OFF + evaluation harness | §6 | optional |
| P10 | feat(pgvector/p10): Pinecone removal (immediately after P7 — DECISION #4) | adapter, settings, pins, docs sweep, audit re-run, namespace disposition (DECISION #5) | audit job green |

### 12.4 DECISIONS — CLOSED by Farhad 2026-11-23 (docs/tasks/pgvector-p1.md)

1. **Access derivation** — **DECIDED: derived access** (§3).
2. **RLS strength** — **DECIDED: ENABLE RLS without FORCE** (§5.3).
3. **Seam shape** — **DECIDED: AccessContext** (user_id or ORG_ONLY) —
   NOT a token list, NOT the Pinecone `$in` dict; the Pinecone adapter
   builds its own tokens via the resolver; LangGraph state carries the
   AccessContext (§2.1/§2.3). PART 0 0.4 applied throughout.
4. **Transitional `vector_store` setting** — **DECIDED: accepted**, with
   P10 immediately after P7 (no lingering dual-impl period).
5. **27 legacy vectors + rag-v2 namespace** — **DECIDED: migrate nothing**
   from legacy namespaces (§8.1); disposition stays operator-owned.
6. **Exact-first** — **DECIDED** (§4).
7. **Embeddings** — **DECIDED: keep text-embedding-3-small** (§7); no
   model switch in this task.
8. **Hybrid search** — **DECIDED: OFF** until an evaluation wins (§6).
9. **A9.1 machinery removal timing** — **DECIDED: remove at P7** (§8.4).
10. **Cutover gate** — **CHANGED by Farhad**: the gate is a
    **golden-question check against pgvector ALONE** — expected source
    document known for each question, ≥20 questions, recorded in
    docs/evidence/pgvector-golden.md; parity against Pinecone is optional
    and NOT a gate. (The draft's Pinecone-parity gate and its 2026-12-01
    read-cap dependency are GONE — §8.2 step 6 is fully local.)

Design-quality amendments from Farhad's PART 0 review (applied): 0.1 RLS
function parameter shadowing (distinct `p_` names; STABLE; SECURITY
INVOKER; NULLIF unwrap), 0.2 fail-closed corrected to ORG-ONLY + the
mandated group-A/B bypass test, 0.3 RLS scoped to document_chunks with
explicit write policies, 0.5 the orphan-files report-only CLI, 0.6 the
2000-dimension HNSW ceiling of the `vector` type.

---

## Status

DESIGN ONLY — nothing implemented; no Pinecone was contacted (code
reading only, per the cap constraint). Next step per the task: STOP and
report to Farhad; implementation starts at P1 only on his go, phase by
phase with the §12.3 checkpoints.
