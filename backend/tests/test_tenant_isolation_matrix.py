from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.archive.paths import ArchivePathError, tenant_relative_path
from app.briefings import service as briefing_service
from app.core.config import Settings
from app.core.context import ActorContext, TenantContext, serialize_job_context
from app.db.models import Article, Briefing, IngestionRun, UsageEvent
from app.discovery import jobs as discovery_jobs
from app.embeddings.qdrant_index import EmbeddingIndex, EmbeddingSpec
from app.ingestion import jobs as ingestion_jobs
from app.mcp import tools as mcp_tools
from app.mcp import use_cases as mcp_use_cases
from app.usage.service import enable_usage_maintenance
from fastmcp.server.auth import AccessToken
from httpx import AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


async def _register(client: AsyncClient, email: str) -> tuple[int, dict[str, str]]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return payload["user"]["id"], {"Authorization": f"Bearer {payload['access_token']}"}


@pytest.mark.asyncio
async def test_rest_tenant_matrix_hides_overlapping_resources(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def public_resolver(_: str, __: int) -> list[object]:
        import ipaddress

        return [ipaddress.ip_address("93.184.216.34")]

    from app.ingestion import http_client

    monkeypatch.setattr(http_client, "_resolve_host", public_resolver)
    owner_a, headers_a = await _register(auth_client, "matrix-a@example.com")
    owner_b, headers_b = await _register(auth_client, "matrix-b@example.com")

    source_a = await auth_client.post(
        "/api/v1/sources",
        headers=headers_a,
        json={"name": "Shared name", "url": "https://matrix.example.test/feed"},
    )
    source_b = await auth_client.post(
        "/api/v1/sources",
        headers=headers_b,
        json={"name": "Shared name", "url": "https://matrix.example.test/feed"},
    )
    assert source_a.status_code == 201
    assert source_b.status_code == 201
    source_b_id = source_b.json()["id"]

    article_b = Article(
        user_id=owner_b,
        source_id=source_b_id,
        title="Tenant B article",
        original_title="Tenant B article",
        url="https://matrix.example.test/article",
        canonical_url_hash=uuid4().hex,
        content_clean="Tenant B content",
        summary="Tenant B summary",
        status="published",
    )
    db_session.add(article_b)
    await db_session.flush()
    briefing_b = Briefing(
        user_id=owner_b,
        briefing_date="2026-08-02",
        local_time="08:00",
        timezone="UTC",
        title="Tenant B briefing",
        content_markdown="B only",
    )
    db_session.add(briefing_b)
    await db_session.commit()

    hidden_source = await auth_client.get(f"/api/v1/sources/{source_b_id}", headers=headers_a)
    hidden_update = await auth_client.patch(
        f"/api/v1/sources/{source_b_id}",
        headers=headers_a,
        json={"name": "cross-tenant"},
    )
    hidden_feedback = await auth_client.post(
        "/api/v1/feedback",
        headers=headers_a,
        json={"action_type": "article", "rating": "like", "article_id": article_b.id},
    )
    hidden_briefing = await auth_client.get(f"/api/v1/briefings/{briefing_b.id}", headers=headers_a)
    feed_a = await auth_client.get("/api/v1/feed", headers=headers_a)
    sources_a = await auth_client.get("/api/v1/sources", headers=headers_a)

    assert hidden_source.status_code == 404
    assert hidden_update.status_code == 404
    assert hidden_feedback.status_code == 404
    assert hidden_briefing.status_code == 404
    assert all(item["id"] != article_b.id for item in feed_a.json()["items"])
    source_a_ids = [source["id"] for source in sources_a.json()]
    assert all(source["id"] != source_b_id for source in sources_a.json())
    assert source_a.json()["id"] in source_a_ids
    assert len(sources_a.json()) == len(source_a_ids)

    settings_a = await auth_client.patch(
        "/api/v1/preferences/settings",
        headers=headers_a,
        json={"llm_profile": "cloud"},
    )
    settings_b = await auth_client.get("/api/v1/preferences/settings", headers=headers_b)
    assert settings_a.status_code == 200
    assert settings_b.status_code == 200
    assert settings_a.json()["llm_profile"] == "cloud"
    assert settings_b.json()["llm_profile"] == "eco"


@pytest.mark.asyncio
async def test_mcp_tenant_matrix_hides_overlapping_briefings(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    user_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_a = user_factory()
    owner_b = user_factory()
    db_session.add_all([owner_a, owner_b])
    await db_session.flush()
    db_session.add(
        Briefing(
            user_id=owner_b.id,
            briefing_date="2026-08-02",
            local_time="08:00",
            timezone="UTC",
            title="Tenant B briefing",
            content_markdown="B only",
        )
    )
    await db_session.commit()
    monkeypatch.setattr(
        mcp_use_cases,
        "SessionFactory",
        async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False),
    )
    token_a = AccessToken(
        token="matrix-a",
        client_id=f"user:{owner_a.id}",
        scopes=["news:read"],
        claims={"sub": str(owner_a.id), "type": "user"},
    )
    token_b = AccessToken(
        token="matrix-b",
        client_id=f"user:{owner_b.id}",
        scopes=["news:read"],
        claims={"sub": str(owner_b.id), "type": "user"},
    )

    briefing_a = await mcp_tools.get_daily_briefing_tool(
        token=token_a,
        ctx=SimpleNamespace(request_id="mcp-a"),
    )
    briefing_b = await mcp_tools.get_daily_briefing_tool(
        token=token_b,
        ctx=SimpleNamespace(request_id="mcp-b"),
    )

    assert briefing_a.found is False
    assert briefing_b.found is True
    assert briefing_b.title == "Tenant B briefing"


@pytest.mark.asyncio
async def test_altered_worker_payloads_are_rejected_before_data_access(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    user_factory,
    source_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = user_factory()
    other = user_factory()
    db_session.add_all([owner, other])
    await db_session.flush()
    source = source_factory(owner.id)
    db_session.add(source)
    await db_session.flush()
    run = IngestionRun(user_id=owner.id, source_id=source.id)
    db_session.add(run)
    await db_session.commit()
    monkeypatch.setattr(
        ingestion_jobs,
        "SessionFactory",
        async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False),
    )
    foreign_payload = serialize_job_context(
        ActorContext(
            tenant=TenantContext(other.id),
            actor_type="agent",
            actor_id="agent:2",
            correlation_id="matrix-job",
        )
    )
    assert (await ingestion_jobs.ingest_source({"job_try": 1}, run.id, foreign_payload))["status"] == "rejected"
    assert (await discovery_jobs.discover_user({}, owner.id, job_payload=foreign_payload))["status"] == "rejected"
    assert (
        await briefing_service.generate_briefing(
            {}, owner.id, "2026-08-02", "08:00", "UTC", job_payload=foreign_payload
        )
    )["status"] == "rejected"


def test_archive_matrix_rejects_foreign_paths() -> None:
    assert tenant_relative_path(1, Path("tenants/1/standalone/story.md"))
    with pytest.raises(ArchivePathError):
        tenant_relative_path(1, Path("tenants/2/standalone/story.md"))


@pytest.mark.asyncio
async def test_qdrant_search_filters_points_by_tenant() -> None:
    settings = Settings(
        qdrant_url="http://qdrant:6333",
        qdrant_collection_prefix=f"matrix_{uuid4().hex[:8]}",
        embedding_dimension=3,
    )
    index = EmbeddingIndex(
        settings,
        spec=EmbeddingSpec(model="matrix", version="v1", dimension=3),
    )
    try:
        try:
            await index.ensure_collection()
        except Exception as exc:
            pytest.skip(f"Qdrant integration service unavailable: {type(exc).__name__}")
        await index.upsert(tenant=TenantContext(1), article_id=1, vector=[1.0, 0.0, 0.0], canonical_url_hash="a")
        await index.upsert(tenant=TenantContext(2), article_id=2, vector=[1.0, 0.0, 0.0], canonical_url_hash="b")
        hits = await index.search(user_id=1, vector=[1.0, 0.0, 0.0], limit=10)
        assert [hit.article_id for hit in hits] == [1]
    finally:
        client = index.client
        try:
            await client.delete_collection(collection_name=index.collection)
        except Exception:
            pass
        await index.aclose()


@pytest.mark.asyncio
async def test_usage_events_are_append_only_with_explicit_maintenance_override(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    user_factory,
) -> None:
    if db_engine.dialect.name != "postgresql":
        pytest.skip("append-only trigger requires PostgreSQL")
    user = user_factory()
    db_session.add(user)
    await db_session.flush()
    event = UsageEvent(
        user_id=user.id,
        tenant_id=user.id,
        actor_type="user",
        actor_id=str(user.id),
        operation="matrix.trigger",
        provider="test",
        model="test/model",
    )
    db_session.add(event)
    await db_session.commit()
    event_id = event.id

    with pytest.raises(DBAPIError):
        await db_session.execute(update(UsageEvent).where(UsageEvent.id == event_id).values(outcome="tampered"))
    await db_session.rollback()

    await enable_usage_maintenance(db_session)
    await db_session.execute(delete(UsageEvent).where(UsageEvent.id == event_id))
    await db_session.commit()
