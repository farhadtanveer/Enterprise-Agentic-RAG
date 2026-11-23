# pgvector P2 evidence — migration 0006 `document_chunks` (docs/pgvector-design.md §3)

Executed evidence per CLAUDE.md: command, expected, actual, PASS/FAIL.
Tests FIRST (RED before the migration existed). ADDITIVE schema only —
no production code reads the table until P3; the Pinecone backend is
untouched; zero Pinecone calls.

## RED first (the mandate: report what failed first)

```
command:  docker compose --profile test run --rm test \
            python -m pytest tests/test_pgvector_schema.py -q
expected: every test fails (no migration 0006 → table absent; model
          module absent)
actual:   11 failed in 2.42s — programming errors on the absent
          app.models.document_chunk and relation "document_chunks"
          does not exist
verdict:  RED as required
```

## The migration + model, then GREEN

```
added:    migrations/versions/20261123_0006_document_chunks.py
          (revision 0006, down_revision 0005; create_table document_chunks
          with PK (document_id, chunk_index), FK documents ON DELETE
          CASCADE, embedding VECTOR(1536) via a literal-col_spec
          UserDefinedType (no pgvector-python), generated tsvector
          GENERATED ALWAYS AS to_tsvector('simple', content) STORED,
          page nullable, defaults 1/now(); downgrade = drop_table)
added:    app/models/document_chunk.py (Vector UserDefinedType +
          DocumentChunk; Computed column excluded from INSERTs)
          + app/models/__init__.py registration
```

```
command:  docker compose --profile test run --rm test \
            python -m pytest tests/test_pgvector_schema.py tests/test_migrations.py -q
expected: all green — schema shape + the FULL alembic chain incl.
          downgrade-to-base (which exercises 0006's downgrade)
actual:   22 passed in 3.18s (12 schema + 10 migration-chain tests)
verdict:  PASS
```

What failed first in GREEN (test-side, no product change): my fixtures
inserted 3-dimension embeddings into the dimension-LOCKED vector(1536)
column → `DataError: expected 1536 dimensions, not 3`. That IS the design
behavior (§3: a different-dimension insert fails loudly, never silently
truncates) — the fixtures were fixed to 1536-dim literals AND the behavior
became an explicit permanent pin:
`test_embedding_dimension_is_locked_at_1536`.

## Schema facts pinned by the tests (catalog queries, rolled back)

- exact 9-column set (document_id, chunk_index, content, page, embedding,
  embedding_model, embedding_version, generated, created_at);
  nullability per the task (content/embedding/embedding_model/
  embedding_version/created_at NOT NULL; page nullable).
- PK = (document_id, chunk_index) — the doc-scoped replace+delete the
  sha256 chunk-id grammar provided, now enforced by the key.
- embedding: type `vector`, **atttypmod = 1536** (the dimension is locked
  IN the type, per design §3; DECISION #7 keeps text-embedding-3-small).
- generated: attgenerated = 's' (STORED), expression
  `to_tsvector('simple', content)` — 'simple' config pinned; the 'german'
  alternative stays the §6 evaluation.
- FK to documents with confdeltype = 'c' (CASCADE) — §A9.1's
  one-transaction delete at P7 rests on this.
- behavior: embedding_version defaults 1, created_at defaults now(),
  generated equals `to_tsvector('simple', content)` on real inserts,
  unknown-document insert raises IntegrityError, NULL content raises
  IntegrityError, page nullable (7 and NULL both proven), document delete
  cascades both chunks away.

## Settings note

NO new Settings field in P2. `embedding_dimensions` (design §3) lands with
P3's PgVectorStore — its first production READER. A setting introduced now
would violate the no-decorative-settings rule (tests/test_settings_audit.py).
The 1536 in migration 0006 and the model is a commented literal pointing at
the design section and the future setting.

## Full suite, both modes

```
command:  docker compose --profile test run --rm test
expected: 411 baseline + 12 new = 423 passed / 0 failed
actual:   423 passed, 3 warnings in 188.90s                 (compose mode)
command:  docker compose --profile test run --rm -e ENABLE_WEB_FALLBACK=false test
actual:   423 passed, 3 warnings in 222.81s                 (CI mode)
verdict:  PASS both modes
command:  bash scripts/verify/no-root-files.sh
actual:   PASS (800 historical uploads/ entries — reported only, unchanged)
verdict:  PASS
```

## Dev database note

The operator's dev `rag` DB picks up 0006 at its next app-container start
(RUN_MIGRATIONS_ON_STARTUP=true) or via the documented alembic step — NOT
applied by this session (operator data is never touched unasked). The
schema is additive and ahead of code: harmless until P3.

## CI

- run on this commit: **confirmed — test success, sast success, audit success** (recorded in PROGRESS.md 2026-11-23)
