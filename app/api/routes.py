import logging
import secrets
import time
from pathlib import Path
from fastapi import APIRouter, Depends, Request, UploadFile, File, HTTPException, Header
from pydantic import BaseModel, Field
from app.core.config import get_settings
from app.core.db import get_db
from app.rag.workflow import ask
from app.rag.vectorstore import add_documents
from app.services import documents as documents_service
from app.services.ingestion import load_file, chunk_documents, SUPPORTED
from app.services.audit import write_audit
from app.services.audit_events import record_event
from app.services.ratelimit import SlidingWindowLimiter
from app.services.retrieval import ORG_ONLY_FILTER
from app.services.validation import display_name_problems

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
        # D8/§A7: a static key proves no identity — legacy retrieval is
        # org-only from B4 on (private chunks never match this filter).
        result = ask(payload.question, retrieval_filter=ORG_ONLY_FILTER)
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
        # F4 (review finding 5): static line + exception CLASS only.
        # logger.exception embedded the full traceback, and upstream
        # exceptions can echo the QUESTION verbatim into shared logs.
        logger.error("chat request failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Internal error") from exc



@router.post("/ingest")
async def ingest(
    file: UploadFile = File(...),
    x_admin_key: str = Header(default=""),
    db=Depends(get_db),
):
    """Legacy admin ingest (D8): unchanged contract, but from B4 it routes
    through the documents flow — an org document with owner_id = NULL,
    streamed under MAX_UPLOAD_MB, ACL metadata attached (§A7/§A10)."""
    # encode() so odd header bytes can't raise TypeError inside compare_digest
    if not secrets.compare_digest(
        x_admin_key.encode("utf-8"), settings.admin_api_key.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid admin key")
    display_name = Path(file.filename or "").name
    # F3 (review finding 3): the client-controlled filename is validated —
    # NUL/control characters (a raw NUL used to crash the DB driver as an
    # unhandled 500), Windows reserved device names, reserved filesystem
    # characters, trailing dot/space, over-length — before any row or file.
    name_problems = display_name_problems(display_name)
    if name_problems:
        raise HTTPException(status_code=422, detail="; ".join(name_problems))
    suffix = Path(display_name).suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(status_code=400, detail=f"Supported: {', '.join(sorted(SUPPORTED))}")

    # reject early on Content-Length when the client told the truth (§A7)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(status_code=413, detail="File exceeds MAX_UPLOAD_MB")

    doc = documents_service.create_document(
        db, scope="org", owner_id=None, display_name=display_name,
        storage_path="", checksum="", size_bytes=0, mime_suffix=suffix or None,
    )
    try:
        dest, size, checksum = documents_service.save_upload_stream(
            file.file, dest_dir=Path(settings.upload_dir) / str(doc.id),
            storage_name=f"{doc.id}{suffix}",  # F1: server-generated stored name
        )
    except documents_service.UploadTooLarge:
        db.rollback()  # the pending row dies with the request transaction
        raise HTTPException(status_code=413, detail="File exceeds MAX_UPLOAD_MB") from None
    except documents_service.UploadRejected:
        db.rollback()  # the pending row dies with the request transaction
        raise HTTPException(status_code=422, detail="Invalid storage name") from None
    doc.storage_path = str(dest)
    doc.size_bytes = size
    doc.checksum = checksum
    db.flush()
    try:
        loaded = load_file(dest)
        chunks = chunk_documents(loaded)
        ids = add_documents(
            chunks, doc_id=doc.id, scope="org", owner_id=None, source_name=display_name
        )
    except Exception as exc:
        documents_service.mark_failed(db, doc, "ingest_failed")
        record_event(
            db, "document_uploaded", object_type="document", object_id=doc.id,
            outcome="error", route="/api/ingest",
            details={"scope": "org", "via": "legacy_ingest", "error_class": "ingest_failed"},
        )
        db.commit()
        # F4: same rule as the chat path — parser errors can embed file
        # content (e.g. XML fragments from a malformed docx); log the class
        # only, never the exception body.
        logger.error("legacy ingest failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Ingest failed") from None

    documents_service.attach_ingest_result(db, doc, len(chunks))
    record_event(
        db, "document_uploaded", object_type="document", object_id=doc.id,
        outcome="ok", route="/api/ingest",
        details={"scope": "org", "via": "legacy_ingest", "chunks": len(chunks)},
    )
    db.commit()
    return {
        "message": "Document indexed",
        "file": display_name,
        "chunks": len(chunks),
        "ids_created": len(ids),
        "document_id": str(doc.id),
    }