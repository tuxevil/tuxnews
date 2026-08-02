from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import IdentityContext, require_scope
from app.core.permissions import Scope
from app.db.models import AuditEvent
from app.db.session import get_session

router = APIRouter(prefix="/api/v1/admin/audit-events", tags=["audit"])


class AuditEventPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int | None
    actor_type: str
    actor_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    correlation_id: str | None
    created_at: Any
    details: dict[str, Any]


class AuditEventExport(BaseModel):
    items: list[AuditEventPublic]
    next_before_id: int | None


_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = ("password", "token", "secret", "hash", "cookie", "authorization")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _REDACTED if any(term in key.lower() for term in _SENSITIVE_KEYS) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _public(event: AuditEvent) -> AuditEventPublic:
    return AuditEventPublic(
        id=event.id,
        tenant_id=event.tenant_id,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        outcome=event.outcome,
        correlation_id=event.correlation_id,
        created_at=event.created_at,
        details=_redact(event.details or {}),
    )


@router.get("", response_model=AuditEventExport)
async def export_audit_events(
    tenant_id: int | None = Query(default=None, ge=1),
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    identity: IdentityContext = Depends(require_scope(Scope.AUDIT_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> AuditEventExport:
    del identity
    query = select(AuditEvent).order_by(desc(AuditEvent.id)).limit(limit + 1)
    if tenant_id is not None:
        query = query.where(AuditEvent.tenant_id == tenant_id)
    if before_id is not None:
        query = query.where(AuditEvent.id < before_id)
    events = list(await session.scalars(query))
    has_more = len(events) > limit
    events = events[:limit]
    return AuditEventExport(
        items=[_public(event) for event in events],
        next_before_id=events[-1].id if has_more and events else None,
    )
