import logging
import secrets
import time
from pathlib import Path
from fastapi import APIRouter, Request, UploadFile, File, HTTPException, Header
from pydantic import BaseModel, Field
from app.core.config import get_settings
from app.rag.workflow import ask
from app.rag.vectorstore import add_documents
from app.services.ingestion import load_file, chunk_documents, SUPPORTED
from app.services.audit import write_audit
from app.services.ratelimit import SlidingWindowLimiter

router = APIRouter(prefix="/api")
settings = get_settings()
logger = logging.getLogger(__name__)
chat_limiter = SlidingWindowLimiter(settings.chat_rate_limit_per_minute)

class ChatRequest(BaseModel):
    question: str = Field(min_length=2, max_length=3000)


@router.get("/health")
def health():
    return {"status": "ok", "service": settings.app_name}


@router.post("/chat")
def chat(payload: ChatRequest, request: Request, x_api_key: str = Header(default="")):
    if settings.chat_api_key and not secrets.compare_digest(
        x_api_key.encode("utf-8"), settings.chat_api_key.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    if settings.chat_rate_limit_per_minute > 0 and not chat_limiter.allow(
        request.client.host if request.client else "unknown"
    ):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Try again shortly.",
            headers={"Retry-After": "60"},
        )
    try:
        started = time.perf_counter()
        result = ask(payload.question)
        duration_ms = int((time.perf_counter() - started) * 1000)
        write_audit(
            payload.question,
            result["source_used"],
            result.get("trace", []),
            client_ip=request.client.host if request.client else "unknown",
            authenticated=bool(settings.chat_api_key),
            duration_ms=duration_ms,
        )

        return {
            "answer": result["answer"],
            "source_used": result["source_used"],
            "trace": result.get("trace", []),
            "citations": result.get("citations", []),
            "rewritten_query": result.get("current_query", payload.question),

        }

    except Exception as exc:
        logger.exception("chat request failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc



@router.post("/ingest")
async def ingest(file: UploadFile = File(...), x_admin_key: str = Header(default="")):
    # encode() so odd header bytes can't raise TypeError inside compare_digest
    if not secrets.compare_digest(
        x_admin_key.encode("utf-8"), settings.admin_api_key.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid admin key")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(status_code=400, detail=f"Supported: {', '.join(sorted(SUPPORTED))}")
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / Path(file.filename).name
    dest.write_bytes(await file.read())
    docs = load_file(dest)
    chunks = chunk_documents(docs)
    ids = add_documents(chunks)
    return {"message": "Document indexed", "file": dest.name, "chunks": len(chunks), "ids_created": len(ids)}