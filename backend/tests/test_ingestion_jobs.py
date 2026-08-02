import pytest
from app.core.config import Settings
from app.core.context import ActorContext, TenantContext, serialize_job_context
from app.db.models import Article, IngestionRun
from app.ingestion import jobs
from app.ingestion.http_client import FetchResult, HttpFetchError
from arq import Retry
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

FEED = b"""
<rss version="2.0"><channel><item>
<title>Job article</title><link>https://example.com/job-article</link>
<guid>job-article</guid><description>Job summary</description>
</item></channel></rss>
"""


class SuccessfulClient:
    async def __aenter__(self) -> "SuccessfulClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def fetch(self, _: str) -> FetchResult:
        return FetchResult(
            url="https://example.com/feed",
            status_code=200,
            headers={"content-type": "application/rss+xml"},
            content=FEED,
        )


class FailingClient:
    async def __aenter__(self) -> "FailingClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def fetch(self, _: str) -> FetchResult:
        raise HttpFetchError("upstream request timed out")


@pytest.mark.asyncio
async def test_ingestion_job_is_idempotent_and_updates_run_state(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    user_factory,
    source_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = user_factory()
    db_session.add(user)
    await db_session.flush()
    source = source_factory(user.id)
    db_session.add(source)
    await db_session.flush()
    run = IngestionRun(user_id=user.id, source_id=source.id)
    db_session.add(run)
    await db_session.commit()

    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionFactory", session_factory)
    monkeypatch.setattr(jobs, "SafeHttpClient", SuccessfulClient)

    first = await jobs.ingest_source({"job_try": 1, "tenant_id": user.id}, run.id)
    assert first["status"] == "succeeded"
    await db_session.refresh(run)
    await db_session.refresh(source)
    assert run.status == "succeeded"
    assert source.last_fetched_at is not None
    article = await db_session.scalar(select(Article))
    assert article is not None
    assert article.status == "extracted"

    second = await jobs.ingest_source({"job_try": 1, "tenant_id": user.id}, run.id)
    assert second["status"] == "succeeded"
    article_count = await db_session.scalar(select(func.count()).select_from(Article))
    assert article_count == 1


@pytest.mark.asyncio
async def test_ingestion_job_retries_with_backoff_then_marks_failure(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    user_factory,
    source_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = user_factory()
    db_session.add(user)
    await db_session.flush()
    source = source_factory(user.id)
    db_session.add(source)
    await db_session.flush()
    run = IngestionRun(user_id=user.id, source_id=source.id)
    db_session.add(run)
    await db_session.commit()

    settings = Settings(ingestion_max_attempts=2, ingestion_base_backoff_seconds=0.01)
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionFactory", session_factory)
    monkeypatch.setattr(jobs, "SafeHttpClient", FailingClient)

    with pytest.raises(Retry):
        await jobs.ingest_source({"job_try": 1, "tenant_id": user.id}, run.id)
    await db_session.refresh(run)
    assert run.status == "retrying"
    assert run.attempt == 1

    result = await jobs.ingest_source({"job_try": 2, "tenant_id": user.id}, run.id)
    assert result["status"] == "failed"
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.attempt == 2


@pytest.mark.asyncio
async def test_ingestion_job_rejects_a_foreign_or_missing_tenant_context(
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

    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionFactory", session_factory)
    missing = await jobs.ingest_source({}, run.id)
    foreign = await jobs.ingest_source({"tenant_id": other.id}, run.id)
    explicit_foreign = await jobs.ingest_source(
        {"job_try": 1},
        run.id,
        serialize_job_context(
            ActorContext(
                tenant=TenantContext(other.id),
                actor_type="agent",
                actor_id="agent:foreign",
                correlation_id="job-request",
            )
        ),
    )
    assert missing["status"] == "rejected"
    assert foreign["status"] == "rejected"
    assert explicit_foreign["status"] == "rejected"
