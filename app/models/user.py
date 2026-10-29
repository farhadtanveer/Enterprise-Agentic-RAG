from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Usernames are stored lowercase; the CHECK pins what the services
        # normalize (docs/backend-design.md §A4).
        CheckConstraint("username = lower(username)", name="ck_users_username_lowercase"),
        CheckConstraint("role IN ('admin', 'user')", name="ck_users_role"),
    )

    # uuidv7() as the Postgres-side default: the DB is the single ID
    # authority (Postgres 18 native generator, §A4).
    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, server_default=func.uuidv7())
    username: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.username} role={self.role} active={self.is_active}>"
