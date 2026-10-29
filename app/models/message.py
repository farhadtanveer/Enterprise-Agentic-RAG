from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Message(Base):
    """The ONLY place question/answer content lives (§A4/§A6) — owner-scoped
    through the conversation join, never denormalized onto the row."""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("msg_role IN ('user', 'assistant')", name="ck_messages_role"),
        CheckConstraint("msg_status IN ('ok', 'error')", name="ck_messages_status"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, server_default=func.uuidv7())
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    msg_role: Mapped[str] = mapped_column(String(16))
    # 'error' rows carry empty content and no upstream text (§A8 lifecycle)
    msg_status: Mapped[str] = mapped_column(String(16), default="ok")
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    # owner-readable trace incl. rewritten query — their own data (§A4);
    # NEVER copied into audit_events
    trace: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    source_used: Mapped[str | None] = mapped_column(Text)
    rewritten_query: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Message {self.msg_role}/{self.msg_status} conv={self.conversation_id}>"
