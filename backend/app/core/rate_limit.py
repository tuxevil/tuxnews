from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from redis.exceptions import RedisError

from app.observability import metrics


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    retry_after: int
    remaining: int


class RedisRateLimiter:
    """Fixed-window limiter using Redis as the shared counter store."""

    def __init__(self, redis: Any, *, key_prefix: str = "tuxnews:rate") -> None:
        self.redis = redis
        self.key_prefix = key_prefix

    async def check(self, *, scope: str, identity: str, limit: int, window_seconds: int) -> RateLimitDecision:
        if limit < 1 or window_seconds < 1:
            raise ValueError("rate-limit configuration must be positive")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        key = f"{self.key_prefix}:{scope}:{digest}"
        timer = metrics.timer("rate_limit.redis")
        try:
            count = int(await self.redis.incr(key))
            if count == 1:
                await self.redis.expire(key, window_seconds)
        except (RedisError, OSError):
            # Availability of Redis must not turn local development into a 500;
            # production health checks should still alert on this fail-open path.
            timer.finish(success=False)
            return RateLimitDecision(True, limit, window_seconds, limit)
        timer.finish(success=True)
        retry_after = window_seconds
        if count > limit and hasattr(self.redis, "ttl"):
            try:
                retry_after = max(int(await self.redis.ttl(key)), 1)
            except (RedisError, OSError, TypeError, ValueError):
                retry_after = window_seconds
        return RateLimitDecision(count <= limit, limit, retry_after, max(limit - count, 0))
