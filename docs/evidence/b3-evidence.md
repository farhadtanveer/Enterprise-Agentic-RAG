# B3 live verification evidence (Part 2)

- **Commit:** `3098a32` + verify-script commit (B3 stack: `479c03c`, `a93adfb`)
- **Timestamp:** 2026-11-05 10:35:10Z → 10:36:50Z (UTC)
- **Command:** `bash scripts/verify/b3.sh`
- **Environment:** disposable Postgres DB `rag_verify_b3` + throwaway app
  container (compose app image + repo bind mount) on `127.0.0.1:8082`; a
  second override container on `:8085` with caps
  (`MAX_MESSAGE_LENGTH=50, MAX_MESSAGES_PER_CONVERSATION=2,
  MAX_CONVERSATIONS_PER_USER=2, CHAT_RATE_LIMIT_PER_MINUTE=2`). All torn down
  afterwards; the dev database was never touched.
- **Real graph calls: exactly 3** (send on the direct route, retry after the
  simulated graph outage, rate-limit first send). The failure-path send
  consumed zero calls (empty OPENAI key → fails at the router). The
  double-submit proof uses a seeded pending message, so both concurrent sends
  exercise the `FOR UPDATE` lock + pending check without LLM calls.
- **Result: PASS=53 FAIL=0 SKIP=0** — full script output below.

## Honest history of the three runs (rule 4: what failed first)

1. **Run 1 (10:25Z): 8 FAIL.** Verifier-side causes: the IDOR
   send/rename/delete probes carried the *victim's* CSRF token with the
   *attacker's* cookie — session-bound CSRF correctly answered 403
   csrf_failed before the ownership check (correct app behavior; the script
   now uses the attacker's own token); `grep -c` counted *lines* not
   *matches* for the two 409 bodies; the "title > max" check assumed titles
   are governed by `MAX_MESSAGE_LENGTH`, but titles have their own
   hardcoded 200-char limit (the 51-char title legitimately passed; the
   check now probes 201 chars and a separate check pins
   `MAX_MESSAGE_LENGTH` on questions); `CV2`'s id was never captured (empty
   path segment → 422).
2. **Run 2 (10:33Z): 1 FAIL.** The message-cap 409 send consumed the
   `CHAT_RATE_LIMIT_PER_MINUTE=1` allowance (the limiter runs before TX1),
   so the rate-limit test's *first* send was already 429. Fixed by giving
   the override container an allowance of 2.
3. **Run 3 (10:35Z): 0 FAIL.** No app code changed between runs — only
   `scripts/verify/b3.sh`.

## Full script output (run 3)

```
==================================================================
B3 LIVE VERIFICATION — 2026-11-05 10:35:10Z — disposable db rag_verify_b3
==================================================================
PASS  disposable app healthy  (expected=200 actual=200)
PASS  admin login ok  (expected=200 actual=200)
PASS  regular user login ok  (expected=200 actual=200)
---- CHECK CRUD: create/list/get/rename + send (REAL graph call #1, direct route)
PASS  create conversation returns id  (found: "title":"B3 live conv")
PASS  list shows the conversation  (found: 01a1ab47-4c17-7163-b670-7175d1b18763)
PASS  list carries pagination envelope  (found: "total":1)
PASS  get returns metadata  (found: "B3 live conv")
PASS  rename works  (found: "B3 renamed")
PASS  send message (real graph, direct route) -> ok  (found: "status":"ok")
PASS  assistant message carries model  (found: "model")
PASS  assistant message carries trace  (found: "trace")
PASS  assistant message carries duration_ms  (found: "duration_ms")
PASS  assistant message carries token counts  (found: "prompt_tokens")
PASS  messages list has both rows  (found: "role":"user")
PASS  assistant row present  (found: "role":"assistant")
PASS  citations persisted on assistant row  (found: "citations")
PASS  chat_asked audit row exists with route  (found: chat_asked)
PASS  audit carries no question text  (absent: helpful)
PASS  audit carries no answer text  (absent: final answer)
---- CHECK authz: 401 unauth, 403 missing CSRF, 404 IDOR on every verb, admin 404
PASS  unauthenticated list -> 401  (expected=401 actual=401)
PASS  unauthenticated send -> 401  (expected=401 actual=401)
PASS  missing CSRF on create -> 403  (expected=403 actual=403)
PASS  IDOR get -> 404  (expected=404 actual=404)
PASS  IDOR list messages -> 404  (expected=404 actual=404)
PASS  IDOR send -> 404  (expected=404 actual=404)
PASS  IDOR rename -> 404  (expected=404 actual=404)
PASS  IDOR delete -> 404  (expected=404 actual=404)
PASS  admin reading user conv -> 404  (expected=404 actual=404)
PASS  admin listing sees 0 user convs  (expected=0 actual=0)
PASS  nonexistent id -> 404  (expected=404 actual=404)
---- CHECK failure path: empty OPENAI key -> 502, rows, marker absent
PASS  graph failure -> 502  (expected=502 actual=502)
PASS  user message STAYS + error marker row  (found: user:ok)
PASS  assistant error row present  (found: assistant:error)
PASS  error row content empty (no raw exception)  (expected=1 actual=1)
PASS  chat_failed audit row exists  (found: chat_failed)
PASS  chat_failed audit has no marker  (absent: SECRET-MARKER-B3-LIVE)
PASS  marker absent from app logs on failure  (expected=0 actual=0)
PASS  retry after recovery -> 200 (REAL graph call #2)  (expected=200 actual=200)
---- CHECK double-submit: two concurrent sends serialize on the row lock
      concurrent send codes: 409 409
PASS  concurrent send one -> 409  (expected=409 actual=409)
PASS  concurrent send two -> 409  (expected=409 actual=409)
PASS  409 bodies name message_pending  (expected=2 actual=2)
---- CHECK delete conversation
PASS  delete returns status deleted  (found: "status":"deleted")
PASS  get after delete -> 404  (expected=404 actual=404)
---- CHECK caps + per-user chat rate limit (override container :8085)
PASS  title > 200 chars -> 422  (expected=422 actual=422)
PASS  question > MAX_MESSAGE_LENGTH(50) -> 422  (expected=422 actual=422)
PASS  conversation cap: 3rd create -> 409  (expected=409 actual=409)
PASS  message cap: 3rd message -> 409  (expected=409 actual=409)
PASS  rate limit: first send -> 200 (REAL graph call #3; allowance 2 — the earlier
      cap-409 send consumed one)  (expected=200 actual=200)
PASS  rate limit: immediate second send -> 429  (expected=429 actual=429)
PASS  429 carries Retry-After  (found: retry-after)
---- CHECK D19 + legacy
PASS  ask() has no follow-up context parameter (D19)  (expected=question,retrieval_filter actual=question,retrieval_filter)
PASS  legacy /api/chat still validates body (422)  (expected=422 actual=422)
PASS  legacy /api/ingest wrong key still 401  (expected=401 actual=401)
==================================================================
RESULT: PASS=53 FAIL=0 SKIP=0  (2026-11-05 10:36:50Z)
==================================================================
```

## Requirement → evidence map (review requirements a–j)

| Req | Evidence |
|---|---|
| a TX1→graph→TX2 (pool released) | Offline proof: `tests/test_conversations_api.py::test_tx_discipline_pool_released_during_graph` (production-shaped sessions; `pool.checkedout() == 0` asserted *during* the graph run) — green in the full suite (170 passed) |
| b CRUD + ownership + pagination | Live: CRUD/IDOR blocks above (404 on every verb, admin 404, pagination envelope); offline: ownership + pagination tests |
| c failure path + retry | Live: 502, user row stays, `assistant:error` with empty content, retry → 200; audit/log marker scan clean |
| d double-submit | Live: two concurrent sends both 409 `message_pending` through the row lock; offline: seeded-pending test |
| e caps | Live: question 422 at 51 chars (limit 50), conversation cap 409 at 3rd, message cap 409 at 3rd; all configurable via env (the override container IS the proof of configurability) |
| f per-user chat rate limit | Live: 200 then 429 + Retry-After keyed on the user; offline test proves another user unaffected |
| g audit contents | Live: `chat_asked` row carries route/status/counts only (question/answer strings absent); `chat_failed` same; marker absent from audit + logs; offline marker test covers success+failure through captured logs |
| h citations/trace/timings persisted | Live: assistant message payload carries citations/trace/model/tokens/duration_ms; offline test asserts exact persisted values |
| i D19 unimplemented | Live: `inspect.signature(ask)` == `{question, retrieval_filter}` |
| j legacy endpoints | Live + b2.sh: `/api/chat` 422 validation, `/api/ingest` 401 wrong key (end-to-end legacy chat call still blocked on PINECONE_API_KEY — see b2-evidence.md SKIP) |

## Note on the "direct route" choice for real sends

All three real sends used greeting-class questions that the router sends to
`direct_answer` — deliberately: with `PINECONE_API_KEY` still empty (lost in
the 2026-11-05 incident), any `kb`-routed question would fail at the
retriever. The full TX1→graph→TX2 lifecycle is exercised for real either
way. Once Pinecone is refilled, re-run `bash scripts/verify/b3.sh` and the
same script then also proves the KB route live (and b2.sh's skipped legacy
call completes).
