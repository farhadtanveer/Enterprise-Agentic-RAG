"""Conversation & chat endpoints (docs/backend-design.md §A8) — B3.

Chat lifecycle (§A8 connection discipline): TX1 (auth + ownership lock +
caps + pending check + user message) → graph run holding NO DB resource →
TX2 (assistant message, or an error-status marker if the graph failed).
The graph NEVER sees a session; the pool test proves checkedout()==0.
"""
import logging
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.models.user import User
from app.rag.workflow import ask
from app.services import conversations as conv_service
from app.services.audit_events import record_event
from app.services.ratelimit import SlidingWindowLimiter

router = APIRouter(prefix="/api", tags=["conversations"])
settings = get_settings()
logger = logging.getLogger(__name__)
# Per-user chat rate limit (§A3 re-keyed from IP; review requirement f)
chat_limiter = SlidingWindowLimiter(settings.chat_rate_limit_per_minute)

SEND_ROUTE = "/api/conversations/{id}/messages"


class CreateConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class RenameConversationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class SendMessageRequest(BaseModel):
    question: str = Field(min_length=2, max_length=settings.max_message_length)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _get_own_or_404(db: DBSession, conversation_id: UUID, user: User, *, locked=False):
    if locked:
        conv = conv_service.get_own_conversation_locked(db, conversation_id, user.id)
    else:
        conv = conv_service.get_own_conversation(db, conversation_id, user.id)
    if conv is None:
        # another user's id and a nonexistent id are the same 404 (§A8)
        raise HTTPException(status_code=404, detail="Not found")
    return conv


@router.get("/conversations")
def list_conversations(
    response: Response,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    items, total = conv_service.list_conversations(db, user.id, limit=limit, offset=offset)
    _no_store(response)
    return {
        "items": [conv_service.conversation_payload(c) for c in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/conversations")
def create_conversation(
    payload: CreateConversationRequest,
    response: Response,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    if conv_service.count_conversations(db, user.id) >= settings.max_conversations_per_user:
        raise HTTPException(
            status_code=409,
            detail={"code": "conversation_limit_reached"},
            headers={"Retry-After": "86400"},
        )
    conv = conv_service.create_conversation(db, user.id, payload.title)
    record_event(
        db, "conversation_created", actor_user_id=user.id,
        object_type="conversation", object_id=conv.id, outcome="ok",
        route="/api/conversations",
    )
    db.commit()
    _no_store(response)
    return conv_service.conversation_payload(conv)


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: UUID,
    response: Response,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    conv = _get_own_or_404(db, conversation_id, user)
    _no_store(response)
    return conv_service.conversation_payload(conv)


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: UUID,
    payload: RenameConversationRequest,
    response: Response,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    conv = _get_own_or_404(db, conversation_id, user)
    conv_service.rename_conversation(db, conv, payload.title)
    record_event(
        db, "conversation_renamed", actor_user_id=user.id,
        object_type="conversation", object_id=conv.id, outcome="ok",
        route="/api/conversations/{id}",
    )
    db.commit()
    _no_store(response)
    return conv_service.conversation_payload(conv)


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: UUID,
    response: Response,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    conv = _get_own_or_404(db, conversation_id, user)
    conv_service.delete_conversation(db, conv)
    record_event(
        db, "conversation_deleted", actor_user_id=user.id,
        object_type="conversation", object_id=conversation_id, outcome="ok",
        route="/api/conversations/{id}",
    )
    db.commit()
    _no_store(response)
    return {"status": "deleted"}


@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: UUID,
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    conv = _get_own_or_404(db, conversation_id, user)
    items, total = conv_service.list_messages(db, conv, limit=limit, offset=offset)
    _no_store(response)
    return {
        "items": [conv_service.message_payload(m) for m in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/conversations/{conversation_id}/messages")
def send_message(
    conversation_id: UUID,
    payload: SendMessageRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    if settings.chat_rate_limit_per_minute > 0 and not chat_limiter.allow(str(user.id)):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Try again shortly.",
            headers={"Retry-After": "60"},
        )

    # --- TX1: ownership LOCK + caps + pending check + user message ---------
    conv = _get_own_or_404(db, conversation_id, user, locked=True)
    if conv_service.has_pending_message(db, conv):
        # a previous question is unanswered (graph in flight or crashed
        # mid-write); serialize rather than interleave (review requirement d)
        raise HTTPException(status_code=409, detail={"code": "message_pending"})
    if conv_service.count_messages(db, conv) >= settings.max_messages_per_conversation:
        raise HTTPException(status_code=409, detail={"code": "message_limit_reached"})
    conv_service.add_message(db, conv, "user", payload.question)
    db.commit()  # TX1 done — connection returns to the pool
    _no_store(response)

    # --- graph run: zero DB resources held ---------------------------------
    started = time.perf_counter()
    try:
        result = ask(payload.question)
    except Exception:
        # static log line only — no exception text can echo the question (g)
        logger.error("chat graph failed for a conversation message")
        duration_ms = int((time.perf_counter() - started) * 1000)
        # --- TX2 (failure): the user message STAYS; assistant row status=error
        conv_service.add_message(
            db, conv, "assistant", "", msg_status="error",
            model=settings.openai_model, duration_ms=duration_ms,
        )
        record_event(
            db, "chat_failed", actor_user_id=user.id,
            object_type="conversation", object_id=conv.id,
            outcome="error", route=SEND_ROUTE, duration_ms=duration_ms,
        )
        db.commit()
        raise HTTPException(
            status_code=502, detail="The assistant failed to answer. You can retry."
        ) from None

    duration_ms = int((time.perf_counter() - started) * 1000)
    usage = result.get("token_usage") or {}
    # --- TX2 (success): answer + citations + trace + timings + tokens ------
    assistant = conv_service.add_message(
        db, conv, "assistant", result["answer"],
        citations=result.get("citations", []),
        trace=result.get("trace", []),
        source_used=result.get("source_used"),
        rewritten_query=result.get("current_query"),
        model=settings.openai_model,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        duration_ms=duration_ms,
    )
    # audit: counts and status ONLY — no question/answer/trace/title (§A6)
    record_event(
        db, "chat_asked", actor_user_id=user.id,
        object_type="conversation", object_id=conv.id,
        outcome="ok", route=SEND_ROUTE, duration_ms=duration_ms,
        tokens_total=usage.get("total_tokens"),
        details={"source_used": result.get("source_used"), "status": "ok"},
    )
    db.commit()
    return conv_service.message_payload(assistant)
