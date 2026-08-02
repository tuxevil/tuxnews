from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import IdentityContext, require_scope
from app.audit.service import record_audit
from app.core.permissions import Scope
from app.db.session import get_session
from app.observability.privacy import delete_user_telemetry, export_user_telemetry

router = APIRouter(prefix="/api/v1/admin/telemetry", tags=["telemetry"])


class TelemetryExportPublic(BaseModel):
    user_id: int
    usage_count: int
    audit_count: int
    usage_events: list[dict[str, Any]]
    audit_events: list[dict[str, Any]]


class TelemetryDeletionPublic(BaseModel):
    user_id: int
    deleted_usage_events: int
    anonymized_audit_events: int


def _correlation_id(request: Request) -> str | None:
    value = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, str) else None


@router.get("/{user_id}", response_model=TelemetryExportPublic)
async def export_user_telemetry_route(
    user_id: int,
    identity: IdentityContext = Depends(require_scope(Scope.USERS_MANAGE.value)),
    session: AsyncSession = Depends(get_session),
) -> TelemetryExportPublic:
    del identity, session
    return TelemetryExportPublic(**await export_user_telemetry(user_id))


@router.delete("/{user_id}", response_model=TelemetryDeletionPublic)
async def delete_user_telemetry_route(
    user_id: int,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.USERS_MANAGE.value)),
    session: AsyncSession = Depends(get_session),
) -> TelemetryDeletionPublic:
    actor_id = str(identity.user.id)
    result = await delete_user_telemetry(user_id)
    record_audit(
        session,
        user_id=None,
        action="telemetry.deleted",
        resource_type="telemetry",
        resource_id=str(user_id),
        outcome="success",
        correlation_id=_correlation_id(request),
        actor_type="admin",
        actor_id=actor_id,
        tenant_id=user_id,
        details=result,
    )
    await session.commit()
    return TelemetryDeletionPublic(user_id=user_id, **result)
