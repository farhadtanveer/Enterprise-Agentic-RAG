# Enterprise Agentic RAG: project rules

## What this is

Internal knowledge assistant for the whole company. Documents have an
audience: org (everyone), group (department/team), private (one user).
FastAPI + LangGraph, layers api/ core/ rag/ services/. Branch
backend/multi-user. Design: docs/backend-design.md (section A14 wins).
Task files: docs/tasks/.

## Architecture (keep)

- Additive changes only. Keep the LangGraph topology and node logic unless
  strictly required. Auth/permission logic lives in api/ and services/,
  never in graph nodes.
- Chat history lives in our own Postgres tables, not the LangGraph
  checkpointer. Follow-up context (D19) is not implemented; do not add it.
- Postgres only. Schema changes through Alembic. No new dependency without
  a one-line justification and a pinned version.
- Legacy /api/chat and /api/ingest stay until the B9 removal commit. No
  AUTH_MODE flag.

## Privacy (non-negotiable)

- Only the owner can read a user's conversations/messages, admins and group
  editors included. No endpoint, export, dashboard or log may expose them.
  No impersonation, no break-glass.
- Admins never see content or filenames of private documents.
- Anything outside a user's audience returns 404, never 403.
- Question/answer text never appears in logs, audit_events, exceptions or
  traces. Engine uses hide_parameters=True. LangSmith/LangChain tracing off.
- Admin aggregates are suppressed below 5 users. No HR/payroll documents.

## Access control

- read_access tokens: "org", "group:<id>", "user:<id>".
- The retrieval filter is built server-side by the AccessResolver from the
  session, recomputed from the DB on every request. No filter or no
  metadata means fail closed.
- Org and group documents are ownerless (owner_id NULL). Deleting a user
  never deletes them. Admins have no implicit group access.
- Sharing a private doc into a group is a COPY approved by that group's
  editor. Admins can never force-share.
- Last-admin guard (409) on every path including self and /api/me.
- ENABLE_WEB_FALLBACK defaults to false.

## Safety of the working tree (a past test run deleted the repo)

- NEVER run recursive or directory deletes (rm -rf, shutil.rmtree, git clean,
  git reset --hard, docker compose down -v, volume rm) unless the target is
  a path you created this session under a temp directory. Ask first.
- Never touch .env, uploads/, data/ or user data unless asked. Report
  data/audit.db, never delete it.
- Code deletes only a single regular FILE whose realpath is inside
  upload_dir, never follows symlinks, never deletes directories derived from
  DB values or settings. Storage names are server-generated.
- Tests that delete anything use tmp_path only, never settings defaults.

## Test and data safety

- Tests and verify scripts write to Pinecone ONLY in the namespace
  multi-user-test and clean up their own vectors by id in a trap. Never
  write to rag-v2 or enterprise-agentic-rag from a test or script. Never
  delete_index. Verify per document by explicit vector ids, not stats.
- Real LLM/embedding calls are budgeted per task; prefer fakes.
- Never print, log or commit secrets. Redact cookies and tokens.

## Evidence and workflow

- Security-relevant changes need executed evidence (command, expected,
  actual, PASS/FAIL) in docs/evidence/. Never weaken or delete a test or
  check to make it pass. Report what failed first. A contract change to an
  existing test must be explicit in the commit message.
- One step = one commit; push after every green step; keep CI green;
  never leave large work uncommitted.
- When resuming, read docs/tasks/ and docs/PROGRESS.md first.

## Environment (Windows, Docker Desktop, MSYS bash)

- Export MSYS_NO_PATHCONV=1 before docker commands with container paths.
- The db service has no host port. App: 127.0.0.1:8081.
- Tests: docker compose --profile test run --rm test
- CLIs: docker compose run --rm app python -m app.cli <command>
- Live checks: bash scripts/verify/<name>.sh

## Progress log

- After every finished step, append to docs/PROGRESS.md: date, step, commit
  hash, tests/evidence that prove it, and the exact next step. Commit it with
  the step. A new session must be able to resume from this file and git log.
