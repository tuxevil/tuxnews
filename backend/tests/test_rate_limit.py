import pytest
from app.core.rate_limit import RedisRateLimiter


class FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.expirations: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expirations[key] = seconds
        return True


@pytest.mark.asyncio
async def test_rate_limiter_uses_hashed_identity_and_enforces_window() -> None:
    redis = FakeRedis()
    limiter = RedisRateLimiter(redis)

    first = await limiter.check(scope="api", identity="secret-token", limit=1, window_seconds=60)
    second = await limiter.check(scope="api", identity="secret-token", limit=1, window_seconds=60)

    assert first.allowed is True
    assert second.allowed is False
    assert list(redis.counts)[0].startswith("tuxnews:rate:api:")
    assert "secret-token" not in list(redis.counts)[0]
    assert redis.expirations[list(redis.counts)[0]] == 60


@pytest.mark.asyncio
async def test_rate_limiter_fails_open_without_exposing_redis_error() -> None:
    class BrokenRedis:
        async def incr(self, _: str) -> int:
            raise OSError("redis password should not be logged")

    decision = await RedisRateLimiter(BrokenRedis()).check(
        scope="api", identity="client", limit=10, window_seconds=60
    )
    assert decision.allowed is True
