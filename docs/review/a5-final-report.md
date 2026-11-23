# G2 PART A5 — final report (docs/tasks/g2.md, "PART A5 runs BEFORE Part B")

Date: 2026-11-19 (A5.1–A5.7) + 2026-11-23 (A5.8, CI audit fix on resume).
Branch `backend/multi-user`. Raw per-step evidence: **docs/evidence/g2a5-evidence.md**;
per-step log: **docs/PROGRESS.md** (2026-11-19 and 2026-11-23 sections).

| Step | Commit | Delivered | Proof | Evidence |
|---|---|---|---|---|
| A5.1 upload connection discipline | `d4dccc9` | Admin org upload restructured to TX1 (staging → pending row → commit) → ingest with NO DB held → TX2 (indexed/failed). User route already complied — pinned by test only. | Pool probe RED first (admin route held its TX across parse/embed: `assert 1 == 0`), then green (`checkedout()==0` during a slow fake ingest); admin contract tests unmodified; suite **397 both modes**. Disclosed: legacy /api/ingest (dies at B9) + operator CLI still hold a TX. | docs/evidence/g2a5-evidence.md §A5.1 |
| A5.2 quota accounting | `2c3c9ac` | `QUOTA_COUNTED_STATUSES` = pending+indexed — failed documents stop consuming quota (bytes AND count), deleted frees bytes, pending still counts; admin near-quota view uses the same accounting; stale-pending sweep (`user_upload_pending_stale_minutes=30`) + `uploads-sweep-stale` CLI. | 7 tests RED first (failed rows consumed quota; near-quota listed them; no sweep) → 35 green incl. existing quota/race/CLI suites; suite **406 both modes**. Green-phase failure was test arithmetic only (no product change). | §A5.2 |
| A5.3 mutation-harness safety | `3815274` (+ live runs `8d1996b`, `4a55afd`) | Harness can never discard uncommitted work: refuses a dirty tree (incl. untracked); backs up each target file to a per-run mktemp dir; restores from THAT copy (never git) with a byte-identical `cmp` assert; crash-safe EXIT trap. Shared `_lib.sh` + source-safe main guard + `selftest.sh` (25 checks). | selftest RED 10/15 first (sourcing the old harness STARTED a mutant run) → **25/0 green**. Live: RUN 1 12/13 killed with **M-P1 = real anchor drift** (retargeted), RUN 2 **13/13 KILLED, zero RESTORE-FAILED** through 14+ real patch/run cycles. | §A5.3 + addendum |
| A5.4 per-run verify namespaces + rejection-before-embedding + the 55,598-vector investigation | `3c45ab3` | b4/b5/b6/g1/g2a each use a unique per-run namespace (`multi-user-test-<random>`) whose prefix-guarded cleanup may delete_all; oversize/zip-bomb rejections proven to happen BEFORE any embedding (CHECK 0); `MSYS_NO_PATHCONV=1` guard on all 7 scripts; `purge-multi-user-test.sh`. 55,598 mystery closed: the 2026-11-16 by-id cleanup never completed; SIGKILL'd runs' traps can't survive; census 61 distinct test doc_ids; 414-on-large-fetch reproduced live. Purged 55,597→0 by id in 100-chunks — rag-v2 5 / legacy 27 untouched. | Live g2a.sh **42 PASS / 5 FAIL — all 5 = the Pinecone egress cap (1 GB/month) exhausted account-wide** (429 on reads; writes+stats work). Reads recover 2026-12-01 or on plan upgrade — **FARHAD'S CALL**; the 5 checks + b4/b5/b6/g1 re-runs recorded PENDING in the evidence. My purge's full-metadata fetch disclosed as a contributor to the cap. | §A5.4 |
| A5.5 no root-owned files in the repo | `cf78999` | Compose test service runs `user: "1000:1000"` with `-p no:cacheprovider` (repo-write-FREE); `no-root-files.sh` gate (root + non-world-writable discriminator, source-tree scope; uploads/data INFO-only). | Check RED first (873 historical root-owned cache artifacts, growing 565→873 within a day) → writer fix proven by BOTH-mode suites with count UNCHANGED 873→873 → purge executed **with Farhad's explicit approval** (one-shot `--user root`, three passes) → **GREEN (source tree 0)**. REPORTED, not touched: 800 root-owned `uploads/` entries (operator decides). | §A5.5 |
| A5.6 RLIMIT_AS explicit degradation | `443c8a2` | Off Linux, the parser no longer silently swallows the missing limit: platform-naming WARNING; `AttributeError` (resource without RLIMIT_AS) caught; import-safety pinned by a real subprocess test. | Stub-module tests RED first (silent degrade + uncaught AttributeError) → green; parser suite unchanged; suite **409 both modes**. | §A5.6 |
| A5.7 decorative-settings docs sweep | `d8f6504` | The four H7-removed settings confirmed gone (sample_kb_dir, pinecone_test_namespace, conversation_retention_days, test_database_url) and docs swept: README's dead `ingest_sample_kb.py` ×2 → operator CLI; `PINECONE_NAMESPACE` default → rag-v2; .env.example web-fallback template → false (amended D6); dev-testing live-Pinecone section rewritten to the keyless-suite reality. | Grep sweep CLEAN (only intentional removal notes remain); settings-audit 8/8 (allowlist still empty); suite 409 both modes. | §A5.7 |
| A5.8 CI audit fix (resume session, 2026-11-23) | `ad7adc7` | The `audit` job had been red on runs 76–78 with ONE advisory: opentelemetry-api 1.45.1 / CVE-2026-126360 (GHSA-mf5v-w3w3-f6cg, fix 1.45.2). Pin bumped one line; NO ignore flags. | OSV + PyPI verified before the bump; pip-audit clean in the rebuilt image; suite **409/0 compose AND CI-mode**; no-root-files.sh PASS; **CI run 45130905445 green (audit+test+sast)**. | §A5.8 |

Suite stands at **409 passed / 0 failed** in compose mode AND CI-mode
(`docker compose --profile test run --rm test` and the same with
`-e ENABLE_WEB_FALLBACK=false`).

## Open items for Farhad (none block PART B)

1. **Pinecone egress cap** — account-wide 1 GB/month reads exhausted
   (429). g2a's 5 blocked checks + b4/b5/b6/g1 live re-runs are recorded
   PENDING; recover 2026-12-01 or upgrade the plan.
2. **800 root-owned `uploads/` entries** — pre-appuser-era stored files,
   reported only (A5.5); operator decision.
3. **data/audit.db** — still on disk (4 stale rows, pre-B5 writer era);
   never deleted here, per the standing instruction.
4. **PART B go/no-go** — share flow, versions, governance, citations
   (docs/tasks/g2.md, session 2) awaits the explicit go.
