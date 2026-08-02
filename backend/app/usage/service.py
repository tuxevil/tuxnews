from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import UsageEvent
from app.db.session import SessionFactory
from app.usage.types import UsageRecord


@dataclass(frozen=True)
class UsageBreakdown:
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


@dataclass(frozen=True)
class UsageReport:
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
    breakdown: list[UsageBreakdown]


_SAFE_METADATA = re.compile(r"[^A-Za-z0-9._:@/-]")


def _metadata(value: str, limit: int) -> str:
    return _SAFE_METADATA.sub("_", value)[:limit]


def _percentile(values: list[int], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.999999) - 1))
    return float(ordered[index])


async def record_usage_event(record: UsageRecord) -> None:
    """Persist telemetry independently so a failed caller transaction cannot erase it."""

    async with SessionFactory() as session:
        session.add(
            UsageEvent(
                user_id=record.tenant_id,
                tenant_id=record.tenant_id,
                actor_type=_metadata(record.actor_type, 32),
                actor_id=_metadata(record.actor_id, 120),
                operation=_metadata(record.operation, 120),
                provider=_metadata(record.provider, 80),
                model=_metadata(record.model, 160),
                input_tokens=max(record.input_tokens, 0),
                output_tokens=max(record.output_tokens, 0),
                estimated_cost=max(record.cost_usd, 0.0),
                cost_is_estimated=record.cost_is_estimated,
                cost_currency="USD",
                latency_ms=max(record.latency_ms, 0),
                outcome=_metadata(record.outcome, 24),
                used_fallback=record.used_fallback,
                attempt_count=max(record.attempt_count, 0),
                error_code=_metadata(record.error_code, 120) if record.error_code else None,
                provider_request_id=_metadata(record.provider_request_id, 160)
                if record.provider_request_id
                else None,
                correlation_id=_metadata(record.correlation_id, 64) if record.correlation_id else None,
            )
        )
        await session.commit()


async def enable_usage_maintenance(session: AsyncSession) -> None:
    """Allow intentional retention/deletion paths to bypass the append-only trigger."""

    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        await session.execute(text("SET LOCAL tuxnews.usage_maintenance = 'on'"))


async def delete_usage_events_for_user(session: AsyncSession, *, user_id: int) -> int:
    """Honor a deletion request by removing all usage events for a tenant."""

    await enable_usage_maintenance(session)
    result = await session.execute(delete(UsageEvent).where(UsageEvent.user_id == user_id))
    return int(getattr(result, "rowcount", 0) or 0)


async def purge_expired_usage_events(
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> int:
    settings = get_settings()
    days = retention_days if retention_days is not None else settings.llm_usage_retention_days
    if days < 1:
        raise ValueError("usage retention must be at least one day")
    current = now or datetime.now(UTC)
    cutoff = current.astimezone(UTC) - timedelta(days=days)
    async with SessionFactory() as session:
        await enable_usage_maintenance(session)
        result = await session.execute(delete(UsageEvent).where(UsageEvent.created_at < cutoff))
        await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


async def purge_usage_events(_: dict[Any, Any]) -> dict[str, Any]:
    deleted = await purge_expired_usage_events()
    return {"status": "ok", "deleted": deleted}


async def get_usage_report(
    session: AsyncSession,
    *,
    start_at: datetime,
    end_at: datetime,
    tenant_id: int | None = None,
) -> UsageReport:
    if start_at >= end_at:
        raise ValueError("usage report start must be before end")
    filters = [UsageEvent.created_at >= start_at, UsageEvent.created_at < end_at]
    if tenant_id is not None:
        filters.append(UsageEvent.tenant_id == tenant_id)

    summary = await session.execute(
        select(
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0),
            func.coalesce(func.sum(UsageEvent.estimated_cost), 0.0),
            func.coalesce(func.sum(case((UsageEvent.cost_is_estimated.is_(True), 1), else_=0)), 0),
            func.coalesce(func.sum(case((UsageEvent.used_fallback.is_(True), 1), else_=0)), 0),
            func.coalesce(func.avg(UsageEvent.latency_ms), 0.0),
        ).where(*filters)
    )
    row = summary.one()
    latency_rows = await session.execute(
        select(
            UsageEvent.tenant_id,
            UsageEvent.actor_type,
            UsageEvent.operation,
            UsageEvent.provider,
            UsageEvent.model,
            UsageEvent.latency_ms,
        ).where(*filters)
    )
    latencies_by_group: dict[tuple[int, str, str, str, str], list[int]] = {}
    all_latencies: list[int] = []
    for tenant_key, actor_type, operation, provider, model, latency_ms in latency_rows:
        latency = max(int(latency_ms or 0), 0)
        key = (int(tenant_key), actor_type, operation, provider, model)
        latencies_by_group.setdefault(key, []).append(latency)
        all_latencies.append(latency)
    breakdown_rows = await session.execute(
        select(
            UsageEvent.tenant_id,
            UsageEvent.actor_type,
            UsageEvent.operation,
            UsageEvent.provider,
            UsageEvent.model,
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0),
            func.coalesce(func.sum(UsageEvent.estimated_cost), 0.0),
            func.coalesce(func.sum(case((UsageEvent.cost_is_estimated.is_(True), 1), else_=0)), 0),
            func.coalesce(func.sum(case((UsageEvent.used_fallback.is_(True), 1), else_=0)), 0),
            func.coalesce(func.avg(UsageEvent.latency_ms), 0.0),
        )
        .where(*filters)
        .group_by(
            UsageEvent.tenant_id,
            UsageEvent.actor_type,
            UsageEvent.operation,
            UsageEvent.provider,
            UsageEvent.model,
        )
        .order_by(desc(func.count(UsageEvent.id)), UsageEvent.provider, UsageEvent.model)
    )
    breakdown = [
        UsageBreakdown(
            tenant_id=tenant_id_for_row,
            actor_type=actor_type,
            operation=operation,
            provider=provider,
            model=model,
            event_count=int(event_count),
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            cost_usd=float(cost_usd),
            estimated_event_count=int(estimated_event_count),
            fallback_event_count=int(fallback_event_count),
            average_latency_ms=float(average_latency_ms),
            p95_latency_ms=_percentile(latencies_by_group.get((
                int(tenant_id_for_row),
                actor_type,
                operation,
                provider,
                model,
            ), []), 0.95),
            p99_latency_ms=_percentile(latencies_by_group.get((
                int(tenant_id_for_row),
                actor_type,
                operation,
                provider,
                model,
            ), []), 0.99),
        )
        for (
            tenant_id_for_row,
            actor_type,
            operation,
            provider,
            model,
            event_count,
            input_tokens,
            output_tokens,
            cost_usd,
            estimated_event_count,
            fallback_event_count,
            average_latency_ms,
        ) in breakdown_rows
    ]
    return UsageReport(
        start_at=start_at,
        end_at=end_at,
        tenant_id=tenant_id,
        event_count=int(row[0]),
        input_tokens=int(row[1]),
        output_tokens=int(row[2]),
        cost_usd=float(row[3]),
        estimated_event_count=int(row[4]),
        fallback_event_count=int(row[5]),
        average_latency_ms=float(row[6]),
        p95_latency_ms=_percentile(all_latencies, 0.95),
        p99_latency_ms=_percentile(all_latencies, 0.99),
        breakdown=breakdown,
    )
