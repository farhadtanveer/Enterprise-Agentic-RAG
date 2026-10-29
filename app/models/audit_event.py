from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AuditEvent(Base):
    """Who/when/what-kind only — NEVER question or answer content (§A6).

    ``details`` JSONB holds ids and counters only; a Phase B test pins the
    no-content rule by scanning every column. ``object_id`` has no FK on
    purpose so audit rows survive object deletion.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("outcome IN ('ok', 'denied', 'error')", name="ck_audit_events_outcome"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # NULL for anonymous/system; SET NULL so events survive user deletion (D9).
    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(Text, index=True)
    object_type: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[UUID | None] = mapped_column(Uuid())
    ip: Mapped[str | None] = mapped_column(INET)
    outcome: Mapped[str] = mapped_column(Text)
    route: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    tokens_total: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
