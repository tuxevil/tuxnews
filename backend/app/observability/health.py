from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.db.session import engine
from app.observability.metrics import metrics

WORKER_HEARTBEAT_KEY = "tuxnews:health:worker"


@dataclass(frozen=True)
class DependencyStatus:
    status: str
    latency_ms: float
    detail: str | None = None


@dataclass(frozen=True)
class HealthSnapshot:
    status: str
    readiness: str
    checked_at: float
    checks: dict[str, DependencyStatus]


async def _probe(
    name: str,
    operation: Any,
    *,
    timeout_seconds: float,
) -> DependencyStatus:
    started = perf_counter()
    try:
        await asyncio.wait_for(operation(), timeout=timeout_seconds)
    except TimeoutError:
        status = DependencyStatus("down", _elapsed_ms(started), "timeout")
        metrics.observe(f"health.{name}", status.latency_ms, success=False)
        return status
    except Exception:
        status = DependencyStatus("down", _elapsed_ms(started), "unavailable")
        metrics.observe(f"health.{name}", status.latency_ms, success=False)
        return status
    status = DependencyStatus("ok", _elapsed_ms(started))
    metrics.observe(f"health.{name}", status.latency_ms, success=True)
    return status


def _elapsed_ms(started: float) -> float:
    return round(max(perf_counter() - started, 0.0) * 1000, 2)


async def _database_probe() -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def _redis_probe(settings: Settings) -> None:
    redis = Redis.from_url(settings.redis_url)
    try:
        await redis.ping()
        try:
            queue_depth = await redis.llen("arq:queue")
        except Exception:
            queue_depth = 0
        metrics.set_gauge("worker.queue_depth", queue_depth)
    finally:
        await redis.aclose()


async def _qdrant_probe(settings: Settings) -> None:
    client = AsyncQdrantClient(url=settings.qdrant_url, timeout=int(settings.health_check_timeout_seconds))
    try:
        await client.get_collections()
    finally:
        await client.close()


async def _worker_probe(settings: Settings) -> None:
    redis = Redis.from_url(settings.redis_url)
    try:
        raw_heartbeat = await redis.get(WORKER_HEARTBEAT_KEY)
        if raw_heartbeat is None:
            raise RuntimeError("worker heartbeat unavailable")
        try:
            heartbeat = float(raw_heartbeat)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("worker heartbeat invalid") from exc
        metrics.set_gauge("worker.heartbeat_age_seconds", max(time.time() - heartbeat, 0.0))
    finally:
        await redis.aclose()


async def collect_health(settings: Settings | None = None) -> HealthSnapshot:
    active_settings = settings or get_settings()
    database, redis, qdrant, worker = await asyncio.gather(
        _probe("database", _database_probe, timeout_seconds=active_settings.health_check_timeout_seconds),
        _probe(
            "redis",
            lambda: _redis_probe(active_settings),
            timeout_seconds=active_settings.health_check_timeout_seconds,
        ),
        _probe(
            "qdrant",
            lambda: _qdrant_probe(active_settings),
            timeout_seconds=active_settings.health_check_timeout_seconds,
        ),
        _probe(
            "worker",
            lambda: _worker_probe(active_settings),
            timeout_seconds=active_settings.health_check_timeout_seconds,
        ),
    )
    checks = {"database": database, "redis": redis, "qdrant": qdrant, "worker": worker}
    core_checks = tuple(
        checks.get(name, DependencyStatus("down", 0.0, "missing")) for name in ("database", "redis")
    )
    if any(check.status == "down" for check in core_checks):
        overall = "unavailable"
        readiness = "not_ready"
    elif any(
        checks.get(name, DependencyStatus("down", 0.0, "missing")).status == "down"
        for name in ("qdrant", "worker")
    ):
        overall = "degraded"
        readiness = "ready"
    else:
        overall = "healthy"
        readiness = "ready"
    return HealthSnapshot(
        status=overall,
        readiness=readiness,
        checked_at=time.time(),
        checks=checks,
    )
