"""audit_events writer (B2 minimal core; B5 expands coverage, §A6).

Content rule (schema-level, test-pinned): ids, counters and enum-ish
strings ONLY — never question/answer text, never credentials, never
attempted usernames (login_failed records actor_user_id for known users,
nothing at all for unknown ones — §A6/§A11.13).

The writer deliberately does NOT commit: callers own the transaction
boundary (docs/backend-design.md §A8 — short TX, explicit commit).
"""
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent


def record_event(
    db: Session,
    event_type: str,
    *,
    actor_user_id: UUID | None = None,
    object_type: str | None = None,
    object_id: UUID | None = None,
    ip: str | None = None,
    outcome: str = "ok",
    route: str | None = None,
    duration_ms: int | None = None,
    tokens_total: int | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_user_id=actor_user_id,
        event_type=event_type,
        object_type=object_type,
        object_id=object_id,
        ip=ip,
        outcome=outcome,
        route=route,
        duration_ms=duration_ms,
        tokens_total=tokens_total,
        details=details or {},
    )
    db.add(event)
    return event
