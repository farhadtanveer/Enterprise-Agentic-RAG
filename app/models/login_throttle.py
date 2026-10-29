from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class LoginThrottle(Base):
    """Failed-attempt counter for one (username, client IP) pair.

    ``username_hash`` is sha256(pepper + username) — never the plaintext —
    so the table works for unknown users too and leaks nothing when read
    (docs/backend-design.md §A5). ``salt`` column kept out: the pepper lives
    in settings, not per row, so lookups stay O(1).
    """

    __tablename__ = "login_throttle"

    username_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    ip: Mapped[str] = mapped_column(INET, primary_key=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    throttle_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LoginThrottle {self.username_hash[:8]}… ip={self.ip} fails={self.fail_count}>"
