from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache, wraps
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings, get_settings
from app.core.context import job_context_from_payload
from app.observability import log_event, metrics

logger = logging.getLogger(__name__)

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.:-]")

RESERVE_LUA = """
local dimensions = #KEYS - 1
local retry_after = 0
local remaining = nil
for i = 1, dimensions do
  local offset = (i - 1) * 3
  local limit = tonumber(ARGV[offset + 1])
  local current = tonumber(redis.call('GET', KEYS[i]) or '0')
  if current + tonumber(ARGV[offset + 3]) > limit then
    local ttl = redis.call('TTL', KEYS[i])
    if ttl > retry_after then retry_after = ttl end
    return {0, math.max(limit - current, 0), retry_after}
  end
  local available = limit - current
  if remaining == nil or available < remaining then remaining = available end
end
for i = 1, dimensions do
  local offset = (i - 1) * 3
  local units = tonumber(ARGV[offset + 3])
  local next_value = redis.call('INCRBY', KEYS[i], units)
  if next_value == units then redis.call('EXPIRE', KEYS[i], tonumber(ARGV[offset + 2])) end
  redis.call('HSET', KEYS[#KEYS], KEYS[i], units)
end
redis.call('EXPIRE', KEYS[#KEYS], tonumber(ARGV[dimensions * 3 + 2]))
return {1, math.max(remaining - tonumber(ARGV[3]), 0), 0}
"""

RELEASE_LUA = """
for i = 2, #KEYS do
  local units = tonumber(redis.call('HGET', KEYS[1], KEYS[i]) or '0')
  local current = tonumber(redis.call('GET', KEYS[i]) or '0')
  if units > 0 and current > 0 then
    local next_value = current - math.min(current, units)
    if next_value > 0 then redis.call('SET', KEYS[i], next_value, 'KEEPTTL')
    else redis.call('DEL', KEYS[i]) end
  end
end
redis.call('DEL', KEYS[1])
return 1
"""

COMMIT_LUA = "return redis.call('DEL', KEYS[1])"


@dataclass(frozen=True)
class QuotaPolicy:
    version: str
    requests_per_window: int
    scope_requests_per_window: int
    operation_requests_per_window: int
    provider_requests_per_window: int
    window_seconds: int
    daily_cost_cents: int
    reservation_ttl_seconds: int
    fail_open: bool

    @classmethod
    def from_settings(cls, settings: Settings) -> QuotaPolicy:
        return cls(
            version=settings.quota_policy_version,
            requests_per_window=settings.quota_requests_per_window,
            scope_requests_per_window=settings.quota_scope_requests_per_window,
            operation_requests_per_window=settings.quota_operation_requests_per_window,
            provider_requests_per_window=settings.quota_provider_requests_per_window,
            window_seconds=settings.quota_window_seconds,
            daily_cost_cents=round(settings.quota_daily_cost_usd * 100),
            reservation_ttl_seconds=settings.quota_reservation_ttl_seconds,
            fail_open=settings.quota_fail_open,
        )


@dataclass(frozen=True)
class QuotaRequest:
    tenant_id: int
    actor_id: str
    scope: str
    operation: str
    provider: str | None = None
    units: int = 1
    cost_cents: int = 0
    reservation_id: str | None = None


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    policy_version: str


class QuotaExceeded(RuntimeError):
    code = "quota_exceeded"

    def __init__(self, decision: QuotaDecision) -> None:
        self.decision = decision
        super().__init__(self.code)

    @property
    def retry_after(self) -> int:
        return self.decision.retry_after


class QuotaBackendUnavailable(RuntimeError):
    code = "quota_backend_unavailable"


@dataclass
class QuotaReservation:
    service: QuotaService
    reservation_id: str | None
    keys: tuple[str, ...]
    decision: QuotaDecision
    _closed: bool = False

    async def commit(self) -> None:
        if self._closed or self.reservation_id is None:
            return
        await self.service.commit(self)
        self._closed = True

    async def release(self) -> None:
        if self._closed or self.reservation_id is None:
            return
        await self.service.release(self)
        self._closed = True

    async def __aenter__(self) -> QuotaReservation:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            await self.commit()
        else:
            await self.release()


class QuotaService:
    """Tenant quota admission shared by HTTP, MCP and worker entrypoints."""

    def __init__(self, redis: Any, settings: Settings | None = None, *, key_prefix: str = "tuxnews:quota") -> None:
        self.redis = redis
        self.settings = settings or get_settings()
        self.policy = QuotaPolicy.from_settings(self.settings)
        self.key_prefix = key_prefix

    def _component(self, value: int | str) -> str:
        return _SAFE_COMPONENT.sub("_", str(value))[:120] or "unknown"

    def _dimensions(self, request: QuotaRequest) -> tuple[tuple[str, int, int, int], ...]:
        tenant = self._component(request.tenant_id)
        scope = self._component(request.scope)
        operation = self._component(request.operation)
        units = max(request.units, 1)
        dimensions: list[tuple[str, int, int, int]] = [
            (
                f"{self.key_prefix}:tenant:{tenant}:requests",
                self.policy.requests_per_window,
                self.policy.window_seconds,
                units,
            ),
            (
                f"{self.key_prefix}:tenant:{tenant}:scope:{scope}",
                self.policy.scope_requests_per_window,
                self.policy.window_seconds,
                units,
            ),
            (
                f"{self.key_prefix}:tenant:{tenant}:operation:{operation}",
                self.policy.operation_requests_per_window,
                self.policy.window_seconds,
                units,
            ),
        ]
        if request.provider:
            dimensions.append(
                (
                    f"{self.key_prefix}:tenant:{tenant}:provider:{self._component(request.provider)}",
                    self.policy.provider_requests_per_window,
                    self.policy.window_seconds,
                    units,
                )
            )
        if request.cost_cents > 0 and self.policy.daily_cost_cents > 0:
            dimensions.append(
                (
                    f"{self.key_prefix}:tenant:{tenant}:cost:daily",
                    self.policy.daily_cost_cents,
                    86_400,
                    request.cost_cents,
                )
            )
        return tuple(dimensions)

    def _allowed_decision(self, *, remaining: int | None = None) -> QuotaDecision:
        return QuotaDecision(
            allowed=True,
            limit=self.policy.requests_per_window,
            remaining=max(self.policy.requests_per_window if remaining is None else remaining, 0),
            retry_after=0,
            policy_version=self.policy.version,
        )

    async def reserve(self, request: QuotaRequest) -> QuotaReservation:
        if request.tenant_id < 1 or not request.actor_id:
            raise ValueError("quota identity is required")
        if request.units < 1 or request.cost_cents < 0:
            raise ValueError("quota units must be positive and cost cannot be negative")
        dimensions = self._dimensions(request)
        reservation_id = request.reservation_id or uuid4().hex
        lease_key = f"{self.key_prefix}:lease:{reservation_id}"
        keys = tuple(item[0] for item in dimensions)
        args: list[str] = []
        for _, limit, window, units in dimensions:
            args.extend((str(limit), str(window), str(units)))
        args.extend((reservation_id, str(self.policy.reservation_ttl_seconds)))
        timer = metrics.timer("quota.reserve")
        try:
            result = await self.redis.eval(RESERVE_LUA, len(keys) + 1, *keys, lease_key, *args)
        except (RedisError, OSError) as exc:
            timer.finish(success=self.policy.fail_open)
            metrics.set_gauge("quota.backend_available", 0)
            if self.policy.fail_open:
                log_event(logger, "quota.backend_bypassed", level=logging.WARNING, operation=request.operation)
                return QuotaReservation(self, None, (), self._allowed_decision(), _closed=True)
            raise QuotaBackendUnavailable("quota backend unavailable") from exc
        timer.finish(success=True)
        metrics.set_gauge("quota.backend_available", 1)
        allowed = bool(int(result[0]))
        remaining = max(int(result[1]), 0)
        retry_after = max(int(result[2]), 0)
        decision = QuotaDecision(
            allowed=allowed,
            limit=min(item[1] for item in dimensions),
            remaining=remaining,
            retry_after=retry_after,
            policy_version=self.policy.version,
        )
        if not allowed:
            metrics.observe("quota.rejected", 0, success=False)
            log_event(
                logger,
                "quota.rejected",
                tenant_id=request.tenant_id,
                actor_id=request.actor_id,
                operation=request.operation,
                scope=request.scope,
                provider=request.provider,
                retry_after=retry_after,
            )
            return QuotaReservation(self, None, (), decision, _closed=True)
        metrics.observe("quota.allowed", 0, success=True)
        return QuotaReservation(self, reservation_id, (lease_key, *keys), decision)

    async def enforce(self, request: QuotaRequest) -> QuotaDecision:
        reservation = await self.reserve(request)
        if not reservation.decision.allowed:
            raise QuotaExceeded(reservation.decision)
        await reservation.commit()
        return reservation.decision

    async def commit(self, reservation: QuotaReservation) -> None:
        if reservation.reservation_id is None:
            return
        try:
            await self.redis.eval(COMMIT_LUA, 1, reservation.keys[0])
        except (RedisError, OSError) as exc:
            if self.policy.fail_open:
                return
            raise QuotaBackendUnavailable("quota commit failed") from exc

    async def release(self, reservation: QuotaReservation) -> None:
        if reservation.reservation_id is None:
            return
        try:
            await self.redis.eval(RELEASE_LUA, len(reservation.keys), *reservation.keys)
        except (RedisError, OSError) as exc:
            if self.policy.fail_open:
                return
            raise QuotaBackendUnavailable("quota release failed") from exc

    @staticmethod
    def headers(decision: QuotaDecision) -> dict[str, str]:
        return {
            "RateLimit-Limit": str(decision.limit),
            "RateLimit-Remaining": str(decision.remaining),
            "RateLimit-Reset": str(decision.retry_after),
            "Retry-After": str(decision.retry_after),
        }


@lru_cache(maxsize=1)
def get_quota_service() -> QuotaService:
    settings = get_settings()
    return QuotaService(Redis.from_url(settings.redis_url), settings)


async def enforce_job_quota(
    ctx: dict[str, Any],
    *,
    tenant_id: int,
    actor_id: str,
    scope: str,
    operation: str,
    provider: str | None = None,
) -> QuotaDecision:
    redis = ctx.get("redis")
    service = QuotaService(redis, get_settings()) if redis is not None else get_quota_service()
    return await service.enforce(
        QuotaRequest(
            tenant_id=tenant_id,
            actor_id=actor_id,
            scope=scope,
            operation=operation,
            provider=provider,
        )
    )


def quota_guard(
    *,
    scope: str,
    operation: str,
    provider: str | None = None,
    payload_position: int | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Reserve worker quota and release it when the job fails or retries."""

    def decorate(function: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(function)
        async def wrapped(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            if ctx.get("_quota_checked") is True:
                return await function(ctx, *args, **kwargs)
            payload: Mapping[str, Any] = ctx
            candidate = kwargs.get("job_payload")
            if candidate is None and payload_position is not None and payload_position < len(args):
                candidate = args[payload_position]
            if isinstance(candidate, Mapping):
                payload = candidate
            job = job_context_from_payload(payload)
            if job is None:
                return await function(ctx, *args, **kwargs)
            redis = ctx.get("redis")
            service = QuotaService(redis, get_settings()) if redis is not None else get_quota_service()
            try:
                reservation = await service.reserve(
                    QuotaRequest(
                        tenant_id=job.tenant.tenant_id,
                        actor_id=job.actor.actor_id,
                        scope=scope,
                        operation=operation,
                        provider=provider,
                    )
                )
            except QuotaBackendUnavailable:
                return {"status": "quota_unavailable", "tenant_id": job.tenant.tenant_id}
            if not reservation.decision.allowed:
                return {
                    "status": "quota_exceeded",
                    "tenant_id": job.tenant.tenant_id,
                    "retry_after": reservation.decision.retry_after,
                }
            try:
                result = await function(ctx, *args, **kwargs)
            except Exception:
                await reservation.release()
                raise
            if isinstance(result, Mapping) and result.get("status") in {"failed", "quota_unavailable"}:
                await reservation.release()
            else:
                await reservation.commit()
            return result

        return wrapped

    return decorate
