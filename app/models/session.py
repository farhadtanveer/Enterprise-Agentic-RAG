from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, Uuid, func
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        # sha256 hex digest of the session token — 64 lowercase hex chars.
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_sessions_token_hash_sha256"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, server_default=func.uuidv7())
    user_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # NEVER the token itself — a leaked sessions table can't be replayed (§A2).
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # absolute cap
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # sliding
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Session user={self.user_id} revoked={self.revoked_at is not None}>"
