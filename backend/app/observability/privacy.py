from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.audit.service import anonymize_audit_for_user
from app.db.models import AuditEvent, UsageEvent
from app.db.session import SessionFactory
from app.usage.service import delete_usage_events_for_user

_EXPORT_ROW_LIMIT = 200


def _usage_row(event: UsageEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "tenant_id": event.tenant_id,
        "actor_type": event.actor_type,
        "operation": event.operation,
        "provider": event.provider,
        "model": event.model,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "estimated_cost": event.estimated_cost,
        "cost_is_estimated": event.cost_is_estimated,
        "latency_ms": event.latency_ms,
        "outcome": event.outcome,
        "used_fallback": event.used_fallback,
        "attempt_count": event.attempt_count,
        "error_code": event.error_code,
        "created_at": event.created_at,
    }


def _audit_row(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "tenant_id": event.tenant_id,
        "actor_type": event.actor_type,
        "action": event.action,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "outcome": event.outcome,
        "correlation_id": event.correlation_id,
        "created_at": event.created_at,
        "details": event.details,
    }


async def export_user_telemetry(user_id: int) -> dict[str, Any]:
    """Export telemetry for one tenant without session state or PII in URLs."""

    async with SessionFactory() as session:
        usage_count = int(
            await session.scalar(select(func.count(UsageEvent.id)).where(UsageEvent.user_id == user_id)) or 0
        )
        audit_count = int(
            await session.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.user_id == user_id)) or 0
        )
        usage = list(
            await session.scalars(
                select(UsageEvent)
                .where(UsageEvent.user_id == user_id)
                .order_by(UsageEvent.id.desc())
                .limit(_EXPORT_ROW_LIMIT)
            )
        )
        audit = list(
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.user_id == user_id)
                .order_by(AuditEvent.id.desc())
                .limit(_EXPORT_ROW_LIMIT)
            )
        )
    return {
        "user_id": user_id,
        "usage_count": usage_count,
        "audit_count": audit_count,
        "usage_events": [_usage_row(event) for event in usage],
        "audit_events": [_audit_row(event) for event in audit],
    }


async def delete_user_telemetry(user_id: int) -> dict[str, int]:
    """Remove usage history and anonymize audit history for a deletion request."""

    async with SessionFactory() as session:
        deleted_usage = await delete_usage_events_for_user(session, user_id=user_id)
        anonymized_audit = await anonymize_audit_for_user(session, user_id=user_id)
        await session.commit()
    return {"deleted_usage_events": deleted_usage, "anonymized_audit_events": anonymized_audit}
