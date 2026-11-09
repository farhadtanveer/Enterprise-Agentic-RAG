# S3 — mutation re-test of the F1/F2/F6 fix code (2026-11-09)

Scratch copy outside the repo (compose `test` image, real `rag_test`
Postgres), full offline suite per mutation. Baseline in the scratch copy:
221 passed + 1 environment artifact (`test_dockerignore`, fails because the
copy has no `.dockerignore` — not a regression).

| Mutation (security line broken) | Result | Killed by |
|---|---|---|
| **M-A** — `real != plain` symlink refusal disabled in `complete_delete` (reverts to the pre-F1 resolved-path guard) | **KILLED** (2 failed) | `test_complete_delete_refuses_symlinked_intermediate_directory` (+ the dockerignore artifact) |
| **M-B** — the explicit `"/" in storage_name` check removed from `save_upload_stream` | **SURVIVED — equivalent mutant** | (see below) |
| **M-C** — `chat_failed` audit event dropped from the S1 delete-during-send handler | **KILLED** (2 failed) | `test_delete_during_send_returns_409_conversation_deleted_with_audit` (exactly-1-event + no-content assertions; both S1 variants) |
| **M-D** — the S1 409 downgraded back to 502 | **KILLED** (2 failed) | `test_delete_during_send_returns_409_conversation_deleted_with_audit` (exact-status + exact-body assertions) |
| **M-E** — PDF magic-byte check disabled (`content_matches_suffix` returns True for .pdf) | **SURVIVED → KILLED BY A STRONGER TEST** (see below) | — |
| **M-F** — NUL check disabled in the streaming text validator | **KILLED** (3 failed) | `test_text_upload_with_nul_bytes_is_422_no_row_no_file` + `test_text_validator_unit_contract` |

All mutated files were restored byte-identical afterwards.

## Survivor analysis

**M-B — equivalent mutant (accepted, no action).** On POSIX, `Path(
"sub/dir.txt").name != "sub/dir.txt"` already rejects separators; on
Windows `os.sep`/`os.altsep` cover both `/` and `\`. The explicit `"/"`
literal is redundant **defense in depth** — no observable behavior
difference exists, so no test can (or should) distinguish it. The line
stays as belt-and-braces.

**M-E — deeper-catch survivor, now killed by a stronger test.** With the
magic check disabled, a non-PDF file named `.pdf` was STILL rejected with a
4xx — but via the S2 parser-stage 422 (`row marked 'failed'`, stored file
written) instead of F6's clean pre-row 415. The review acceptance test
(`400 <= status < 500`) cannot distinguish the two. A new pin —
`test_pdf_magic_rejection_is_pre_row` — asserts **exactly 415, no document
row, no stored file**, so the fast-fail contract is enforced and the
mutant no longer survives. Added in this commit (test strengthened, never
weakened).

## Verdict

After strengthening: **6/6 mutations killed, zero survivors**. The F1/F2/F6
fix code is mutation-verified as of `dee4e09`/`ac18d9e` + this commit.
