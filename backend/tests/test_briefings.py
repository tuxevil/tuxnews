import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from app.ai.gateway import LLMProfile, LLMResponse
from app.briefings import service
from app.core.config import Settings
from app.db.models import Article, Briefing, BriefingItem
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


class ValidGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **_: object) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=json.dumps(
                {
                    "title": "The daily edition",
                    "executive_summary": "Two stories connect around a useful change.",
                    "key_points": ["The first report adds context."],
                    "caveat": "Verify the source before acting.",
                }
            ),
            profile=LLMProfile.ECO,
            model="fixture",
            used_fallback=False,
        )


class FailingGateway:
    async def complete(self, **_: object) -> LLMResponse:
        return LLMResponse(
            content="provider fallback text",
            profile=LLMProfile.ECO,
            model="fixture",
            used_fallback=True,
            error="provider_unavailable",
        )


@pytest.mark.asyncio
async def test_briefing_is_idempotent_and_regeneration_is_explicit(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    user_factory,
    source_factory,
    article_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = user_factory()
    db_session.add(user)
    await db_session.flush()
    source = source_factory(user.id)
    db_session.add(source)
    await db_session.flush()
    articles: list[Article] = []
    for index in range(3):
        article = article_factory(user.id, source.id)
        article.status = "published"
        article.title = f"Published story {index}"
        article.summary = f"Summary {index}"
        article.relevance_score = 0.9 - (index * 0.1)
        article.published_at = datetime(2026, 8, 1, 8 + index, tzinfo=UTC)
        articles.append(article)
    db_session.add_all(articles)
    await db_session.commit()

    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(service, "SessionFactory", session_factory)
    monkeypatch.setattr(service, "get_settings", lambda: Settings(briefing_max_items=2))
    gateway = ValidGateway()
    first = await service.generate_briefing(
        {"gateway": gateway, "tenant_id": user.id},
        user.id,
        "2026-08-01",
        "08:00",
        "UTC",
    )
    assert first["status"] == "ready"
    assert first["revision"] == 1
    assert first["items"] == 2
    assert gateway.calls == 1

    repeated = await service.generate_briefing(
        {"gateway": gateway, "tenant_id": user.id},
        user.id,
        "2026-08-01",
        "08:00",
        "UTC",
    )
    assert repeated["briefing_id"] == first["briefing_id"]
    assert repeated["revision"] == 1
    assert gateway.calls == 1

    regenerated = await service.generate_briefing(
        {"gateway": gateway, "tenant_id": user.id},
        user.id,
        "2026-08-01",
        "08:00",
        "UTC",
        True,
    )
    assert regenerated["briefing_id"] == first["briefing_id"]
    assert regenerated["revision"] == 2
    assert await db_session.scalar(select(func.count()).select_from(Briefing)) == 1
    assert await db_session.scalar(select(func.count()).select_from(BriefingItem)) == 2

    fallback = await service.generate_briefing(
        {"gateway": FailingGateway(), "tenant_id": user.id},
        user.id,
        "2026-08-01",
        "08:00",
        "UTC",
        True,
    )
    assert fallback["revision"] == 3
    assert fallback["used_fallback"] is True
    refreshed = await service.get_briefing_view(
        db_session,
        user_id=user.id,
        briefing_id=int(first["briefing_id"]),
    )
    assert refreshed is not None
    assert refreshed.error_message == "provider_unavailable"


@pytest.mark.asyncio
async def test_briefing_fallback_is_valid_and_owner_scoped(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    auth_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "briefing-owner@example.com", "password": "correct horse battery staple"},
    )
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(service, "SessionFactory", session_factory)
    monkeypatch.setattr(service, "get_settings", lambda: Settings(briefing_max_items=2))

    created = await auth_client.post(
        "/api/v1/briefings/generate",
        headers=headers,
        json={
            "briefing_date": datetime.now(ZoneInfo("UTC")).date().isoformat(),
            "local_time": "08:00",
            "timezone": "UTC",
        },
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["status"] == "ready"
    assert payload["security_context"] == "UNTRUSTED_EXTERNAL_DATA"
    assert payload["items"] == []
    assert "No published stories" in payload["content_markdown"]

    history = await auth_client.get("/api/v1/briefings", headers=headers)
    assert history.status_code == 200
    assert [item["id"] for item in history.json()] == [payload["id"]]
    schedule = await auth_client.get("/api/v1/briefings/schedule", headers=headers)
    assert schedule.status_code == 200
    assert schedule.json()["local_time"] == "08:00"
    updated_schedule = await auth_client.put(
        "/api/v1/briefings/schedule",
        headers=headers,
        json={"local_time": "07:30", "timezone": "Europe/Madrid", "is_active": False},
    )
    assert updated_schedule.status_code == 200
    assert updated_schedule.json()["timezone"] == "Europe/Madrid"
    assert updated_schedule.json()["is_active"] is False
    invalid_schedule = await auth_client.put(
        "/api/v1/briefings/schedule",
        headers=headers,
        json={"local_time": "07:30", "timezone": "Not/AZone", "is_active": True},
    )
    assert invalid_schedule.status_code == 422
    today = await auth_client.get("/api/v1/briefings/today", headers=headers)
    assert today.status_code == 200
    assert today.json()["id"] == payload["id"]

    other = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "briefing-other@example.com", "password": "correct horse battery staple"},
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    hidden = await auth_client.get(f"/api/v1/briefings/{payload['id']}", headers=other_headers)
    assert hidden.status_code == 404
