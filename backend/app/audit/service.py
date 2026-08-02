from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import ActorContext
from app.db.models import AuditEvent
from app.db.session import SessionFactory


def record_audit(
    session: AsyncSession,
    *,
    user_id: int | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    outcome: str,
    correlation_id: str | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
    actor: ActorContext | None = None,
    tenant_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    resolved_actor_type = actor.actor_type if actor is not None else actor_type
    resolved_actor_id = actor.actor_id if actor is not None else actor_id or (
        str(user_id) if user_id is not None else None
    )
    resolved_tenant_id = actor.tenant_id if actor is not None else tenant_id if tenant_id is not None else user_id
    event_details = {
        **(details or {}),
        "actor_type": resolved_actor_type,
        "actor_id": resolved_actor_id,
        "tenant_id": resolved_tenant_id,
    }
    event = AuditEvent(
        user_id=user_id,
        tenant_id=resolved_tenant_id,
        actor_type=resolved_actor_type,
        actor_id=resolved_actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        correlation_id=correlation_id,
        details=event_details,
    )
    session.add(event)
    return event


async def anonymize_audit_for_user(session: AsyncSession, *, user_id: int) -> int:
    """Keep diagnostics but detach audit history from a deleted or requesting user."""

    result = await session.execute(
        update(AuditEvent)
        .where(AuditEvent.user_id == user_id)
        .values(
            user_id=None,
            tenant_id=None,
            actor_type="deleted",
            actor_id=None,
            details={},
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def purge_expired_audit_events(
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> int:
    settings = get_settings()
    days = retention_days if retention_days is not None else settings.audit_retention_days
    if days < 1:
        raise ValueError("audit retention must be at least one day")
    current = now or datetime.now(UTC)
    cutoff = current.astimezone(UTC) - timedelta(days=days)
    async with SessionFactory() as session:
        result = await session.execute(delete(AuditEvent).where(AuditEvent.created_at < cutoff))
        await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


async def purge_audit_events(_: dict[str, Any]) -> dict[str, Any]:
    deleted = await purge_expired_audit_events()
    return {"status": "ok", "deleted": deleted}
