# g2a.sh — G2 PART A live verification evidence (docs/tasks/g2.md)

## FINAL RUN (2026-11-16 13:37:14Z): PASS=45 FAIL=0 SKIP=0, exit 0

```
command: bash scripts/verify/g2a.sh
setup:   throwaway app container rag-verify-g2a + scratch db rag_verify_g2a
         (:8089), namespace pinned multi-user-test, ENABLE_WEB_FALLBACK=false,
         tiny quotas (USER_UPLOAD_MAX_MB=1, TOTAL=2MB, MAX_DOCS=3,
         UPLOADS_PER_HOUR=5) so every quota code is exercisable.
model budget: TWO chat conversations (owner positive control + outsider
         isolation) — inside the ≤4 budget. Embeddings: ~6 small (probe +
         the two conversation retrievals).
hygiene: rag-v2 compared by EXACT vector ids before/after (the H6 KB ids,
         unchanged); legacy namespace stats 27 == 27; trap deleted the
         script's tracked vectors BY ID; scratch db dropped; no scratch
         containers left.
```

### The mandated scenarios, all green

| Mandate | Live proof (PASS lines) |
|---|---|
| quota 413 | `oversize file (2MB > USER_UPLOAD_MAX_MB=1) -> 413`; no row left |
| quota 409 (total) | `upload C -> 409` + body `{"code":"quota_total"}`; alice still exactly 2 docs |
| quota 409 (docs) | `upload E (4th doc > MAX_DOCS=3) -> 409` + body `quota_docs` |
| quota 429 + Retry-After | `hourly window exhausted -> 429` + header captured on the SAME curl |
| admin exempt | `ADMIN EXEMPT: 2MB private file -> 201` (per-file and window skipped) |
| same filename, two users | bob + carol `shared-notes.txt` → DISTINCT documents; bob 404 on carol's id; bob's listing never shows her id |
| oversize file | the 413 above |
| corrupt docx → 422 | real subprocess parse failure → generic 422, row marked `failed` (0 `indexed`, 2 `failed` — the S2/B4 contract) |
| parser-timeout file | zip-bomb docx (300MB declared) → 422 pre-parse guard, generic message |
| parallel-upload race | 4 concurrent 0.8MB uploads by erin → codes `409 201 409 201`; committed sum bounded by the 2MB quota |
| private upload owner-only | bob's private doc: vector tokens EXACTLY `['user:<bob>]` (deterministic id fetch); bob 200 / carol 404 / carol's listing never shows it; **full chat path**: bob's answer cites `bob-private.txt`, carol's answer cites `org-note.txt` and NEVER bob's doc |

## The A3 pipe deadlock — FOUND AND FIXED BY THIS SCRIPT

Runs 1–7 failed every large upload with a bogus 422 "File could not be
parsed". Root cause (commit `da0731b`): `_spawn_and_collect` joined the
child BEFORE reading the pipe — any verdict larger than the pipe buffer
(~64KB, i.e. every real document) deadlocks: the child blocks in send(),
the parent waits in join() until the 60s timeout. The parent now drains
the verdict concurrently with a deadline. Regression test
(`test_large_verdict_does_not_deadlock_the_pipe`, 200KB end-to-end) added —
it fails on the old code and protects the seam permanently.

## Environment incidents (disclosed, not product bugs)

- **Root-owned `uploads/.incoming`**: the compose TEST container runs as
  uid 0 against the bind-mounted repo; a root-owned `.incoming` (created by
  an earlier crashed run) blocked the appuser throwaway containers from
  staging uploads (PermissionError → 500). Removed with a one-shot
  `--user root` **`rmdir`** — the directory was verified EMPTY first
  (non-recursive, fails loudly if not); nothing else under uploads/ was
  touched, and no user data existed in it.
- **multi-user-test accumulation**: 55,598 leftover test vectors (mostly
  from the one run killed by a harness timeout — its trap never fired) made
  namespace-wide list+fetch probes return HTTP 414. Deleted BY ID in
  batches of 100 (never delete_all; the namespace is the designated test
  namespace). The script's per-doc probes were rewritten to recompute the
  exact production chunk ids from the stored file and FETCH by id — no
  namespace-wide listing anywhere.
- Script iteration honesty: 18 runs to green across the day. Failures were
  (in order) a stale jar reference in the script itself, the pipe deadlock
  above, quota-ladder slot arithmetic, the org note being uploaded to the
  wrong endpoint (by design `/api/documents` cannot produce org docs — the
  script now uses `/api/admin/documents`), curl-append contention under
  MSYS, and Pinecone upsert lag. NO app code was changed to make a live
  check pass EXCEPT the pipe deadlock, which is a genuine product bug with
  its own regression test.

## Model-spend disclosure

Across today's g2a runs, 8 chat conversations ran for real (2 per run × 4
qualifying runs; earlier runs 401'd before reaching chat) ≈ 24 LLM API
requests; ~10 small embeddings for probes. Per-run budget (≤4 conversations)
was never exceeded.
