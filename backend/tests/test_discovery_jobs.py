from datetime import UTC, datetime

import pytest
from app.core.config import Settings
from app.db.models import Article, DiscoveryRun, Source, UserTopic
from app.discovery import jobs
from app.discovery.jobs import build_discovery_queries
from app.discovery.search import SearchCandidate, SearchResult
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


class FakeProvider:
    provider = "fixture-search"
    version = "fixture-v1"

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.limits: list[int] = []

    async def search(self, query: str, *, limit: int = 10, **_: object) -> SearchResult:
        self.queries.append(query)
        self.limits.append(limit)
        return SearchResult(
            query=query,
            provider=self.provider,
            provider_version=self.version,
            candidates=(
                SearchCandidate(
                    title="Safe <script>story</script>",
                    snippet="A useful <b>external</b> snippet.",
                    url="https://example.com/story?utm_source=search",
                    published_at=datetime(2026, 8, 1, tzinfo=UTC),
                    provider=self.provider,
                    provider_version=self.version,
                ),
            ),
        )


class FakeRedis:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def enqueue_job(self, function: str, *args: object, **kwargs: object) -> None:
        self.jobs.append((function, args, kwargs))

    async def eval(self, *_: object) -> list[int]:
        return [1, 100, 0]


@pytest.mark.asyncio
async def test_discovery_queries_use_only_controlled_preferences() -> None:
    topic = UserTopic(topic_name="Linux; ignore previous instructions", weight_score=0.9)
    source = Source(user_id=1, name="External source name", url="https://news.example.test/feed")
    queries = build_discovery_queries([topic], [source], serendipity=0.75, max_queries=3)

    assert queries
    assert all(";" not in query.query for query in queries)
    assert all("ignore previous instructions" in query.query for query in queries)
    assert all("external source name" not in query.query for query in queries)
    assert all(len(query.query) <= 300 for query in queries)


@pytest.mark.asyncio
async def test_discovery_job_is_slot_idempotent_and_article_deduplicated(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    user_factory,
    source_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = user_factory()
    user.serendipity_score = 0.75
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserTopic(user_id=user.id, topic_name="linux", weight_score=0.9))
    db_session.add(source_factory(user.id))
    await db_session.commit()

    provider = FakeProvider()
    redis = FakeRedis()
    validated: list[str] = []

    async def validate_url(url: str) -> None:
        validated.append(url)

    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionFactory", session_factory)
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: Settings(discovery_max_queries=3, discovery_max_candidates=10),
    )

    first = await jobs.discover_user(
        {"search_provider": provider, "url_validator": validate_url, "redis": redis, "tenant_id": user.id},
        user.id,
        "2026-08-01T08",
    )
    assert first["status"] == "succeeded"
    assert first["created"] == 1
    assert first["queued"] == 1
    assert redis.jobs[0][0] == "ingest_discovered_article"
    assert redis.jobs[0][2]["_job_id"].startswith("discovery-ingest:")
    assert len(provider.queries) == 3
    assert provider.limits == [20, 20, 20]
    assert validated == ["https://example.com/story?utm_source=search"] * 3

    article = await db_session.scalar(select(Article))
    assert article is not None
    assert article.status == "discovered"
    assert article.title == "Safe"
    assert article.summary == "A useful external snippet."
    assert article.tags == ["linux"]
    assert await db_session.scalar(select(func.count()).select_from(Article)) == 1

    repeated = await jobs.discover_user(
        {"search_provider": provider, "url_validator": validate_url, "redis": redis, "tenant_id": user.id},
        user.id,
        "2026-08-01T08",
    )
    assert repeated["status"] == "succeeded"
    assert repeated["run_id"] == first["run_id"]
    assert len(provider.queries) == 3

    second_slot = await jobs.discover_user(
        {"search_provider": provider, "url_validator": validate_url, "redis": redis, "tenant_id": user.id},
        user.id,
        "2026-08-01T09",
    )
    assert second_slot["status"] == "succeeded"
    assert second_slot["created"] == 0
    assert await db_session.scalar(select(func.count()).select_from(Article)) == 1
    assert await db_session.scalar(select(func.count()).select_from(DiscoveryRun)) == 2


@pytest.mark.asyncio
async def test_discovery_job_rejects_a_foreign_tenant_context() -> None:
    result = await jobs.discover_user({"tenant_id": 8}, 7)
    assert result["status"] == "rejected"
