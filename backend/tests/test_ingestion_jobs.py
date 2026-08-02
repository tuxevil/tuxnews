import pytest
from app.core.config import Settings
from app.core.context import ActorContext, TenantContext, serialize_job_context
from app.curation.schemas import CurationResult
from app.curation.service import CurationOutcome
from app.db.models import Article, IngestionRun
from app.embeddings.qdrant_index import EmbeddingHit, EmbeddingSpec
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
ARTICLE = b"""
<html><head><title>Full article</title></head><body><article>
<h1>Full article title</h1>
<p>This is a sufficiently long article body with enough detail for the extractor
to identify the main content and return a useful plain text result.</p>
<p>It contains a second paragraph for the integration test.</p>
</article></body></html>
"""


class SuccessfulClient:
    def __init__(self, *_: object) -> None:
        pass

    async def __aenter__(self) -> "SuccessfulClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def fetch(self, url: str) -> FetchResult:
        is_feed = "/feed/" in url
        return FetchResult(
            url=url,
            status_code=200,
            headers={"content-type": "application/rss+xml" if is_feed else "text/html"},
            content=FEED if is_feed else ARTICLE,
        )


class FailingClient:
    def __init__(self, *_: object) -> None:
        pass

    async def __aenter__(self) -> "FailingClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def fetch(self, _: str) -> FetchResult:
        raise HttpFetchError("upstream request timed out")


class SuccessfulCurator:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def curate(self, *, title: str, content: str, profile: str | None = None) -> CurationOutcome:
        self.calls.append({"title": title, "content": content, "profile": profile or ""})
        return CurationOutcome(
            result=CurationResult(
                title="Curated job article",
                summary="A concise article summary.",
                tags=["python"],
                reading_time_minutes=1,
                relevance_score=0.8,
            ),
            fallback_summary="",
            used_fallback=False,
            rejected=False,
            reason=None,
        )


class SuccessfulEmbeddingProvider:
    spec = EmbeddingSpec("test-model", "v1", 3)

    async def embed(self, _: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class FailingEmbeddingProvider(SuccessfulEmbeddingProvider):
    async def embed(self, _: str) -> list[float]:
        raise RuntimeError("embedding unavailable")


class SuccessfulEmbeddingIndex:
    def __init__(self) -> None:
        self.upserts: list[int] = []

    async def upsert(self, *, article_id: int, **_: object) -> None:
        self.upserts.append(article_id)

    async def search(self, *, user_id: int, **_: object) -> tuple[EmbeddingHit, ...]:
        return (EmbeddingHit(article_id=999, score=0.8, payload={"user_id": user_id}),)

    async def aclose(self) -> None:
        return None


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
    curator = SuccessfulCurator()
    monkeypatch.setattr(jobs, "CurationService", lambda: curator)
    embedding_provider = SuccessfulEmbeddingProvider()
    embedding_index = SuccessfulEmbeddingIndex()
    monkeypatch.setattr(jobs, "SentenceTransformerProvider", lambda _: embedding_provider)
    monkeypatch.setattr(jobs, "EmbeddingIndex", lambda: embedding_index)

    first = await jobs.ingest_source({"job_try": 1, "tenant_id": user.id}, run.id)
    assert first["status"] == "succeeded"
    await db_session.refresh(run)
    await db_session.refresh(source)
    assert run.status == "succeeded"
    assert source.last_fetched_at is not None
    article = await db_session.scalar(select(Article))
    assert article is not None
    assert article.status == "published"
    assert article.original_title == "Job article"
    assert article.title == "Curated job article"
    assert article.summary == "A concise article summary."
    assert article.tags == ["python"]
    assert article.content_clean is not None
    assert "sufficiently long article body" in article.content_clean
    assert curator.calls[0]["title"] == "Job article"
    assert curator.calls[0]["profile"] == "eco"
    assert article.embedding_model == "test-model"
    assert article.embedding_version == "v1"
    assert article.score_breakdown["semantic_similarity"] == 0.8
    assert article.score_breakdown["fallback"] == 0.0
    assert article.cluster_id is not None
    assert embedding_index.upserts == [article.id]

    second = await jobs.ingest_source({"job_try": 1, "tenant_id": user.id}, run.id)
    assert second["status"] == "succeeded"
    article_count = await db_session.scalar(select(func.count()).select_from(Article))
    assert article_count == 1


@pytest.mark.asyncio
async def test_ingestion_publishes_with_explicit_score_fallback_when_embedding_fails(
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
    monkeypatch.setattr(jobs, "CurationService", SuccessfulCurator)
    embedding_index = SuccessfulEmbeddingIndex()
    monkeypatch.setattr(jobs, "SentenceTransformerProvider", lambda _: FailingEmbeddingProvider())
    monkeypatch.setattr(jobs, "EmbeddingIndex", lambda: embedding_index)

    result = await jobs.ingest_source({"job_try": 1, "tenant_id": user.id}, run.id)

    assert result["status"] == "succeeded"
    article = await db_session.scalar(select(Article))
    assert article is not None
    assert article.status == "published"
    assert article.embedding_model is None
    assert article.score_breakdown["semantic_similarity"] == 0.0
    assert article.score_breakdown["fallback"] == 1.0
    assert article.cluster_id is not None
    assert embedding_index.upserts == []


@pytest.mark.asyncio
async def test_discovered_article_job_reuses_the_safe_processing_pipeline(
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
    article = Article(
        user_id=user.id,
        source_id=source.id,
        title="Discovered article",
        original_title="Discovered article",
        url="https://example.test/article/1",
        canonical_url_hash="c" * 64,
        content_clean="Search result snippet",
        summary="Search result snippet",
    )
    db_session.add(article)
    await db_session.commit()

    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionFactory", session_factory)
    monkeypatch.setattr(jobs, "SafeHttpClient", SuccessfulClient)
    monkeypatch.setattr(jobs, "CurationService", SuccessfulCurator)
    embedding_provider = SuccessfulEmbeddingProvider()
    embedding_index = SuccessfulEmbeddingIndex()
    monkeypatch.setattr(jobs, "SentenceTransformerProvider", lambda _: embedding_provider)
    monkeypatch.setattr(jobs, "EmbeddingIndex", lambda: embedding_index)

    result = await jobs.ingest_discovered_article({"job_try": 1, "tenant_id": user.id}, article.id)

    assert result["status"] == "published"
    await db_session.refresh(article)
    assert article.status == "published"
    assert article.cluster_id is not None
    assert "sufficiently long article body" in (article.content_clean or "")
    repeated = await jobs.ingest_discovered_article({"job_try": 1, "tenant_id": user.id}, article.id)
    assert repeated["status"] == "published"


@pytest.mark.asyncio
async def test_discovered_article_job_retries_and_marks_failure(
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
    article = Article(
        user_id=user.id,
        source_id=source.id,
        title="Unavailable article",
        original_title="Unavailable article",
        url="https://example.test/article/2",
        canonical_url_hash="d" * 64,
        summary="Search result snippet",
    )
    db_session.add(article)
    await db_session.commit()

    settings = Settings(ingestion_max_attempts=2, ingestion_base_backoff_seconds=0.01)
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionFactory", session_factory)
    monkeypatch.setattr(jobs, "SafeHttpClient", FailingClient)
    monkeypatch.setattr(jobs, "EmbeddingIndex", SuccessfulEmbeddingIndex)

    with pytest.raises(Retry):
        await jobs.ingest_discovered_article({"job_try": 1, "tenant_id": user.id}, article.id)
    await db_session.refresh(article)
    assert article.status == "failed"

    result = await jobs.ingest_discovered_article({"job_try": 2, "tenant_id": user.id}, article.id)
    assert result["status"] == "failed"
    await db_session.refresh(article)
    assert article.status == "failed"


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
