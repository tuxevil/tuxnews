import pytest
from app.core.config import Settings
from app.core.quota import (
    COMMIT_LUA,
    RELEASE_LUA,
    RESERVE_LUA,
    QuotaExceeded,
    QuotaRequest,
    QuotaService,
)


class FakeRedis:
    def __init__(self, result: list[int] | None = None) -> None:
        self.result = result or [1, 42, 0]
        self.calls: list[tuple[str, int, tuple[str, ...]]] = []

    async def eval(self, script: str, number_of_keys: int, *values: str) -> list[int]:
        self.calls.append((script, number_of_keys, values))
        if script == RESERVE_LUA:
            return self.result
        return [1]


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "quota_requests_per_window": 100,
        "quota_scope_requests_per_window": 40,
        "quota_operation_requests_per_window": 20,
        "quota_provider_requests_per_window": 10,
        "quota_daily_cost_usd": 2,
        "quota_fail_open": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_quota_reservation_uses_tenant_scoped_dimensions_and_commit() -> None:
    redis = FakeRedis()
    service = QuotaService(redis, _settings())

    decision = await service.enforce(
        QuotaRequest(
            tenant_id=7,
            actor_id="agent:secret-token",
            scope="news:read",
            operation="mcp.search_articles",
            provider="search",
            cost_cents=25,
        )
    )

    assert decision.allowed is True
    assert decision.policy_version == "quota-v1"
    reserve_call = redis.calls[0]
    assert reserve_call[1] == 6
    keys = reserve_call[2][:5]
    assert all("agent:secret-token" not in key for key in keys)
    assert "tenant:7" in keys[0]
    assert redis.calls[1][0] == COMMIT_LUA


@pytest.mark.asyncio
async def test_quota_reservation_releases_all_dimensions_on_failure() -> None:
    redis = FakeRedis()
    service = QuotaService(redis, _settings())
    reservation = await service.reserve(
        QuotaRequest(
            tenant_id=7,
            actor_id="user-7",
            scope="content:write",
            operation="worker.generate_briefing",
            provider="llm",
        )
    )

    await reservation.release()

    assert redis.calls[1][0] == RELEASE_LUA
    assert redis.calls[1][1] == 5
    await reservation.release()
    assert len(redis.calls) == 2


@pytest.mark.asyncio
async def test_quota_exceeded_preserves_retry_after() -> None:
    service = QuotaService(FakeRedis([0, 0, 17]), _settings())

    with pytest.raises(QuotaExceeded) as error:
        await service.enforce(
            QuotaRequest(
                tenant_id=7,
                actor_id="user-7",
                scope="content:read",
                operation="api.feed",
            )
        )

    assert error.value.code == "quota_exceeded"
    assert error.value.retry_after == 17
    assert QuotaService.headers(error.value.decision)["Retry-After"] == "17"
