# B3 — conversations & chat (delivered; no STOP gate — recipe for hands-on checks)

B3 scope (§A12 row B3, plus review requirements a–j): conversations+messages
(+ message_feedback table, no endpoint) · CRUD with ownership 404s · chat via
TX1 → graph (no DB held) → TX2 · failure path with retry · per-user chat rate
limit · token/citation/trace persistence · caps. **D19 (follow-up context)
stays unimplemented** — pinned by a signature test.

Login first (cookies + CSRF recipe: docs/b2-checkpoint.md steps 1–2). All
routes below need the cookie jar; mutating ones need `X-CSRF-Token: $CSRF`
(`CSRF=$(grep csrf /tmp/b2-jar.txt | awk '{print $NF}')`).

## Create / list / rename / delete

```bash
curl -s -b /tmp/b2-jar.txt -X POST http://127.0.0.1:8081/api/conversations \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"title":"VPN help"}'
# {"id":"<CONV_ID>","title":"VPN help","last_message_at":null,...}

curl -s -b /tmp/b2-jar.txt "http://127.0.0.1:8081/api/conversations?limit=10&offset=0"
# {"items":[...],"total":1,"limit":10,"offset":0}

curl -s -b /tmp/b2-jar.txt -X PATCH http://127.0.0.1:8081/api/conversations/<CONV_ID> \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"title":"VPN + MFA"}'

curl -s -b /tmp/b2-jar.txt -X DELETE http://127.0.0.1:8081/api/conversations/<CONV_ID> \
  -H "X-CSRF-Token: $CSRF"
# {"status":"deleted"} — then GET → 404
```

## Send a message (TX1 → graph → TX2)

```bash
curl -s -b /tmp/b2-jar.txt -X POST http://127.0.0.1:8081/api/conversations/<CONV_ID>/messages \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"question":"How do I connect to the company VPN?"}'
```

Response = the assistant message: `status:"ok"`, `content`, `citations`,
`trace`, `rewritten_query`, `model`, `prompt_tokens`/`completion_tokens`,
`duration_ms`. Content lives in `messages` only — the audit row carries
counts/status/route only:

```bash
docker compose exec db psql -U rag -d rag -c \
  "SELECT event_type, outcome, duration_ms, tokens_total, details FROM audit_events
   WHERE event_type IN ('chat_asked','chat_failed') ORDER BY id DESC LIMIT 5;"
```

## IDOR check (second user)

```bash
docker compose run --rm app python -m app.cli bootstrap-admin   # or create a 2nd user via SQL for the demo
# login as the second user into a SECOND jar:
curl -s -c /tmp/b2-jar2.txt -X POST http://127.0.0.1:8081/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<other>","password":"<pw>"}' -o /dev/null
CSRF2=$(grep csrf /tmp/b2-jar2.txt | awk '{print $NF}')
for m in "GET /api/conversations/<CONV_ID>" "GET /api/conversations/<CONV_ID>/messages"; do
  curl -s -o /dev/null -w "%{http_code} $m\n" -b /tmp/b2-jar2.txt "http://127.0.0.1:8081/${m#* }"
done
curl -s -o /dev/null -w "%{http_code} POST others messages\n" -b /tmp/b2-jar2.txt \
  -X POST http://127.0.0.1:8081/api/conversations/<CONV_ID>/messages \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF2" \
  -d '{"question":"is anyone there?"}'
# → 404 on every one (existence never leaked, never 403)
```

## Failure path (graph down → 502, message stays, retry works)

Restart the app WITHOUT a key so the graph fails immediately:

```bash
docker compose --profile dev stop app
OPENAI_API_KEY= docker compose --profile dev up -d app   # empty key → graph raises
# login again (new process, old session still valid — same DB)
curl -s -b /tmp/b2-jar.txt -X POST http://127.0.0.1:8081/api/conversations/<CONV_ID>/messages \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"question":"why is the printer on fire?"}' -o /dev/null -w '%{http_code}\n'
# → 502 {"detail":"The assistant failed to answer. You can retry."} — no upstream text
docker compose exec db psql -U rag -d rag -c \
  "SELECT msg_role, msg_status, left(content,40) FROM messages
   WHERE conversation_id='<CONV_ID>' ORDER BY created_at;"
# → user row with the question + assistant row status='error', empty content
docker compose --profile dev stop app && docker compose --profile dev up -d app  # key back
# retry the same send → 200 ok (the user message was never deleted)
```

## Pending 409 (serialize while unanswered)

While a question has no assistant row after it, further sends are rejected —
this is enforced with a conversation row lock (FOR UPDATE) so concurrent
senders serialize:

```bash
docker compose exec db psql -U rag -d rag -c \
  "INSERT INTO messages (conversation_id, msg_role, msg_status, content)
   VALUES ('<CONV_ID>', 'user', 'ok', 'seeded unanswered question');"
curl -s -b /tmp/b2-jar.txt -X POST http://127.0.0.1:8081/api/conversations/<CONV_ID>/messages \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"question":"second while pending"}' -o /dev/null -w '%{http_code}\n'
# → 409 {"code":"message_pending"}
```

## Caps (defaults: 3000 chars / 200 messages / 100 conversations)

Message >3000 chars → 422; cap overruns → 409 `message_limit_reached` /
`conversation_limit_reached`. Per-user chat limit (`CHAT_RATE_LIMIT_PER_MINUTE`,
default 20/min) → 429 + `Retry-After`, keyed on the user id — another user is
never affected.

## Tests

```bash
docker compose --profile test run --rm test
```

The TX-discipline proof (review requirement a) is
`tests/test_conversations_api.py::test_tx_discipline_pool_released_during_graph`:
with production-shaped sessions, a fake graph asserts
`engine.pool.checkedout() == 0` while it runs; the marker test proves question
text never reaches audit rows or logs through both the success and failure
paths.
