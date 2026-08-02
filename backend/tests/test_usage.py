from datetime import UTC, datetime, timedelta

import pytest
from app.core.security import hash_password
from app.db.models import UsageEvent, User
from app.usage.service import get_usage_report
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


def _event(user_id: int, created_at: datetime, *, provider: str, fallback: bool = False) -> UsageEvent:
    return UsageEvent(
        user_id=user_id,
        tenant_id=user_id,
        actor_type="user",
        actor_id=str(user_id),
        operation="test.report",
        provider=provider,
        model=f"{provider}/model",
        input_tokens=10,
        output_tokens=5,
        estimated_cost=0.25,
        cost_is_estimated=not fallback,
        cost_currency="USD",
        latency_ms=12,
        outcome="fallback" if fallback else "success",
        used_fallback=fallback,
        attempt_count=1,
        correlation_id="report-test",
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.mark.asyncio
async def test_usage_report_aggregates_tenant_and_time_range(
    db_session: AsyncSession,
    user_factory,
) -> None:
    user = user_factory()
    other = user_factory()
    db_session.add_all([user, other])
    await db_session.flush()
    start = datetime(2026, 8, 1, tzinfo=UTC)
    db_session.add_all(
        [
            _event(user.id, start + timedelta(hours=1), provider="openai"),
            _event(user.id, start + timedelta(hours=2), provider="openai", fallback=True),
            _event(other.id, start + timedelta(hours=1), provider="ollama"),
            _event(user.id, start - timedelta(days=1), provider="openai"),
        ]
    )
    await db_session.commit()

    report = await get_usage_report(
        db_session,
        start_at=start,
        end_at=start + timedelta(days=1),
        tenant_id=user.id,
    )

    assert report.event_count == 2
    assert report.input_tokens == 20
    assert report.output_tokens == 10
    assert report.cost_usd == 0.5
    assert report.estimated_event_count == 1
    assert report.fallback_event_count == 1
    assert report.p95_latency_ms == 12
    assert report.p99_latency_ms == 12
    assert report.breakdown[0].provider == "openai"
    assert report.breakdown[0].p95_latency_ms == 12
    assert report.breakdown[0].p99_latency_ms == 12


@pytest.mark.asyncio
async def test_admin_usage_report_endpoint_is_range_filtered(
    auth_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    admin = User(
        email="usage-admin@example.com",
        password_hash=hash_password("usage-admin-password"),
        role="admin",
    )
    db_session.add(admin)
    await db_session.flush()
    start = datetime(2026, 8, 1, tzinfo=UTC)
    db_session.add(_event(admin.id, start + timedelta(hours=1), provider="openai"))
    await db_session.commit()

    login = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": admin.email, "password": "usage-admin-password"},
    )
    assert login.status_code == 200
    response = await auth_client.get(
        "/api/v1/admin/usage-events/report",
        params={"from": start.isoformat(), "to": (start + timedelta(days=1)).isoformat()},
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["event_count"] == 1
    assert payload["breakdown"][0]["provider"] == "openai"
