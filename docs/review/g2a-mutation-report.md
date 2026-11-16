# G2 PART A mutation report (docs/tasks/g2.md, 2026-11-16)

Harness: `scripts/mutation/run-g2a-mutations.sh` (hand-written mutants over
the four mandated targets, applied to a CLEAN working tree, restored
byte-identical from git and asserted after every run — the H10/A2 lesson:
commit before mutating). Final run: 2026-11-16 10:10:39Z.

## Result: 13 mutants, 13 KILLED, ZERO survivors

Baseline selections unmutated: quota 15 passed; documents 18 passed;
parser+settings 18 passed (three separate container runs shown above the
mutant list in the harness output).

| ID | Target | Mutant | Killed by |
|---|---|---|---|
| M-Q1 | quota enforcement | `with_for_update()` dropped from `enforce_private_quota` | `test_security/test_upload_quota_race.py` — the 4-thread race oversubscribes (the exact red the A2 step documented) |
| M-Q2 | quota enforcement | doc-count check replaced by `pass` | `test_doc_count_quota_409` + delete-frees-count test |
| M-Q3 | quota enforcement | `is_admin = user.role == "admin"` → `False` (exemption removed) | `test_per_file_cap_413_admin_exempt` + rate test |
| M-Q4 | quota enforcement | hourly-window check → `if False` | `test_hourly_rate_429_with_retry_after_admin_exempt` |
| M-C1 | can_read in listings | listing filter drops the private-owner clause (every private doc leaks into every listing) | `test_listing_shows_only_your_audience` |
| M-C2 | can_read in detail | `can_read` skipped in `_get_visible_or_404` | 3 tests (foreign detail, archived vanish, listing/detail matrix) |
| M-G1 | group-editor upload check | editor gate → `if False` (any member can upload) | `test_group_upload_editor_only` (member expects 403) |
| M-G2 | group-editor upload check | archived-group refusal weakened to unknown-only | `test_upload_into_archived_group_is_404` (H13 on the write path) |
| M-P1 | subprocess limits | hard timeout → `join(timeout=10**9)` | the hang test (child finishes, no verdict → 'crashed' ≠ 'timeout') |
| M-P2 | subprocess limits | pages cap → `if False` | `test_pdf_pages_are_capped` (20 > 5) |
| M-P3 | subprocess limits | zip-bomb guard → `if False` | `test_docx_zip_bomb_guard_refuses_before_parsing` (reason ≠ 'caps') |
| M-P4 | subprocess limits | chars cap ×1000 | `test_extracted_characters_are_capped` (50k > 1000) |
| M-P5 | subprocess limits | production `ingest_gate` → `IngestGate(10**9)` | `test_ingest_concurrency_setting_feeds_the_ingest_gate` (wiring pin) |

## The one first-run survivor — a harness-selection gap, fixed

Run 1 (10:02:52Z): **M-P5 SURVIVED** — because the gate-behavior test
constructs its OWN `IngestGate(1)` (it must, to exercise serialization
deterministically), the production-gate mutant never touched that
selection. The selection lacked the WIRING pin. Exactly the g1 M-C1/M-C2
lesson (a harness gap masquerading as a survivor): the harness now runs
`tests/test_settings_audit.py` alongside the parser selection, whose
`test_ingest_concurrency_setting_feeds_the_ingest_gate` asserts
`ingest_gate.limit == INGEST_CONCURRENCY`. No product code changed. Final
run: M-P5 KILLED, zero survivors.
