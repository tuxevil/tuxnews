from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import IdentityContext, require_scope
from app.core.permissions import Scope
from app.db.session import get_session
from app.usage.service import UsageReport, get_usage_report

router = APIRouter(prefix="/api/v1/admin/usage-events", tags=["usage"])


class UsageBreakdownPublic(BaseModel):
    tenant_id: int
    actor_type: str
    operation: str
    provider: str
    model: str
    event_count: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    estimated_event_count: int
    fallback_event_count: int
    average_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float


class UsageReportPublic(BaseModel):
    start_at: datetime
    end_at: datetime
    tenant_id: int | None
    event_count: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    estimated_event_count: int
    fallback_event_count: int
    average_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    breakdown: list[UsageBreakdownPublic]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _public(report: UsageReport) -> UsageReportPublic:
    return UsageReportPublic(
        start_at=report.start_at,
        end_at=report.end_at,
        tenant_id=report.tenant_id,
        event_count=report.event_count,
        input_tokens=report.input_tokens,
        output_tokens=report.output_tokens,
        cost_usd=report.cost_usd,
        estimated_event_count=report.estimated_event_count,
        fallback_event_count=report.fallback_event_count,
        average_latency_ms=report.average_latency_ms,
        p95_latency_ms=report.p95_latency_ms,
        p99_latency_ms=report.p99_latency_ms,
        breakdown=[UsageBreakdownPublic.model_validate(item.__dict__) for item in report.breakdown],
    )


@router.get("/report", response_model=UsageReportPublic)
async def usage_report(
    start_at: datetime = Query(..., alias="from"),
    end_at: datetime = Query(..., alias="to"),
    tenant_id: int | None = Query(default=None, ge=1),
    identity: IdentityContext = Depends(require_scope(Scope.USAGE_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> UsageReportPublic:
    del identity
    try:
        report = await get_usage_report(
            session,
            start_at=_utc(start_at),
            end_at=_utc(end_at),
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return _public(report)
