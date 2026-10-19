# Enterprise Agentic RAG

An IT support copilot that answers from a private company knowledge base first, and only touches the public web when the internal evidence isn't good enough.

Instead of the usual retrieve-and-hope RAG loop, every step here is deliberate: an LLM routes the question, grades whatever the retriever returns, and falls back to a Tavily web search only if the internal docs fall short. If nothing pans out, it says so and points you to the help desk instead of making something up.

Built with FastAPI, LangGraph, Pinecone, and OpenAI, with a small vanilla JS chat UI that shows the agent's full decision trace.

## What makes it "agentic"

A typical RAG pipeline retrieves chunks and immediately stuffs them into the prompt. This one doesn't trust retrieval that much:

- A **router** decides whether the question needs company knowledge at all ("what's our password policy?" → KB, "thanks!" → plain chat).
- Retrieved chunks are **graded** by an LLM judge before they're allowed near the answer.
- Weak KB evidence triggers a **web fallback** (Tavily) — and those results get graded too.
- If both sources fail, the query gets **rewritten** and the loop retries once. Still nothing? It gives up honestly.

Every answer reports where it came from (`private_kb`, `web_search`, `direct`, or `insufficient_evidence`), and answers built from web results are flagged as external info that may need IT validation.

## The graph

```
START
  │
  ▼
route_question ──(casual chat)──▶ direct_answer ──▶ END
  │ (IT question)
  ▼
retrieve_kb ──▶ grade_kb ──(good)──▶ generate_from_kb ──▶ END
                  │ (weak)
                  ▼
               search_web ──▶ grade_web ──(good)──▶ generate_from_web ──▶ END
                                 │ (weak)
                     ┌───────────┴───────────┐
                     │ retries left          │ retries exhausted
                     ▼                       ▼
               rewrite_query              insufficient ──▶ END
                     │
                     └──▶ (back to retrieve_kb)
```

The whole thing is defined in `app/rag/workflow.py` — nodes are plain functions, routing is conditional edges, and every node appends a human-readable line to a trace that shows up in the UI.

## Setup

You'll need Python 3.13 and API keys for OpenAI, Pinecone, and Tavily.

```bash
git clone <your-repo-url>
cd Enterprise-Agentic-RAG

python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux / macOS

pip install -r requirements.txt
```

Create a `.env` in the project root:

```env
OPENAI_API_KEY=sk-...
PINECONE_API_KEY=...
TAVILY_API_KEY=...
ADMIN_API_KEY=something-secret   # protects the upload endpoint — don't ship "change-me"
```

Load the sample knowledge base:

```bash
python ingest_sample_kb.py
```

This reads everything in `data/sample_kb/` (a couple of fictional "NovaRetail" IT docs), chunks it, and pushes it to Pinecone. The index is created automatically if it doesn't exist yet.

Then run it:

```bash
python run.py
```

Open http://127.0.0.1:8080 and ask something the sample docs cover (VPN setup, password reset) or something that needs current web info — the trace panel on the right shows which path the agent took and why.

## Configuration

All settings live in `app/core/config.py` and can be overridden via `.env`:

| Variable | Required | Default | What it does |
|---|---|---|---|
| `OPENAI_API_KEY` | yes | — | Powers the LLM (routing, grading, answering) and embeddings |
| `PINECONE_API_KEY` | yes | — | Vector store for the private KB |
| `TAVILY_API_KEY` | for web fallback | — | Web search when KB evidence is weak |
| `ADMIN_API_KEY` | recommended | `change-me` | Required by `POST /api/ingest` |
| `OPENAI_MODEL` | no | `gpt-4o-mini` | Chat model |
| `EMBEDDING_MODEL` | no | `text-embedding-3-small` | Dimension map lives in `app/rag/vectorstore.py` |
| `PINECONE_INDEX_NAME` | no | `enterprise-agentic-rag` | Index is auto-created |
| `PINECONE_NAMESPACE` | no | `enterprise-agentic-rag` | |
| `TOP_K` | no | `4` | Chunks fetched per retrieval |
| `MAX_RETRIES` | no | `1` | Query-rewrite retries before giving up |
| `INDEX_READY_TIMEOUT_SECONDS` | no | `120` | How long to wait for a fresh Pinecone index before failing |
| `LLM_TIMEOUT_SECONDS` | no | `60` | Per-call timeout for OpenAI requests |
| `WEB_SEARCH_TIMEOUT_SECONDS` | no | `30` | Deadline for a Tavily search |
| `CHAT_API_KEY` | no | *(unset)* | When set, `/api/chat` requires a matching `X-API-Key` header. The web UI has a key field for this |
| `CHAT_RATE_LIMIT_PER_MINUTE` | no | `20` | Chat requests per minute per IP (0 disables) |
| `LANGSMITH_TRACING` | no | — | Set `true` to trace every graph run in LangSmith (with `LANGSMITH_API_KEY`, optionally `LANGSMITH_PROJECT`). No code needed — the `langsmith` package is already installed |

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Health check |
| `POST` | `/api/chat` | Ask a question, get an answer + trace + citations |
| `POST` | `/api/ingest` | Upload a document (PDF, TXT, MD, DOCX) into the index |

Chat is rate-limited (20 requests/min/IP, `CHAT_RATE_LIMIT_PER_MINUTE`) and returns `429` with a `Retry-After` header when exceeded. Auth is opt-in: set `CHAT_API_KEY` and clients must send a matching `X-API-Key` header — the bundled web UI has a key field in the sidebar for exactly this.

Chat:

```bash
curl -X POST http://127.0.0.1:8080/api/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "How do I connect to the company VPN?"}'
```

Ingest a document (admin key required):

```bash
curl -X POST http://127.0.0.1:8080/api/ingest \
  -H "X-Admin-Key: your-key" \
  -F "file=@your-policy.pdf"
```

The chat response includes the `answer`, which `source_used` produced it, the full step-by-step `trace`, `citations`, and the `rewritten_query` if the agent rewrote yours along the way.

## Project structure

```
├── app/
│   ├── api/routes.py          # /api/chat, /api/ingest, /api/health
│   ├── core/config.py         # pydantic-settings, reads .env
│   ├── core/logging.py
│   ├── rag/
│   │   ├── state.py           # shared graph state + structured output schemas
│   │   ├── workflow.py        # the LangGraph graph — nodes and routing
│   │   └── vectorstore.py     # Pinecone index, embeddings, retriever
│   └── services/
│       ├── audit.py           # SQLite audit log (question, source, trace)
│       └── ingestion.py       # load + chunk PDF / TXT / MD / DOCX
├── data/
│   ├── sample_kb/             # fictional sample docs (NovaRetail IT handbook)
│   └── audit.db               # created on first run
├── templates/  static/        # chat UI (Jinja2 + vanilla JS)
├── ingest_sample_kb.py        # indexes data/sample_kb into Pinecone
└── run.py                     # uvicorn entry point (port 8080)
```

## Things worth knowing

- **Changing `EMBEDDING_MODEL`** to a model with a different dimension deletes and recreates the Pinecone index — your vectors go with it. Re-run ingestion afterward.
- **Every question is audited.** `data/audit.db` (plain SQLite) stores the question, which source answered, and the full execution trace. Good for debugging and for showing your work.
- **The sample docs are fictional.** Swap in real company documents via the "+ Add Company Document" button or `POST /api/ingest`.
- Chunking is `RecursiveCharacterTextSplitter` at 900 chars with 120 overlap — tweak it in `app/services/ingestion.py` if your docs need different treatment.

## License

[MIT](LICENSE)
