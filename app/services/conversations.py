"""Conversation + message persistence (docs/backend-design.md §A4/§A8).

Ownership rule: EVERY access resolves the conversation through
``user_id == owner`` — another user's id is indistinguishable from a
nonexistent one (404, never 403; §A8 conventions).

No commits here: callers own the transaction boundaries (TX1/TX2, §A8).
"""
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DBSession

from app.models.conversation import Conversation
from app.models.message import Message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_conversation(db: DBSession, user_id, title: str | None = None) -> Conversation:
    conv = Conversation(user_id=user_id, title=title or None)
    db.add(conv)
    db.flush()
    return conv


def get_own_conversation(db: DBSession, conversation_id: UUID, user_id) -> Conversation | None:
    return db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    ).scalar_one_or_none()


def get_own_conversation_locked(db: DBSession, conversation_id: UUID, user_id) -> Conversation | None:
    """Same lookup with a row lock: serializes concurrent senders so the
    pending-message check can't race (review requirement d)."""
    return db.execute(
        select(Conversation)
        .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        .with_for_update()
    ).scalar_one_or_none()


def count_conversations(db: DBSession, user_id) -> int:
    return db.execute(
        select(func.count()).select_from(Conversation).where(Conversation.user_id == user_id)
    ).scalar_one()


def list_conversations(
    db: DBSession, user_id, *, limit: int = 20, offset: int = 0
) -> tuple[list[Conversation], int]:
    """Own conversations, newest activity first; (items, total)."""
    total = count_conversations(db, user_id)
    items = db.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(
            Conversation.last_message_at.desc().nullslast(), Conversation.created_at.desc()
        )
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    return list(items), total


def rename_conversation(db: DBSession, conv: Conversation, title: str) -> None:
    conv.title = title
    db.flush()


def delete_conversation(db: DBSession, conv: Conversation) -> None:
    # messages + feedback rows follow via ON DELETE CASCADE
    db.execute(delete(Conversation).where(Conversation.id == conv.id))
    db.flush()


def count_messages(db: DBSession, conv: Conversation) -> int:
    return db.execute(
        select(func.count()).select_from(Message).where(Message.conversation_id == conv.id)
    ).scalar_one()


def has_pending_message(db: DBSession, conv: Conversation) -> bool:
    """True while the conversation's LAST message is an unanswered user
    message — a graph run is conceptually in flight (review requirement d)."""
    last = db.execute(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return last is not None and last.msg_role == "user"


def add_message(
    db: DBSession,
    conv: Conversation,
    msg_role: str,
    content: str,
    *,
    msg_status: str = "ok",
    **fields,
) -> Message:
    msg = Message(conversation_id=conv.id, msg_role=msg_role, msg_status=msg_status, content=content, **fields)
    db.add(msg)
    conv.last_message_at = utcnow()
    db.flush()
    return msg


def list_messages(
    db: DBSession, conv: Conversation, *, limit: int = 50, offset: int = 0
) -> tuple[list[Message], int]:
    """Own messages, chronological within the page; (items, total)."""
    total = count_messages(db, conv)
    items = db.execute(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    return list(items), total


def conversation_payload(conv: Conversation) -> dict:
    return {
        "id": str(conv.id),
        "title": conv.title,
        "created_at": conv.created_at,
        "last_message_at": conv.last_message_at,
    }


def message_payload(msg: Message) -> dict:
    return {
        "id": str(msg.id),
        "conversation_id": str(msg.conversation_id),
        "role": msg.msg_role,
        "status": msg.msg_status,
        "content": msg.content,
        "citations": msg.citations,
        "trace": msg.trace,
        "source_used": msg.source_used,
        "rewritten_query": msg.rewritten_query,
        "model": msg.model,
        "prompt_tokens": msg.prompt_tokens,
        "completion_tokens": msg.completion_tokens,
        "duration_ms": msg.duration_ms,
        "created_at": msg.created_at,
    }
