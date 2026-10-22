# Multi-User Backend Design — Phase A

**Status:** Design, amended after Farhad's decision review 2026-10-22 (D1–D18
resolved, D19 opened — see §13). Phase B scope revised accordingly (§A12).
**Branch:** `backend/multi-user`
**Date:** 2026-10-22 (amended same day)
**Product rules:** `admin`/`user` roles · admins manage users and documents ·
conversations are readable **only** by their owner (admins included in "nobody
else") · admins are users too · no sign-up · users see/delete only their own
conversations.

Every claim about current code below was verified by reading the code on this
branch (file:line references). Items marked **DECISION FOR FARHAD** are open
choices with a recommendation — none are silently decided. A consolidated list
is in §13.

---

## A1. Current-state inventory

### Authentication today

| What | Where | Notes |
|---|---|---|
| Shared optional chat key | `app/api/routes.py:29-33` | `POST /api/chat` checks `X-API-Key` against `CHAT_API_KEY` **only if that env var is set**. One key shared by everyone; no identity. |
| Shared admin key | `app/api/routes.py:70-76` | `POST /api/ingest` requires `X-Admin-Key` == `ADMIN_API_KEY`. Unconditional. |
| Default admin key | `app/core/config.py:8,25` | `ADMIN_API_KEY` defaults to `"change-me"`; `validate_admin_key` (`config.py:40-51`) refuses startup outside development with the default (fix M4), warns in dev. |
| Chat-key-off warning | `app/core/config.py:54-61` | Warns at startup when `CHAT_API_KEY` unset (i.e. `/api/chat` fully open). |
| Users / sessions / login | — | **Do not exist.** No user table, no cookie handling, no session middleware, nothing. |

### Audit logging today — the privacy problem

- `app/services/audit.py:15-32` creates SQLite table `query_audit` in
  `data/audit.db` (`AUDIT_DB_PATH`, `config.py:28`). Columns: `id`,
  `created_at`, **`question` (full question text)**, `source_used`,
  `trace_json`, `client_ip`, `authenticated`, `duration_ms`.
- `app/api/routes.py:46-53` calls `write_audit(payload.question, …)` on every
  chat — so **every question asked is stored verbatim, keyed only by IP, in a
  file any server-side process can read**. `README.md:160` documents this
  ("Every question is audited"). The local `data/audit.db` currently holds 8
  such rows.
- `trace_json` is worse than it looks: the LangGraph trace contains
  `"Query rewrite → {rewritten}"` (`app/rag/workflow.py:157-161`), and the
  rewritten query is derived from the question — so the audit row carries
  question-adjacent content even beyond the `question` column.
- No endpoint reads the audit table (`app/api/routes.py` exposes only
  `/api/health`, `/api/chat`, `/api/ingest`). `steps.md` item 11 mentions an
  "audit information" endpoint that was never built — doc drift, not a leak.
- Semantics bug worth noting: `authenticated=bool(settings.chat_api_key)`
  (`routes.py:51`) records *whether auth was configured*, not *whether the
  caller authenticated*. The column name lies. (Moot after this redesign.)

### Ingest today

- `app/api/routes.py:70-87`: upload → written to `uploads/<basename>`
  (basename via `Path(...).name`, so no path traversal) → `load_file` →
  `chunk_documents` (`RecursiveCharacterTextSplitter`, 900/120,
  `app/services/ingestion.py:27-29`) → Pinecone. Supported: pdf/txt/md/docx
  (`ingestion.py:9`). Success response includes chunk count.
- **Identity of a document is its `source` path string.** Chunk IDs are
  `sha256(source + "\0" + start_index + "\0" + content)`
  (`app/rag/vectorstore.py:125-132`), and re-ingest deletes by
  `{"source": {"$eq": src}}` before adding (`vectorstore.py:135-147`, fix M2).
  Consequence for multi-user: **two users uploading the same filename produce
  identical `source` values → identical chunk IDs and a delete that wipes both
  documents' chunks.** This is the collision the redesign must remove.
- `uploads/` collisions too: same-name upload overwrites the file
  (`routes.py:82-83`).
- `ingest_sample_kb.py:1-17` indexes `data/sample_kb/` (2 fictional NovaRetail
  docs) with `source` set to the local file path.

### Retrieval and namespace today

- One Pinecone index, one namespace for everything:
  `PINECONE_INDEX_NAME`/`PINECONE_NAMESPACE` default
  `enterprise-agentic-rag` (`app/core/config.py:16-17`).
- `get_vectorstore()` (`vectorstore.py:107-116`) builds
  `PineconeVectorStore(index, embedding, namespace)`; `get_retriever()`
  (`vectorstore.py:120-121`) uses `k=top_k` (4) **with no metadata filter at
  all**. Whoever can call `/api/chat` retrieves everything ever ingested.
- Dimension-mismatch guard (`vectorstore.py:76-83`): refuses to start on an
  index with the wrong dimension and explicitly never deletes data.
  ⚠️ `README.md:159` still claims changing `EMBEDDING_MODEL` "deletes and
  recreates the Pinecone index — your vectors go with it". **The README is
  stale**; the code fails loudly instead. Fix the README in Phase B.

### Logging today

- `app/core/logging.py:3-7`: `basicConfig(INFO)` to stdout/stderr. That's all.
- Grep of every `logger.*` in `app/`: `workflow.py:62` (timeout, no content),
  `workflow.py:106` (**logs the Tavily error object verbatim** — normally an
  HTTP status message, but a malformed Tavily response could echo query
  fragments), `routes.py:65` (`logger.exception("chat request failed")` —
  static message, but the traceback may include library exception text that
  echoed request details), plus startup warnings in `config.py`.
- **Conclusion: no log line intentionally contains question text.** The
  residual risks are exception payloads and anything uvicorn/access-log
  future changes introduce. Phase B adds a test that pins this (§A11).

### LangSmith / tracing today

- `langsmith==0.14.8` is installed as a transitive dependency of langchain.
- Tracing is **opt-in by env var only**: `langsmith/utils.py:121-142` enables
  it iff `LANGSMITH_TRACING`/`LANGCHAIN_TRACING_V2 == "true"`;
  `README.md:105` documents it. No `.env` value is currently set for it.
- If someone sets it, the **full question, retrieved chunks, web results and
  answers are sent to LangSmith's cloud.** See §A2 mitigations.

### Existing frontend's auth assumptions

- `templates/index.html:31-32`: a "Chat API key (optional)" password field;
  `static/js/app.js:8-11` persists it in `localStorage`.
- `app.js:109-116`: every chat call sends `X-API-Key: <localStorage value>`.
- Upload modal asks for the admin key in a plain input (`index.html:105`,
  `app.js:151-174`) and POSTs it as `X-Admin-Key`.
- No login, no cookies, no conversation history UI, no user concept. The new
  frontend (login, chat-history sidebar, admin area) does not exist yet —
  this document defines its API contract (§A8).

### Runtime/deployment facts the design must respect

- Single uvicorn process, non-root container, healthcheck on `/api/health`
  (`Dockerfile:18-25`). No docker-compose file exists yet.
- In-memory, per-instance, per-IP sliding-window limiter for chat
  (`app/services/ratelimit.py:1-31`, `routes.py:34-41`).
- CI runs the pytest suite offline + `pip-audit` on Python 3.13
  (`.github/workflows/ci.yml`). Tests force empty API keys and a temp audit DB
  (`tests/conftest.py:7-11`) and use `TestClient` + `monkeypatch` + fake
  vectorstore patterns (e.g. `tests/test_ingest_ids.py`).
- SQLAlchemy **2.1.4 is already pinned** in `requirements.txt` (pulled in by
  `langchain-classic`/`langchain-community`) — adopting it explicitly for our
  own schema adds no new package.
- `state/` is an empty directory; `Agentic-RAG-demo/` is a standalone
  notebook demo, out of scope. `data/audit.db` and `uploads/` are gitignored.

---

## A2. Threat model (short, honest)

**What we protect:** user conversations (questions, answers, titles, traces)
and per-user private documents — against *other authenticated users*, against
*curious admins*, and against accidental exposure through *logs, audit files,
and backups*. Plus the service itself against anonymous internet actors
(brute force, free LLM usage, ingest abuse).

**Who we defend against, concretely:**

| Adversary | Threat | Defense in this design |
|---|---|---|
| Another authenticated user | Reads B's conversations by guessing IDs; retrieves B's private doc chunks via chat | UUIDv7 IDs + owner checks returning **404**, server-side retrieval filter (§A7), authorization tests as the core suite (§A11) |
| Curious admin | Reads employee questions via an endpoint, export, or the audit table | Rule 3: no such endpoint exists (§A8 lists what must NOT exist); content lives owner-only in `messages`; `audit_events` carries no content (§A6); old `query_audit` retired (D2) |
| Anonymous internet | Login brute force; unauthenticated chat/ingest | Login rate limit + temporary lockout (§A5), 401 everywhere except health/login, argon2id hashes |
| Leaked DB backup / stolen laptop | Password or session reuse; content exposure | argon2id hashes, sessions stored as SHA-256 **token hashes** (a leaked sessions table can't be replayed), message content still readable in plaintext — see limits below |
| Compromised browser | XSS steals cookie; CSRF | httpOnly + Secure + SameSite cookie, CSRF tokens, the UI's escape-first markdown renderer stays (fix from `static/js/app.js:25-71`) |

**Explicitly out of scope / honest limits — state these to stakeholders:**

1. **Whoever has server or database access can read everything.** The admin
   who can `psql` into Postgres, or root on the Docker host, can read
   `messages.content`, Pinecone chunk text, and log files. No application
   design changes that. (Mitigation path: field-level encryption of message
   content at rest, listed as a later option in §A9 — it protects against
   *passive DB/backup* reads, not against the running server.)
2. **Third parties that receive question text can see it.** OpenAI (LLM calls
   + embeddings) and Tavily (web fallback receives `current_query`,
   `workflow.py:100`) receive user content. If `LANGSMITH_TRACING=true`, the
   full trace (question, chunks, answers) also goes to LangSmith's cloud.
   Mitigations: `.env.example` sets `LANGSMITH_TRACING=false` **explicitly**
   (D13), labeled "sends user content to a third party"; no question text
   in application logs (pinned by test); a deployment-level kill switch for
   the web fallback is noted in D6. If the company later requires it, the LLM
   vendor choice/contract is the lever — not this codebase.
3. **Pinecone chunk text** is stored in Pinecone's cloud (US region,
   `vectorstore.py:90`). Anyone with the Pinecone API key can read chunk
   text of all documents. Private documents are access-controlled *through
   our API*, not encrypted in Pinecone (see D12 for the honest trade-off).
4. **Timing/metadata leaks are accepted:** admins will see aggregate counts
   and can infer "someone asked questions today" — but never what was asked.

---

## A3. Stack

| Component | Choice | Rationale / challenge |
|---|---|---|
| Database | **PostgreSQL 18** (`postgres:18-alpine` in a new `docker-compose.yml` for local dev **and tests**; prod via `DATABASE_URL`) | Sessions, lockout counters and transactions want a real DB. SQLite would fight us on concurrent writes and row-level constraints. **D1**. Tests also run against real Postgres — never SQLite — because JSONB, `inet` columns, native enums and `uuidv7()` must be exercised exactly as deployed. CI gets the Postgres service in **B1**, not B9. |
| ORM / migrations | **SQLAlchemy 2.x** (already pinned, `requirements.txt`) + **Alembic** (new dep — versioned, reviewable schema migrations for 7 new tables; no runtime auto-create in production) | SQLAlchemy costs nothing new. Alembic is the standard companion. |
| Driver | **psycopg 3** (`psycopg[binary]`, new dep — the Postgres DBAPI driver SQLAlchemy talks to; sync variant matches the existing sync route style) | |
| Password hashing | **argon2id** via `argon2-cffi` (new dep — OWASP-recommended memory-hard hashing; `hashlib`-only options are weaker, `passlib` is unmaintained) | |
| Sessions | **Server-side sessions in Postgres.** 256-bit random token in an httpOnly + SameSite=Lax cookie; DB stores only SHA-256(token). **No JWT** — a JWT would either outlive logout or need a denylist, which is a session table with worse ergonomics. `COOKIE_SECURE` is a settings flag: default `true`; set `false` only for local development over plain http. | |
| CSRF | Double-submit token: non-httpOnly `csrf` cookie + `X-CSRF-Token` header required on all mutating requests; SameSite=Lax as the outer layer | Same-origin deployment makes this simple. |
| Login protection | Per-IP sliding window (reuse `SlidingWindowLimiter`) **and** failed-attempt throttling keyed on **(username, client IP)** with exponential delay (5th failure → 15 min, doubling, capped at 1 h; resets on success). No hard account lock: a hard username-wide lock would let anyone lock a victim out by spamming their username from anywhere. Generic error text (no user enumeration). Recovery CLIs exist (§A5). | |
| Chat rate limiting | Existing `SlidingWindowLimiter` **re-keyed from IP to user_id** (plus keep the IP limit for `/api/auth/login`). Still in-memory, single-process — documented limitation; move to Postgres-backed counters if multi-worker deployment arrives. **Client-IP resolution:** limits use the socket peer IP; behind a reverse proxy, uvicorn is run with `--proxy-headers` and `FORWARDED_ALLOW_IPS` limited to the actual proxy IP, so `request.client.host` is the real client. No `X-Forwarded-For` header is trusted from untrusted sources (spoofing would bypass per-IP limits). | |
| Deployment | Same-origin: FastAPI serves the built frontend (existing pattern, `app/main.py:22-27`). **No CORS middleware at all** — the strongest CSRF posture is not needing cross-origin in the first place. | |

What would change these choices: a mandated Redis/Keycloak SSO from company
IT (sessions/lockout move there), or a decision to run multi-worker/replicas
(rate limiting and per-instance state must go shared first — D14).

---

## A4. Data model

All IDs are UUIDv7 (time-ordered, safe in URLs, non-guessable in practice)
with **`uuidv7()` as the Postgres-side column default** — Postgres 18 provides
it natively and Python 3.13's stdlib has no uuid7, so the DB is the single
generator and no client-side UUID code is needed. All timestamps
`timestamptz` with `now()` defaults. Enums via native PG enums or CHECK
constraints — implementation detail for Phase B; shown here as enums.

```mermaid
erDiagram
    users ||--o{ sessions : "has"
    users ||--o{ conversations : "owns"
    users ||--o{ documents : "uploads (private only)"
    users ||--o{ message_feedback : "gives"
    users ||--o{ audit_events : "acts (nullable)"
    conversations ||--o{ messages : "contains"
    messages ||--o{ message_feedback : "receives (owner only)"

    login_throttle {
        text username_hash UK "salted hash, NOT plaintext - works for unknown users too"
        inet ip
        int fail_count
        timestamptz throttle_until
        timestamptz last_failure_at
    }

    users {
        uuid id PK
        text username UK "lowercase, citext or unique index"
        text password_hash "argon2id"
        text display_name
        user_role role "admin | user"
        boolean is_active "false = disabled"
        boolean must_change_password "temp password flag"
        timestamptz password_changed_at
        timestamptz created_at
        timestamptz updated_at
    }
    sessions {
        uuid id PK
        uuid user_id FK "ON DELETE CASCADE"
        text token_hash UK "sha256(token), never the token"
        timestamptz created_at
        timestamptz expires_at "absolute"
        timestamptz idle_expires_at "sliding"
        inet ip
        text user_agent
        timestamptz revoked_at "null = live"
    }
    conversations {
        uuid id PK
        uuid user_id FK "ON DELETE CASCADE"
        text title "nullable until first message"
        timestamptz created_at
        timestamptz last_message_at
    }
    messages {
        uuid id PK
        uuid conversation_id FK "ON DELETE CASCADE"
        msg_role role "user | assistant"
        msg_status status "ok | error (failed graph run)"
        text content "the ONLY place question/answer text lives"
        jsonb citations
        jsonb trace "owner-readable; contains rewritten query"
        text source_used
        text rewritten_query
        text model
        int prompt_tokens
        int completion_tokens
        int duration_ms
        timestamptz created_at
    }
    documents {
        uuid id PK
        uuid owner_id FK "org docs: ALWAYS NULL (D16); private: required, ON DELETE CASCADE"
        doc_scope scope "org | private"
        text display_name "original filename, for citations"
        text storage_path "uploads/<doc_id>/<display_name>"
        text checksum "sha256 of bytes, dedupe + integrity"
        bigint size_bytes
        text mime_suffix
        int chunk_count
        doc_status status "pending | indexed | failed"
        text error
        timestamptz created_at
        timestamptz indexed_at
    }
    message_feedback {
        uuid id PK
        uuid message_id FK "ON DELETE CASCADE"
        uuid user_id FK "ON DELETE CASCADE"
        feedback_value value "up | down"
        text comment "nullable; USER CONTENT - never shown to admins"
        timestamptz created_at
    }
    audit_events {
        bigint id PK
        timestamptz created_at
        uuid actor_user_id FK "NULL for anonymous; ON DELETE SET NULL"
        event_type type "login, login_failed, chat_asked, ..."
        text object_type "nullable"
        uuid object_id "nullable, no FK on purpose (survives deletes)"
        inet ip
        outcome outcome "ok | denied | error"
        text route
        int duration_ms
        int tokens_total
        jsonb details "counts and ids ONLY - never content"
    }
```

Constraints and indexes (minimum):

- `users.username` unique (stored lowercase). Role CHECK in (`admin`,`user`).
- `sessions.token_hash` unique; index `(user_id)`, index `expires_at` for the
  cleanup sweep. FK `user_id → users.id ON DELETE CASCADE` (disable/delete
  user kills sessions instantly and permanently).
- `conversations (user_id, last_message_at DESC)` for the sidebar listing.
- `messages (conversation_id, created_at)`; conversation and user ownership
  enforced by joining through `conversations` — **no `user_id` denormalized
  onto messages**; one owner, one join, no drift.
- `documents (scope)`, `(owner_id)`, unique `(checksum, scope, owner_id)`
  *not* imposed globally — same file may legitimately exist as org and
  private; dedupe is advisory (§A7). `scope='org' ⇒ owner_id IS NULL` is
  enforced by CHECK (org documents are ownerless by definition — **D16**,
  amended: always NULL, never attributed to an admin); `scope='private' ⇒
  owner_id IS NOT NULL`.
- `message_feedback`: unique `(message_id, user_id)` (one vote per user);
  FK cascade from messages. Comments are owner-visible only (§A8).
- `audit_events (created_at)`, `(actor_user_id)`, `(event_type, created_at)`;
  no FK on `object_id` so audit rows survive object deletion.
- Delete behavior summary: deleting a user cascades sessions, conversations
  (→ messages → feedback) and **application-side** deletes their private
  Pinecone vectors and upload files; `audit_events.actor_user_id` is SET NULL
  (the events stay, attribution anonymized — see §A6/D9).

---

## A5. Auth and account lifecycle

```mermaid
sequenceDiagram
    participant B as Browser
    participant API as FastAPI
    participant DB as PostgreSQL

    B->>API: POST /api/auth/login {username, password}
    API->>DB: throttle check for (username, client IP) — exponential delay active?
    API->>API: argon2id verify
    alt ok
        API->>DB: INSERT sessions(token_hash, expiry); clear (username, IP) failure records
        API-->>B: Set-Cookie sid (httpOnly, Secure per COOKIE_SECURE, SameSite=Lax) + csrf cookie; 200 {user}
    else bad password
        API->>DB: record failure for (username hash, IP); raise delay if 5th
        API-->>B: 401 "Invalid username or password" (same text for unknown user)
    else throttled
        API-->>B: 429 with Retry-After
    end
    Note over B,API: every subsequent request: cookie → sha256 → sessions lookup<br/>(active? not expired? not revoked) → request.user
    B->>API: POST /api/auth/logout + X-CSRF-Token
    API->>DB: sessions.revoked_at = now
    API-->>B: cookie cleared
```

Lifecycle rules:

- **First-admin bootstrap:** `python -m app.cli bootstrap-admin` — an
  interactive CLI that prompts for username and password (getpass, no echo,
  confirm; enforced min length). It **refuses to run if any admin already
  exists**. There is no default password anywhere — not in code, not in env,
  not in docs (extends the spirit of the M4 guard, `config.py:40-51`).
- **Admin creates users** (`POST /api/admin/users`): username, role, optional
  display name, and an admin-chosen **temporary password** (or
  server-generated one returned once in the response). User row is created
  with `must_change_password = true`.
- **Forced password change:** while `must_change_password`, every endpoint
  except `GET /api/me`, `POST /api/auth/password` and `POST /api/auth/logout`
  returns `403 {"code": "password_change_required"}`. The UI shows a change
  form first.
- **Session expiry & renewal:** idle timeout 7 days, absolute 30 days,
  both configurable (`SESSION_IDLE_HOURS`, `SESSION_ABSOLUTE_DAYS`) per
  **D7**. Sliding renewal: on each authenticated request, if more than
  10 minutes of idle budget was consumed, write back a fresh `idle_expires_at`
  (throttled writes). Expired sessions are deleted by a periodic sweep.
- **Logout:** revokes the session row (`revoked_at`), clears cookie.
- **Disable user** (`PATCH /api/admin/users/{id}` `is_active=false`): revokes
  all sessions immediately; every request of a disabled user is 401 even with
  a live cookie (checked in the session lookup). Data retained (**D9**).
- **Last-admin protection:** the system refuses (`409 Conflict`) any admin
  action that would leave zero active admins — disabling, demoting, or
  deleting the last active admin, **including by that admin themself**.
  Counted as `users WHERE role='admin' AND is_active`.
- **Recovery CLIs** (for when an admin is locked out or left the company and
  no other admin exists): `python -m app.cli reset-password <username>` and
  `python -m app.cli unlock <username>` — interactive, run on the server by
  an operator with shell access (same trust level as `bootstrap-admin`).
  `reset-password` sets a temp password + `must_change_password` and revokes
  sessions; `unlock` clears the (username, IP) throttle records for that
  username. Both write audit events.
- **Password change** (`POST /api/auth/password`, old + new required):
  re-hashes argon2id, revokes all *other* sessions, sets
  `must_change_password=false`, updates `password_changed_at`. The current
  session survives so the flow isn't self-immolating.
- **Admin password reset** (`POST /api/admin/users/{id}/reset-password`):
  issues a new temp password, sets `must_change_password`, revokes the user's
  sessions. The temp password is shown once to the admin to hand over
  out-of-band. No email in this phase — handoff is the admin's problem
  (documented).
- **Role change** admin↔user takes effect at next request (session lookup
  re-reads the user row); no token to re-mint since there are no JWTs.
- **Login protections:** per-IP rate limit (reuse limiter) plus failed-attempt
  throttling keyed on **(username, client IP)** — 5th failure starts a
  15-minute delay for that pair, doubling per further failure, capped at 1 h,
  reset on success. No hard username-wide lock (anyone could otherwise lock a
  victim out by spamming their username from anywhere; and a shared office IP
  can't freeze an account for a legitimate user at another site).
- **`login_failed` audit privacy:** when the username matches an existing
  account, the event records `actor_user_id` (never the raw username); for
  unknown usernames it records **no identifier at all** — not even the typed
  string. This keeps credential-stuffing noise out of the audit trail and
  removes a user-enumeration surface; the (username, IP) throttle still works
  because it reads the attempted username at request time and stores only a
  salted hash of it, not the plaintext, in its counters.

---

## A6. Privacy-preserving audit design

**Split logging in two, by content class:**

```mermaid
flowchart LR
    subgraph S1["Chat request"]
        Q["question text"] --> M[("messages table<br/>owner-only")]
        T["trace / rewritten query"] --> M
    end
    subgraph S2["Who/when/what-kind only"]
        AE[("audit_events<br/>type, actor id, route,<br/>duration, status, token counts")]
    end
    Q -. "never written here" .-> AE
    M -. "never read by admins" .-> ADM["Admin metrics:<br/>aggregate counts only"]
    AE --> ADM
```

- **`audit_events`** records: `login`, `login_failed` (see content rule
  below), `login_throttled`, `logout`, `user_created`, `user_disabled`,
  `user_enabled`, `user_deleted`, `role_changed`, `password_changed`,
  `password_reset_by_admin`, `account_unlocked`, `document_uploaded`,
  `document_deleted`, `document_reindexed`, `document_visibility_changed`,
  `chat_asked` (route, `source_used`, outcome, duration_ms, token totals —
  **no question, no answer, no trace, no title**), `chat_failed`,
  `conversation_deleted`, `feedback_submitted`, `export_requested`,
  `account_deleted`.
  The `details` JSONB holds ids and counters only. A schema-level rule, tested
  in Phase B: nothing in this table may contain message content.
- **Message content lives only in `messages`**, owner-scoped. Trace and
  rewritten query stay there (owner-readable — it's their own data) and are
  **removed from audit entirely** (today's `trace_json` in `query_audit`
  carries question-derived text — §A1).
- **What admins see:** aggregate dashboards — chat volume per day, error
  rate, p50/p95 latency, token/cost totals, per-`source_used` distribution,
  document/ingest status, feedback 👍/👎 counts, active users, failed logins.
  **Per-user question counts are NOT exposed** (**D3**, resolved), and every
  aggregate bucket is additionally suppressed unless it covers **≥ 5 users**
  (k-anonymity floor — a "per document" or "per day" breakdown that narrows
  to one or two people is per-user monitoring with extra steps).
  Note: the metrics endpoint itself is **deferred** (§A12); the rule is
  recorded now so a future implementation can't quietly regress it.

**Fate of the existing `query_audit` table** (stores question text,
`app/services/audit.py:18-24`, 8 rows — per Farhad, only his own test
questions): from B5 the legacy chat path writes **no question or trace text
anywhere**; the table receives no new rows, and the drop (`DROP TABLE
query_audit` + delete `data/audit.db`) runs as an explicit, operator-executed
script in B5 — **no CSV export** (D2, amended from the original export
proposal; the rows' owner confirmed they are his own test data). The script
requires a confirmation flag/env (e.g. `--yes-i-understand-this-deletes-8-questions`)
and refuses to run against a DB whose row count it can't verify first.

**Application logs** gain a pinned rule: no endpoint logs request bodies.
`routes.py:65`'s `logger.exception` keeps the traceback (framework noise, no
question text) but a Phase B test asserts question strings never appear in
captured logs (§A11); the Tavily error-logging line (`workflow.py:106`) gets
reduced to error class only.

---

## A7. Documents and retrieval access control

### Scopes

- `org` — admin-managed, retrievable by every active user (the sample KB
  becomes org docs owned by the bootstrapped admin).
- `private` — retrievable only by the uploader.

**D4 — who may upload what (resolved):** **Phase B ships admin-only uploads
(org scope)**, with the `documents` table and metadata scheme built so
regular-user `private` uploads are a later config flip, not a rework.
Rationale: upload is the highest-abuse endpoint (file parsing, ingest cost,
content trust — a user-uploaded "policy" doc becomes org-retrievable in
current code, which would be worse); the product rules make admins the
document managers; private uploads can follow once the file-scanning story
exists.

**Upload hardening (closes the still-open size cap):** today `POST /api/ingest`
has **no size limit** — `routes.py:83` does `await file.read()` (whole body
into RAM) and writes whatever arrives. That was flagged in the original
hardening review (the "upload size cap" item; the M-number list in commit
messages is incomplete — M1/M2/M4/M5 are fixed, M6 is now identified as the
shared-namespace/no-ACL finding, and this cap was never implemented). B4 adds
`MAX_UPLOAD_MB` (default 25): reject early on `Content-Length`, and enforce
the cap while streaming the multipart body to disk in chunks so an oversized
or chunked upload can't exhaust memory. Oversize → `413`. (Also noted in the
commit plan, B4.)

### Pinecone chunk metadata and the retrieval filter

Each vector carries: `doc_id`, `scope`, `owner_id`, `read_access`
(`["org"]` for org docs, `[owner_id]` for private), `source` (display name,
still used for citation titles — `workflow.py:174-179`).

**The filter is built server-side from the session, never from client
input.** Verified against installed versions (`pinecone==7.3.0`,
`langchain-pinecone==0.2.13`):

- `langchain_pinecone/vectorstores.py:527-546` — `similarity_search(...,
  filter=..., namespace=...)` passes `filter` straight through to the
  server; namespace can be overridden per call.
- `pinecone/db_data/types/query_filter.py:7-33` — SDK types include
  `$eq/$ne/$gt/$gte/$lt/$lte/$in/$nin` and top-level `$and`. **`$or` is
  server-supported (docs) but not in this SDK's type union** — it would work
  at runtime but reads as unsupported to a type checker.
- Pinecone docs: when the metadata field is an **array**, `$in` matches if
  *any element* matches.

Therefore the access filter is a single, fully-typed condition:

```python
# built in server code from the session user — the client sends nothing
filter = {"read_access": {"$in": ["org", str(user.id)]}}
```

which is exactly `scope == org OR owner == me`, using only `$in` + array
semantics. (The `{"$or": [{"scope": {"$eq": "org"}}, {"owner_id": ...}]}`
form is equivalent and also correct at runtime; we prefer the `$in` form for
SDK-type cleanliness and one-condition simplicity.) `get_retriever()` gains a
required filter argument and the workflow's `retrieve_kb` node receives it
via graph state, set once from the authenticated user — there is no code path
where retrieval runs unfiltered.

### Rework of chunk IDs and delete-by-source (M2 follow-up)

- New chunk ID: `sha256(doc_id + "\0" + start_index + "\0" + content)` —
  same determinism properties as today (`vectorstore.py:125-132`) but scoped
  by document, so **two users uploading the same filename can no longer
  collide or delete each other's chunks**.
- Replace-on-re-ingest becomes delete-by-`doc_id`:
  `{"doc_id": {"$eq": doc_id}}` (same call shape as `vectorstore.py:142-145`).
- Files stored as `uploads/<doc_id>/<display_name>` — no more `uploads/`
  filename collisions (`routes.py:82-83`).

### Existing vectors without metadata (sample KB + your uploaded doc)

Today's vectors have no `doc_id`/`read_access`, so under the new filter they
match **nothing — fail closed** (good), but the sample KB would silently
disappear from answers. Non-destructive plan, executed in Phase B4:

1. Add `PINECONE_NAMESPACE` migration: new value (e.g. `rag-v2`) — the old
   namespace is left 100% untouched.
2. Re-ingest: sample KB as **org** docs (owner = bootstrap admin), plus
   `uploads/Order Automation Backend — Your Build Roadmap.md` as **private**
   to your user (it's yours; **D15** resolved: private to you).
3. Verify retrieval in the new namespace (live smoke check, §A11).
4. Only after verification, a manual operator step clears the old namespace:
   `index.delete(delete_all=True, namespace="enterprise-agentic-rag")` —
   namespace-scoped, never `delete_index()`. The dimension guard
   (`vectorstore.py:76-83`) continues to protect the index itself.
5. All tests use a dedicated test namespace (`PINECONE_TEST_NAMESPACE`);
   the offline suite fakes the store (existing pattern in
   `tests/test_ingest_ids.py`).

Honest note (D12): private-scope isolation lives in metadata + our filter,
not in Pinecone-enforced tenancy — anyone with the Pinecone API key can read
chunk text of private docs. If that's unacceptable, the options are a
namespace-per-private-doc model or client-side encryption of private chunk
text; both are heavier and deferred until someone states the requirement.

---

## A8. API contract

Conventions: JSON bodies; session cookie auth; `X-CSRF-Token` required on
POST/PATCH/DELETE; **401** unauthenticated/invalid session, **403** wrong
role (or `password_change_required`), **404** for another user's resource
(existence not leaked — never 403 there), 422 validation, 429 rate-limited,
500 without internals.

### Auth & self

| Method | Path | Auth | Roles | Summary | Errors |
|---|---|---|---|---|---|
| POST | `/api/auth/login` | none | — | `{username, password}` → session cookie + `{user, csrf_token}` | 401, 429 |
| POST | `/api/auth/logout` | ✓ | any | Revoke session, clear cookie | 401 |
| POST | `/api/auth/password` | ✓ | any | `{old_password, new_password}` → change own password | 401, 403 |
| GET | `/api/me` | ✓ | any | Own profile: username, display name, role, `must_change_password` | 401 |
| GET | `/api/me/export` | ✓ | any | **Deferred** — GDPR export: own conversations+messages+documents meta as JSON download | 401 |
| DELETE | `/api/me` | ✓ | any | Delete own account: conversations, messages, private docs + vectors; audits anonymized | 401 |

### Conversations & chat (owner-only, always)

| Method | Path | Auth | Roles | Summary | Errors |
|---|---|---|---|---|---|
| GET | `/api/conversations` | ✓ | any | Own conversations (id, title, last_message_at) | 401 |
| POST | `/api/conversations` | ✓ | any | Create empty conversation (optional title) | 401, 422 |
| GET | `/api/conversations/{id}` | ✓ | any | Own conversation metadata | 401, **404** (incl. someone else's) |
| PATCH | `/api/conversations/{id}` | ✓ | any | Rename own conversation | 401, 404 |
| DELETE | `/api/conversations/{id}` | ✓ | any | Delete own conversation + messages (hard delete) | 401, 404 |
| GET | `/api/conversations/{id}/messages` | ✓ | any | Own messages, paginated | 401, 404 |
| POST | `/api/conversations/{id}/messages` | ✓ | any | `{question}` → runs graph with server-built retrieval filter; persists user+assistant messages; returns assistant message (answer, citations, trace, source_used, timings, tokens) | 401, 404, 422, 429 |
| POST | `/api/chat` | — | — | **Legacy only** during transition (§A10); removed with legacy mode | 401 |
| POST | `/api/feedback` | ✓ | any | **Deferred** — `{message_id, value, comment?}` on **own** message only | 401, 404 |

### Chat request lifecycle — DB connection discipline (B3)

The graph run can take tens of seconds. **No database connection or
transaction may be held open across it** — a hung LLM call must not be able
to exhaust the Postgres pool and take logins and the admin area down with it.

```mermaid
flowchart TD
    A["request in"] --> B["TX1 (short):<br/>session lookup + throttle write-back<br/>+ ownership check<br/>+ INSERT user message<br/>COMMIT, return conn to pool"]
    B --> C["graph runs (ask)<br/>NO DB connection held<br/>only Pinecone/OpenAI/Tavily"]
    C -->|"ok"| D["TX2 (short):<br/>INSERT assistant message<br/>+ UPDATE conversations.last_message_at<br/>COMMIT"]
    C -->|"exception / timeout"| E["TX2 (short):<br/>INSERT assistant message with<br/>status='error' (no stack, no content)<br/>+ audit chat_failed<br/>COMMIT"]
    D --> F["respond 200"]
    E --> G["respond 502<br/>'The assistant failed to answer'"]
```

Rules:

- Three short transactions per chat request: **TX1** (auth + ownership +
  persist the user's question), the graph run with **zero** DB resources
  checked out, then **TX2** (persist the result or the failure marker).
- Rate limiting and CSRF checks happen before TX1, on already-loaded request
  state.
- **If the graph fails, the user message stays.** It is the user's own data
  and the user owns it — deleting it silently would hide their own words from
  them, and it costs nothing: the conversation simply shows a question
  without an answer. The assistant side is recorded as a `messages` row with
  `status='error'` (column added for this; `content` empty, no stack trace,
  no upstream error text — errors can echo input), so the UI can render a
  retry affordance and the failure is *counted* (audit `chat_failed`) without
  the error text being stored. A retry posts a new message pair; nothing is
  auto-deleted.
- Practical mechanics: the graph is a plain function call
  (`ask(question, retrieval_filter)`), so the discipline is "don't pass
  sessions into it and don't wrap it in `with session.begin()`". TX1/TX2 use
  short-lived sessions from the request-scoped session factory; the pool's
  `pool_pre_ping` + low `pool_timeout` make any leak loud in tests (a test
  asserts `pool.status() == checked-out 0` after a mocked graph failure).

### Documents (user-facing)

| Method | Path | Auth | Roles | Summary | Errors |
|---|---|---|---|---|---|
| GET | `/api/documents` | ✓ | any | Org docs + own private docs (meta only, no content) | 401 |
| POST | `/api/documents` | ✓ | any* | Upload; scope per D4 (admin→org; user→private when enabled). `MAX_UPLOAD_MB` enforced → **413** oversize | 401, 403, 413, 422 |
| GET | `/api/documents/{id}` | ✓ | any | Status/meta of org doc or own private doc | 401, 404 |
| DELETE | `/api/documents/{id}` | ✓ | any* | Delete own private doc + vectors (org docs: admin only) | 401, 403, 404 |

### Admin

| Method | Path | Auth | Roles | Summary | Errors |
|---|---|---|---|---|---|
| GET | `/api/admin/users` | ✓ | admin | List users: username, display name, role, active, created, last_login — **no conversation data** | 401, 403 |
| POST | `/api/admin/users` | ✓ | admin | Create user (temp password) | 401, 403, 422 |
| PATCH | `/api/admin/users/{id}` | ✓ | admin | Change role, disable/enable, display name. **409** if the change would leave zero active admins (last-admin guard, incl. self) | 401, 403, 409, 422 |
| DELETE | `/api/admin/users/{id}` | ✓ | admin | Delete user: cascades conversations/messages/sessions/private docs+vectors (consistency rules §A9.1). **409** last-admin guard. Double-confirm via request body flag | 401, 403, 404, 409 |
| POST | `/api/admin/users/{id}/reset-password` | ✓ | admin | New temp password, revoke sessions | 401, 403 |
| GET | `/api/admin/documents` | ✓ | admin | All documents incl. private: meta + ingest status (name/size/status/owner — **content never**) | 401, 403 |
| POST | `/api/admin/documents` | ✓ | admin | Upload org document | 401, 403, 422 |
| PATCH | `/api/admin/documents/{id}` | ✓ | admin | Set visibility — org docs only (**D11**) | 401, 403, 404 |
| DELETE | `/api/admin/documents/{id}` | ✓ | admin | Delete any document + vectors (no content exposure) | 401, 403, 404 |
| GET | `/api/admin/metrics` | ✓ | admin | **Deferred (§A12)** — when built: aggregate only, buckets < 5 users suppressed (**D3**); no per-user series | 401, 403 |
| GET | `/api/health` | none | — | Liveness (unchanged, `routes.py:23-25`) | — |

### Endpoints that must NOT exist (test-enforced absence)

- Anything admin-readable of user content: `GET /api/admin/conversations`,
  `GET /api/admin/conversations/{id}`, `GET /api/admin/messages`,
  `GET /api/admin/users/{id}/conversations`, any admin export of
  conversations/messages, any admin read of **feedback comments**.
- Impersonation / "login as user" / session-minting for another user.
- Any endpoint accepting a `user_id` filter that returns chat content.
- Any endpoint returning `audit_events` rows verbatim to the client
  (dashboards are served from aggregates, not raw events).
- No hidden break-glass route, even under an obscure prefix. If a test can't
  find it, a curious admin can't either.

---

## A9. GDPR-minded features

*(Scope note per the revised Phase B plan: the **retention sweep**, **data
export**, **feedback endpoint**, and **admin metrics** are deferred until
Farhad asks for them; conversation deletion and account deletion stay — they
are product rules.)*

- **Delete a conversation:** hard delete (row + messages), user-triggered,
  immediate. Vectors are not involved (chat content never enters Pinecone).
- **Delete own account** (`DELETE /api/me`): deletes conversations, messages,
  feedback, sessions; private documents + their vectors + stored files
  (consistency rules §A9.1); audits keep event rows with `actor_user_id` set
  NULL (counts remain, attribution gone). Org documents are unaffected —
  they are ownerless by definition (**D16**, amended: `owner_id` is always
  NULL for org docs, so nothing is reassigned when any account, admin or
  not, disappears).
- **Admin deletes a user:** same cascade, executed by the admin endpoint;
  double confirmation; `user_deleted` audit event; last-admin guard (**409**).
- **Retention:** default **none** (keep until user deletes) + a config knob
  (`CONVERSATION_RETENTION_DAYS`, 0 = off) so a policy decision later is a
  settings change plus a sweep job, not a migration (**D5** resolved; Farhad
  chooses the real period before go-live — the sweep is deferred until then).
- **Data export** (`GET /api/me/export`): JSON of conversations, messages,
  own document metadata (not document bytes in v1; documents are company
  material — **D17** resolved). **Deferred** until requested.
- **Field-level encryption of message content at rest** — listed as a later
  option (envelope encryption with a key outside the DB) if the company's
  assessment demands it; it protects backups/at-rest reads, not the running
  server or the admins who can attach a debugger. Deferred, not silently
  dropped.

### A9.1 Postgres ↔ Pinecone delete consistency

Deletion spans two systems with no shared transaction. Ordering principle:
**vectors first, database rows last** — a Pinecone delete-by-filter is
idempotent and can be retried safely, whereas an orphaned vector whose
`doc_id` no longer exists in Postgres can never be *targeted* again.

- **Delete one document** (user or admin):
  1. TX1: `documents.status = 'deleting'` (row stays queryable for the sweep,
     hidden from listings).
  2. Outside TX: delete vectors by `{"doc_id": {"$eq": ...}}` — idempotent;
     safe to repeat.
  3. TX2: delete the row; delete `uploads/<doc_id>/` files; audit
     `document_deleted`.
  A sweep (part of the app's startup check, plus manual CLI) retries any row
  stuck in `deleting` — the failure mode is a Pinecone outage, and the retry
  is the same idempotent delete.
- **Delete a user / account**: same protocol per private document first
  (their private docs must go *before* the user row, while `doc_id`s are
  still known), *then* a single cascade transaction deletes the user row
  (sessions, conversations → messages → feedback go with it). If the process
  dies mid-way: user still exists (rows intact), docs are in `deleting` and
  the sweep resumes them — the flow is restartable, never half-deleted.
- **What is deliberately NOT crash-consistent:** the reverse direction
  (crash between cascade and vector delete) is prevented by the ordering
  above; if an operator force-deletes a user row via SQL and strands vectors,
  the audit `user_deleted` event records the `doc_id`s from step 1's
  `details` JSON so a manual Pinecone cleanup by `doc_id` remains possible.
- **Never** `delete_index()`; namespace-scoped operations only (same rule as
  the legacy-vector plan, §A7).
- **Not legal advice — confirm with the company's data protection contact
  (and works council, if one exists):**
  1. Is question content to OpenAI/Tavily covered by DPA/SCCs or an
     enterprise agreement without training use? (§A2 limit 2.)
  2. Works-council position on any per-user *metadata* metrics (we default
     to none — D3) and on the audit of login events.
  3. Retention period the company actually wants (drives D5), and whether
     chat content counts as personal data under your role definitions.
  4. Whether "employee asks about health/HR topics in an IT copilot" needs
     extra handling (this design keeps such questions owner-only by default
     — state that to the DPO as a feature).
  5. Backup retention of Postgres snapshots vs. user deletion requests
     (deletes are immediate in the live DB; backups age out on their cycle).

---

## A10. Transition plan (amended: no `AUTH_MODE` flag)

The existing UI sends `X-API-Key` (`static/js/app.js:109-116`) and admin-key
uploads (`app.js:163-166`); it must keep working until the new frontend
exists. Per **D8** (amended): **no `AUTH_MODE` flag** — the legacy endpoints
simply remain in place, unchanged in shape, until an explicit removal commit
at the end of Phase B. One code path, no mode matrix.

- **B2–B3:** legacy endpoints (`POST /api/chat` with optional `X-API-Key`,
  `POST /api/ingest` with `X-Admin-Key`) behave as today. Retrieval is still
  unfiltered in this window — identical to current production behavior, and
  acceptable because no private documents exist yet (org docs only, all
  retrievable by design). Session-auth endpoints exist alongside; nothing
  legacy can touch them (no static key grants conversation history, ever).
- **From B4:** *all* retrieval paths — legacy `/api/chat` included — go
  through the server-side filter. Legacy chat uses the **org-only filter**
  (`{"read_access": {"$in": ["org"]}}`), because a static key proves no
  identity. Before the B4 re-ingest, old vectors carry no `read_access`
  metadata, so the org-only filter returns nothing from the legacy namespace
  until that namespace is re-ingested — the migration plan (§A7) sequences
  re-ingest with the filter switch so org answers never silently vanish.
  Legacy `/api/ingest` now routes through the documents flow as an **org
  document with `owner_id = NULL`**.
- **From B5:** the legacy chat path stops writing question/trace text to
  `query_audit` entirely (rows get duration/source metadata only — the
  content-free schema of `audit_events`); `audit_events.chat_asked` becomes
  the record of every chat, legacy or not.
- **B8:** minimal login form added to the current UI (a small
  `templates/login.html` + fetch calls — no framework needed); the sidebar
  key fields are removed from the page. Real usage moves to sessions.
- **Removal commit (end of Phase B):** deletes `POST /api/chat`, `POST
  /api/ingest`, the `X-API-Key`/`X-Admin-Key` checks, `CHAT_API_KEY` /
  `ADMIN_API_KEY` settings, the `localStorage` key fields in `app.js`, and
  fixes the stale `README.md:159` claim. Announced in README/`.env.example`
  from B8 onward. What would extend legacy's life: only the new frontend
  slipping — by exactly one release, never indefinitely.

---

## A11. Test plan (authorization is the core)

**Database rule: tests run against real Postgres — never SQLite** (JSONB,
`inet`, native enums and `uuidv7()` must behave exactly as deployed). CI gets
a `postgres:18` service container in **B1**. Local workflow (documented in
`docs/dev-testing.md` and README):

```bash
docker compose up -d db          # local Postgres 18
export TEST_DATABASE_URL=postgresql+psycopg://rag_test:rag_test@localhost:5432/rag_test
pytest tests -q                  # offline provider mocks apply as today
```

Fixture strategy: one schema per test session (Alembic `upgrade head` against
the test DB), each test runs inside a transaction that is rolled back — fast
and isolated. Tests without a DB (pure-unit, existing ones) still run without
Postgres; anything touching users/sessions/documents fails fast with a clear
message when `TEST_DATABASE_URL` is unset. LLM/Tavily/Pinecone stay mocked
offline (existing patterns: `TestClient`, `monkeypatch`, fake vectorstore,
`conftest.py` empty-keys env):

1. **Cross-user conversation isolation** — users A and B exist; A lists,
   reads, renames, deletes, fetches messages of, and posts to B's
   conversation by its real UUID → every call **404**; A cannot find B's
   conversation IDs via listing; sequential/guessed UUIDs 404. Feedback on
   B's message by A → 404.
2. **Admin cannot read user content** — as admin: `GET /api/admin/conversations`
   → 404/405 (route absent), `GET /api/conversations/{B-id}` → 404,
   `/api/admin/users/{B}/conversations` → 404/405. Assert by
   route-listing that no admin route can return conversation/message text.
3. **Role gate** — user calling any `/api/admin/*` → 403.
4. **Unauthenticated** → 401 on every endpoint except `GET /api/health` and
   `POST /api/auth/login` (parameterized test over the route table).
5. **Retrieval ACL through the full chat path** — mock LLM, fake Pinecone
   store seeded with org + A-private + B-private chunks; A's chat retrieval
   filter matches org + A's; B's matches org + B's; A never receives B's
   chunk in answers (assert on captured filter *and* on results).
6. **Disabled user** — session revoked + `is_active=false` → next request 401
   immediately; login → 401.
7. **No content in logs or audit** — with question "SECRET-MARKER-123"
   flowing through chat: assert the marker appears in no captured log record,
   in no `audit_events` row (scan all columns incl. `details`), in no admin
   API response; it *does* appear exactly once, in `messages`.
8. **Login throttling & rate limits** — 5 failed logins from (username, IP)
   start the exponential delay (fake clock like `tests/test_ratelimit.py`);
   the *same username from a different IP* is not throttled, and the *same
   IP with a different username* is not throttled (no victim lock-out, no
   shared-IP freeze); correct password after cooldown resets; per-user chat
   limit returns 429 with `Retry-After` while another user is unaffected.
   `python -m app.cli unlock` clears the records.
9. **Password lifecycle** — temp-password user blocked with
   `password_change_required`; forced change clears flag and old sessions;
   admin reset revokes target sessions.
10. **Document scope** — A uploads private (when enabled): B can't see it in
    `GET /api/documents` (404 on id); same-filename uploads by A and B produce
    distinct `doc_id`s and deleting one never removes the other's vectors
    (fake store asserts delete filter is by `doc_id`).
11. **Cookie/CSRF hygiene** — cookie flags (httpOnly, SameSite, `Secure` per
    `COOKIE_SECURE`) on login response; mutating request without
    `X-CSRF-Token` → 403; logout revokes server-side (replayed cookie → 401).
12. **Last-admin guard** — with exactly one active admin: demote, disable,
    delete (by self or by the other admin-less case) → 409; with two admins,
    demoting one succeeds and the second demotion → 409.
13. **`login_failed` audit privacy** — failed login for a real user writes
    `actor_user_id` and no raw username; failed login for a nonexistent
    username writes no identifier; audit rows never contain the attempted
    password.
14. **Deletion consistency** — fake Pinecone store: document delete records
    `deleting`, issues the `doc_id` filter delete, then removes the row;
    a simulated Pinecone failure leaves the row in `deleting` and the retry
    completes it; account delete removes private docs' vectors *before* the
    user row disappears.
15. **Connection discipline** — with the graph mocked to raise: after the
    502 response, the SQLAlchemy pool reports zero checked-out connections;
    the user message row survives; an `error`-status assistant row exists
    with empty content.
16. **Schema sanity** — `uuidv7()` default produces v7 UUIDs; `inet` column
    rejects garbage; enum/CHECK constraints reject bad role/scope values;
    org-doc `owner_id IS NULL` CHECK holds.

**Live Pinecone check** — one file, `tests/test_live_pinecone.py`, skipped
unless `RUN_LIVE_PINECONE=1` **and** `PINECONE_API_KEY` set; runs against
`PINECONE_TEST_NAMESPACE` only: index a tiny synthetic doc as org and one as
private to two synthetic users, assert filter semantics of
`{"read_access": {"$in": [...]}}` behave as designed against the real
server, then `delete_all` in the test namespace. CI never sets the env var.

---

## A12. Phase B preview (amended order and scope)

One step = one commit, tests first, authorization tests per step, no
destructive actions. **STOP for your review after B2 and after B4** (unchanged).
Per your revision: after B6 the phase skips to the transitional UI; **admin
aggregate metrics, the retention sweep, data export, and the feedback
endpoint are deferred** until you ask for them (their design stays in
§A6/§A8/§A9 so nothing has to be re-decided later).

| Step | Commits (in order) | Key risks |
|---|---|---|
| **B1** | 1. `docker-compose.yml` (Postgres 18) + settings (`DATABASE_URL`, `TEST_DATABASE_URL`, `COOKIE_SECURE`, session lifetimes, `ENABLE_WEB_FALLBACK`, `MAX_UPLOAD_MB`, `PINECONE_TEST_NAMESPACE`) + `.env.example` · 2. SQLAlchemy engine/session factory + `psycopg[binary]` pin · 3. Alembic scaffold + initial (empty-baseline) migration + uuidv7 sanity test · 4. **CI: Postgres service container** + `docs/dev-testing.md` local workflow | Compose on your machine; Alembic wiring; CI service health flakiness |
| **B2** | 5. users+sessions tables/migrations (uuidv7 defaults, CHECKs) · 6. argon2id + `bootstrap-admin` CLI · 7. login/logout/me + CSRF · 8. (username, IP) exponential throttle + login rate limit · 9. recovery CLIs (`reset-password`, `unlock`) · 10. auth dependency (session lookup, disabled-user check) + last-admin guard helpers — **CHECKPOINT: your review** | Cookie flags in prod TLS termination (`COOKIE_SECURE` behind proxy); throttle UX |
| **B3** | 11. conversations+messages tables/migrations · 12. CRUD + ownership (404) tests · 13. chat integration with **TX1 → graph (no DB held) → TX2** discipline (§A8 lifecycle), filter via state, token capture, `status='error'` path · 14. per-user chat rate limit | Connection-discipline regressions (pinned by pool test); threading filter through graph state |
| **B4** | 15. documents table/migration (org ⇒ `owner_id NULL` CHECK) + upload path with `MAX_UPLOAD_MB` (413) · 16. new chunk IDs + delete-by-`doc_id` · 17. server-side retrieval filter (incl. **org-only for legacy chat**) + ACL tests · 18. legacy-vector migration: new namespace, re-ingest scripts (sample KB → org docs, your upload → private to you per **D15**) · 19. deletion consistency per §A9.1 — **CHECKPOINT: your review** | Pinecone filter behavior (mitigated by live test); re-ingest coordination with you; the org-only filter changing legacy answers if re-ingest lapses |
| **B5** | 20. audit_events table + writer · 21. legacy chat stops writing question/trace text; legacy ingest audits metadata-only · 22. `query_audit` drop script (runs only on your explicit confirmation — **D2**) · 23. log-content test + Tavily-error-log scrub | The one destructive step (guarded, explicit, verifiable row count) |
| **B6** | 24. admin users endpoints (create/disable/role/reset-password/delete + last-admin 409s) · 25. admin documents endpoints (list/upload/delete; no visibility promote for private docs — **D11**) · 26. forced-password-change gate · 27. account self-delete (`DELETE /api/me`) with §A9.1 consistency | Cascade correctness; vector cleanup ordering |
| **B8** | 28. minimal login page + logout in the current UI; sidebar key fields removed · 29. new-UI contract docs (`docs/api.md` from the §A8 tables) + deprecation notes in README/`.env.example` | Old UI breaking mid-rollout |
| **B9** | 30. **Removal commit**: legacy `/api/chat` + `/api/ingest`, static-key settings, `localStorage` key handling gone; stale `README.md:159` fixed · 31. docs sweep | Someone still on legacy keys — the removal commit is deliberately last and separate |

Deferred until you ask: admin metrics endpoint (with the D3 k ≥ 5 rule),
retention sweep + `CONVERSATION_RETENTION_DAYS` scheduling, `GET
/api/me/export`, `POST /api/feedback` (+ `message_feedback` table migration
lands with B3's messages migration to avoid a later table reorder, but no
endpoint).

Largest overall risks: (1) the B4 live-Pinecone behaviors — fenced behind the
test namespace and a checkpoint; (2) B5's destructive step — guarded by
explicit confirmation and a verified row count; (3) session cookie flags
behind TLS termination — verified in staging before go-live; (4) the B4
org-only filter making legacy answers empty if the re-ingest is skipped —
the checkpoint exists precisely so this can't happen silently.

### Impact on existing code (every file Phase B touches, and why)

| File | Change | Why / step |
|---|---|---|
| `app/core/config.py` | Add DB/session/limit settings (`DATABASE_URL`, `TEST_DATABASE_URL`, `COOKIE_SECURE`, `SESSION_IDLE_HOURS`, `SESSION_ABSOLUTE_DAYS`, `ENABLE_WEB_FALLBACK`, `MAX_UPLOAD_MB`, `PINECONE_TEST_NAMESPACE`); **remove** `CHAT_API_KEY`/`ADMIN_API_KEY` only in the removal commit | B1, B9 |
| `app/main.py` | Replace `init_db()` call with "run Alembic to head" (or documented `alembic upgrade` deploy step); mount `templates/login.html` route | B1, B2, B8 |
| `app/api/routes.py` | Legacy `/api/chat` gains org-only filter (B4), stops writing content to audit (B5); legacy `/api/ingest` routes into documents flow (B4); both deleted in removal commit. New routers live in new modules (`app/api/auth.py`, `conversations.py`, `documents.py`, `admin.py`) — `routes.py` stays the legacy surface | B3–B5, B9 |
| `app/services/audit.py` | Superseded by `app/services/audit_events.py` (new); legacy `write_audit` reduced to content-free writes from B5; module deleted in removal commit | B5, B9 |
| `app/services/ratelimit.py` | **Unchanged class**; re-keyed call sites (user_id for chat, IP for login) | B2, B3 |
| `app/services/ingestion.py` | `load_file`/`chunk_documents` unchanged; caller attaches `doc_id`/scope metadata after chunking | B4 |
| `app/rag/vectorstore.py` | `get_retriever(filter)` becomes filter-required; new `_chunk_id` (doc_id-scoped); delete-by-`doc_id`; namespace constant from settings | B4 |
| `app/rag/workflow.py` | **Unchanged graph structure** per your instruction — the only change is the retrieval filter passed into state and read by `retrieve_kb`; `ENABLE_WEB_FALLBACK=false` makes `search_web` short-circuit to the insufficient path | B3, B4 |
| `app/rag/state.py` | `AgentState` gains `retrieval_filter: dict` | B3 |
| `templates/index.html` | Sidebar key fields removed (B8); login page link; minimal `templates/login.html` added | B8 |
| `static/js/app.js` | `localStorage` key handling removed (B8); fetch moves to cookie+CSRF calls as the UI evolves; legacy chat fetch keeps working until removal commit | B8, B9 |
| `ingest_sample_kb.py` | Re-run as org documents under the new metadata scheme (or superseded by `python -m app.cli ingest-org <dir>`) | B4 |
| `README.md`, `.env.example` | New settings documented; `LANGSMITH_TRACING=false` explicit with the third-party warning (**D13**); stale `README.md:159` dimension-claim fixed; legacy-key deprecation then removal | B1, B8, B9 |
| `.github/workflows/ci.yml` | Postgres 18 service container + `TEST_DATABASE_URL` (moved here from the old B9) | B1 |
| `tests/conftest.py` | Postgres test-session fixtures (schema-per-session, rollback-per-test); offline key forcing retained | B1–B3 |
| `data/audit.db` | Dropped in B5 on your explicit confirmation (**D2**) | B5 |
| `uploads/` | Restructured to `uploads/<doc_id>/` (B4); existing file re-ingested per **D15** | B4 |
| `requirements.txt` | Add `alembic`, `psycopg[binary]`, `argon2-cffi` (justifications §A3) | B1, B2 |
| `Dockerfile` | No structural change; gains nothing until deploy docs — secrets stay env-injected | — |

Explicitly **unchanged**: the LangGraph graph topology and all node logic
(except the filter read in `retrieve_kb`), the prompt texts, the markdown
UI renderer, the Tavily error-dict handling (M1), the CI pip-audit job.

---

## A13. Decisions — resolved 2026-10-22, plus D19 (open)

| # | Decision | Outcome |
|---|---|---|
| **D1** | Adopt PostgreSQL (compose for dev **and tests**) | **Accepted.** |
| **D2** | Fate of existing `query_audit` rows | **Amended by Farhad:** no CSV export — the 8 rows are his own test questions. Drop table + delete `data/audit.db` in B5, script gated on his explicit confirmation at run time. |
| **D3** | Per-user question counts for admins | **Accepted + tightened:** never exposed; every aggregate bucket also suppressed below **5 users** (k-anonymity floor). Metrics endpoint deferred. |
| **D4** | Who may upload | **Accepted:** admin-only org uploads in Phase B; user private uploads later opt-in. |
| **D5** | Conversation retention default | **Accepted:** none by default; Farhad chooses the real period before go-live (sweep deferred until then). |
| **D6** | Web fallback / Tavily exposure | **Accepted** with `ENABLE_WEB_FALLBACK` kill switch, default on (locally on; operator may disable in prod). |
| **D7** | Session lifetimes | **Accepted:** idle 7 days / absolute 30 days, configurable (`SESSION_IDLE_HOURS`, `SESSION_ABSOLUTE_DAYS`). |
| **D8** | Legacy mode | **Amended by Farhad:** **no `AUTH_MODE` flag.** Legacy endpoints stay until one removal commit (end of Phase B); legacy chat uses the org-only filter from B4 and writes no question/trace text from B5. See §A10. |
| **D9** | Disabled-user data | **Accepted:** retain until explicit delete. |
| **D10** | Admin sees private-doc metadata | **Accepted** (name/size/status only, never content). |
| **D11** | Promoting private docs to org | **Accepted:** forbidden — private docs deletable, not promotable. |
| **D12** | Pinecone-key holders can read chunk text | **Accepted** + documented (§A7). |
| **D13** | LangSmith | **Accepted + tightened:** `.env.example` sets `LANGSMITH_TRACING=false` **explicitly**, labeled as sending user content to a third party. |
| **D14** | Multi-worker deployment | **Accepted:** out of scope; limiters documented single-process. |
| **D15** | Existing upload (`Order Automation Backend — Your Build Roadmap.md`) | **Accepted:** re-ingested as **private to Farhad** in the new namespace. |
| **D16** | Org document ownership | **Amended by Farhad:** org documents **always** have `owner_id = NULL` (enforced by CHECK) — nothing is ever attributed to or reassigned from an admin account. |
| **D17** | Export scope | **Accepted:** conversations + messages + document metadata, no document bytes. (Endpoint deferred.) |
| **D18** | What was "M6"? | **Resolved:** the original audit finding *"shared namespace, no access control on documents"* — covered by this design (§A7: per-vector ACL metadata + server-side filter). |
| **D19** | **OPEN —** should follow-up questions use earlier conversation messages as context? | Analysis below. **Do not implement** until decided. |

### D19 analysis — conversation context for follow-ups (design only)

**What it means.** A user asks "How do I connect to the VPN?", then "What
about on Linux?". Today the second question is routed/graded/answered in
isolation and fails. Context mode would prepend the last K messages of the
conversation to the router/rewrite/answer prompts.

**Effect on the graph.** `AgentState` gains `history: List[...]` (role +
content pairs), loaded in **TX1** (§A8 lifecycle — this fits: history is
loaded in the same short transaction that saves the user message, so the
graph run itself stays DB-free). Node changes, all prompt-template level:
`route_question`, `rewrite_query`, and the three `generate_*` nodes receive a
formatted history block; `retrieve_kb` retrieves the *rewritten standalone
question* (context must be folded into the search string, not into the
embedding input directly). No new nodes, no edge changes — the topology
survives untouched, same as the filter change.

**Effect on cost.** Prompt tokens grow linearly with K turns on up to 4 LLM
calls per request (router, grader, generator; rewriter on retry). At K=6
messages × ~200 tokens, that's ~1.2K extra prompt tokens per call — roughly
2–3× current prompt cost per question. Mitigations: fold history only into
router + rewrite + final generation (skip graders), or cap at K=4. Also a
latency cost on the router call.

**Effect on privacy.** None structurally: history already lives in
owner-only `messages`; it never reaches audit/logs. One nuance — more of the
user's own past wording goes to OpenAI per request (§A2 vendor exposure
grows slightly in volume, not in audience).

**Effect on tests.** New cases: history loaded only from the request's own
conversation (an authz test — user B can never trigger A's history);
history cap enforced; router prompt contains prior turn (mocked LLM asserts
call payload); follow-up routing regression ("what about on Linux?" → kb
route with history, direct without); TX1 loading keeps the no-DB-during-graph
guarantee (pool test extended).

**Recommendation:** implement later as a per-request default-on with K=4 and
graders excluded, behind `CHAT_HISTORY_TURNS` (0 = off, default off for
launch). What would change it: evidence that follow-ups are a top use case
from real usage (the audit `chat_asked` aggregates can't tell us this
content-lessly — ask users instead), or a cost ceiling that rules the token
growth out.

**Rule-conflict note (working rule 4):** unchanged from the original review —
D11 was the one rules-2-vs-3 tension and is resolved (no promotion of private
docs). Rule 3's honest boundary bears repeating: it binds the *application*;
it cannot bind whoever has database or vendor access (§A2).

---

*Sources for Pinecone filter semantics: [Filter by metadata — Pinecone docs](https://docs.pinecone.io/guides/search/filter-by-metadata), plus direct verification of the installed packages (`pinecone==7.3.0` `db_data/types/query_filter.py`, `langchain-pinecone==0.2.13` `vectorstores.py`).*
