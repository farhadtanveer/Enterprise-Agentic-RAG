from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class MessageFeedback(Base):
    """One vote per user per message. The endpoint is deferred (§A12) — the
    table lands with B3's messages migration to avoid a later reorder."""

    __tablename__ = "message_feedback"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_message_feedback_once"),
        CheckConstraint("feedback_value IN ('up', 'down')", name="ck_message_feedback_value"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, server_default=func.uuidv7())
    message_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("messages.id", ondelete="CASCADE")
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE")
    )
    feedback_value: Mapped[str] = mapped_column(String(8))
    # USER CONTENT — never shown to admins (§A8 must-not-exist list)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
