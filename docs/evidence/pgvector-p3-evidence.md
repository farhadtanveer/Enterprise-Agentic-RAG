# pgvector P3 evidence — VectorStore Protocol + PgVectorStore (docs/pgvector-design.md §2)

Executed evidence per CLAUDE.md. Pinecone remains the shipped DEFAULT; no
caller is rewired (that is P5's declared seam switch); zero Pinecone calls;
zero real embeddings (deterministic hash embedder on the pgvector leg).

## RED first

```
command:  docker compose --profile test run --rm test \
            python -m pytest tests/test_vectorstore_contract.py -q
expected: collection errors — app.rag.vector_backend absent (the Protocol,
          adapters, factory); AccessContext absent from access.py
actual:   26 errors (ImportError: cannot import name 'PineconeVectorStoreAdapter'
          / module does not exist), both parametrizations
verdict:  RED as required
```

## What failed first in GREEN (all test-side, no product change)

1. The copy-chunks test created the target documents row on ONE session and
   committed on ANOTHER — the row never committed, the FK refused the copy
   (`IntegrityError`). Fixed: the test's own `db_session` (the caller)
   creates + commits the row, exactly the Part-B share-flow shape.
2. Test fixtures initially counted chunks via `count_chunks` on BOTH legs —
   but the Pinecone adapter has no count primitive (ids do not encode
   doc_id; reads are cap-blocked). Both-leg assertions became search-based;
   `count_chunks` got its own contract: EXACT on pgvector, DECLARED
   NotImplementedError on the Pinecone adapter.

## The shipped pieces

```
added:    app/rag/vector_backend.py —
          @runtime_checkable VectorStore Protocol (add_chunks with replace
          semantics, delete_document idempotent, search by AccessContext
          with §A7 missing-context refusal, copy_chunks, replace_document,
          count_chunks);
          PineconeVectorStoreAdapter — wraps the EXISTING vectorstore
          functions verbatim, renders tokens via DbAccessResolver
          (ORG_ONLY needs no DB); copy_chunks/count_chunks raise
          NotImplementedError with the declared reason (no Pinecone
          primitive; reads cap-blocked) — P8/pg-era capabilities;
          PgVectorStore — exact KNN (`ORDER BY embedding <=> CAST(:qvec AS
          vector)`), the derived access predicate (§3: org OR private-owner
          OR active-group-member; d.status='indexed'), own short session
          per operation (§A8), injectable embedder (lazy OpenAI default —
          import never needs a key), replace = delete+insert in ONE
          transaction (C6's guarantee, §10), copy = INSERT…SELECT with
          embeddings carried over (no re-embed);
          get_vector_store_backend — the DECISION #4 factory ('pinecone'
          default, fail-fast on unknown).
added:    app/services/access.py — AccessContext (user | None) + ORG_ONLY
          (DECISION #3; PART 0 0.2 semantics documented).
added:    app/core/config.py — vector_store='pinecone' (transitional,
          removed at P10) + embedding_dimensions=1536 (migration 0006's
          literal), BOTH with production readers (factory / dimension
          gate) and BOTH with effect tests.
pinned:   tests/conftest.py — VECTOR_STORE=pinecone, EMBEDDING_DIMENSIONS=
          1536 (H8: a developer .env can never move what the contract
          suite asserts).
```

Declared design deviations (both minor, stated in the commit):
- the factory lives in app/rag/vector_backend.py, not vectorstore.py —
  vectorstore.py stays UNTOUCHED in P3 so the four store-seam test files
  and every route-level fake keep passing; P5 wires the entry points.
- the Pinecone adapter cannot honor copy_chunks/count_chunks (no chunk
  enumeration primitive; ids are sha256 hashes, not doc-prefixed) —
  NotImplementedError with the reason INSTEAD of a fake-faithful half
  implementation; the contract suite skips those legs with the same
  declared reason.

## The contract (27 passed / 3 declared skips, per backend parametrization)

Both legs share the SAME fixtures (users/groups/memberships/documents via
the ORM; chunks via the store under test) and the SAME AccessContext →
resolver resolution, so the suite IS the first Python-vs-SQL parity matrix
(§11.4):

```
command:  docker compose --profile test run --rm test \
            python -m pytest tests/test_vectorstore_contract.py -q
expected: both backends agree on every audience scenario
actual:   27 passed, 3 skipped in 6.45s
verdict:  PASS — proven on BOTH legs:
- ORG_ONLY ⇒ ONLY the org chunk (the corrected PART 0 0.2 semantics)
- alice (active g1 + ARCHIVED g2) ⇒ org + own private + g1; NEVER g2
- bob (no groups) ⇒ org + own private
- archived-group chunk retrievable by NOBODY (H13 through both engines)
- membership REVOKED between searches ⇒ the next search excludes it
  (the G1.2 recompute invariant, now in SQL too)
- replace semantics: old chunks gone, EXACTLY the new chunks present
- delete idempotent (second call never raises)
- missing context ⇒ ValueError (§A7)
- k truncation honored
- copy (pg only, declared): chunks copied, embeddings carried over,
  retrievable under the target document, source untouched
- count_chunks (pg exact; pinecone declared NotImplementedError)
- pg-only metadata contract: doc_id/scope/source/page/created_at
- pg-only FK: chunks for unknown documents refused (IntegrityError)
- both adapters satisfy the runtime_checkable Protocol
```

## Settings effects (H7 rule)

```
test_vector_store_setting_selects_the_adapter: default pinecone ✓,
  pgvector honored ✓, unknown value raises ValueError mentioning
  vector_store ✓
test_embedding_dimensions_setting_gates_every_vector: 1536-dim literal
  accepted ✓; with EMBEDDING_DIMENSIONS=4 the 1536-dim vector is refused
  with the §7 procedure named ✓; a 4-dim vector passes the gate ✓
settings-audit reference test (tests/test_settings_audit.py): both fields
  read under app/ — suite green (below)
```

## Full suite, both modes

```
command:  docker compose --profile test run --rm test
expected: 423 baseline + 27 = 450 passed / 0 failed (3 declared skips)
actual:   450 passed, 3 skipped, 3 warnings in 218.52s      (compose mode)
command:  … -e ENABLE_WEB_FALLBACK=false test               (CI mode)
actual:   450 passed, 3 skipped, 3 warnings in 249.84s      (CI mode)
verdict:  PASS both modes
command:  bash scripts/verify/no-root-files.sh
actual:   PASS (800 historical uploads/ entries — reported only)
verdict:  PASS
```

## CI

- run on this commit: (polling — recorded in PROGRESS.md on confirmation)
