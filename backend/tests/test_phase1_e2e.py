from pathlib import Path

import pytest
from app.archive.markdown import ArchiveMetadata, export_article
from app.archive.paths import AtomicArchiveWriter
from app.curation.service import CurationOutcome
from app.db.models import Article, IngestionRun
from app.ingestion import jobs
from app.ingestion.http_client import FetchResult
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

RSS = b"""
<rss version="2.0"><channel><item>
<title>Vertical article</title><link>https://example.com/vertical</link>
<guid>vertical-1</guid><description>Feed preview.</description>
</item></channel></rss>
"""
ARTICLE = b"""
<html><body><article><h1>Vertical article</h1>
<p>Safe vertical content with enough detail to exercise the article extractor.</p>
<p>The article body is persisted before it is archived.</p>
</article></body></html>
"""


class VerticalClient:
    async def __aenter__(self) -> "VerticalClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def fetch(self, url: str) -> FetchResult:
        is_feed = "/feed/" in url
        return FetchResult(
            url=url,
            status_code=200,
            headers={"content-type": "application/rss+xml" if is_feed else "text/html"},
            content=RSS if is_feed else ARTICLE,
        )


class FallbackCurator:
    async def curate(
        self,
        *,
        title: str,
        content: str,
        profile: str | None = None,
        use_llm: bool = True,
    ) -> CurationOutcome:
        return CurationOutcome(None, content, True, False, "provider_unavailable")


@pytest.mark.asyncio
async def test_phase1_source_to_published_article_to_archive(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    user_factory,
    source_factory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    monkeypatch.setattr(jobs, "SafeHttpClient", VerticalClient)
    monkeypatch.setattr(jobs, "CurationService", FallbackCurator)

    result = await jobs.ingest_source({"job_try": 1, "tenant_id": user.id}, run.id)
    assert result["status"] == "succeeded"

    article = await db_session.scalar(select(Article).where(Article.user_id == user.id))
    assert article is not None
    assert article.status == "published"
    assert article.summary is not None
    assert "Safe vertical content" in article.content_clean

    writer = AtomicArchiveWriter(tmp_path / "news-archive")
    export = await export_article(
        db_session,
        article,
        ArchiveMetadata(source.name, source.url),
        writer,
    )
    archived = (tmp_path / "news-archive" / export.path).read_text(encoding="utf-8")
    assert export.status == "ready"
    assert "title: Vertical article" in archived
    assert "Safe vertical content" in archived
    assert (tmp_path / "news-archive" / export.path).is_file()
