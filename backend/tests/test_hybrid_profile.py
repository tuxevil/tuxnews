from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.curation.schemas import CurationResult
from app.curation.service import CurationOutcome
from app.ingestion import jobs
from app.ingestion.feed_parser import NormalizedEntry
from app.ingestion.http_client import FetchResult
from app.ranking.scoring import ScoreWeights
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class FakeIndex:
    def __init__(self) -> None:
        self.upserted = 0

    async def upsert(self, **_: object) -> None:
        self.upserted += 1

    async def search(self, **_: object) -> tuple[object, ...]:
        return ()

    async def aclose(self) -> None:
        return None


class FakeEmbed:
    spec = SimpleNamespace(model="test-model", version="v1")

    async def embed(self, _: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class CountingCurator:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, bool]] = []

    async def curate(
        self,
        *,
        title: str,
        content: str,
        profile: str | None = None,
        use_llm: bool = True,
    ) -> CurationOutcome:
        self.calls.append((profile, use_llm))
        if use_llm:
            result = CurationResult(
                title=f"LLM {title}",
                summary="LLM summary",
                tags=["curated"],
                reading_time_minutes=2,
                relevance_score=0.9,
            )
            return CurationOutcome(result, "fallback", False, False, None)
        return CurationOutcome(None, "fallback summary", True, False, "hybrid_fallback")


class VerticalClient:
    async def __aenter__(self) -> "VerticalClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def fetch(self, url: str) -> FetchResult:
        return FetchResult(
            url=url,
            status_code=200,
            headers={"content-type": "text/html"},
            content=b"<html><body><p>Body content for the article.</p></body></html>",
        )


def _entry(number: int) -> NormalizedEntry:
    return NormalizedEntry(
        title=f"Hybrid story {number}",
        url=f"https://example.com/hybrid/{number}",
        canonical_url=f"https://example.com/hybrid/{number}",
        canonical_url_hash=f"{number:064d}",
        guid=f"hybrid-{number}",
        author=None,
        summary=f"Summary {number}",
        content=f"<p>Content {number}</p>",
        tags=("news",),
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
        image_url=None,
    )


@pytest.mark.asyncio
async def test_hybrid_profile_curates_only_the_top_tier_with_llm(
    db_engine,
    db_session: AsyncSession,
    user_factory,
    source_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = user_factory()
    db_session.add(user)
    await db_session.flush()
    source = source_factory(user.id)
    source.reputation_score = 0.8
    db_session.add(source)
    await db_session.flush()
    from app.db.models import IngestionRun

    run = IngestionRun(user_id=user.id, source_id=source.id)
    db_session.add(run)
    await db_session.commit()

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionFactory", session_factory)
    curator = CountingCurator()
    index = FakeIndex()

    from app.core.context import ActorContext, TenantContext

    actor = ActorContext(tenant=TenantContext(user.id), actor_type="test", actor_id="test")
    created = await jobs._upsert_articles(
        db_session,
        run,
        source,
        tuple(_entry(number) for number in range(1, 6)),
        client=VerticalClient(),
        curator=curator,  # type: ignore[arg-type]
        embedding_provider=FakeEmbed(),  # type: ignore[arg-type]
        index=index,  # type: ignore[arg-type]
        tenant=TenantContext(user.id),
        actor=actor,
        profile="hybrid",
        weights=ScoreWeights("test-v1", 0.6, 0.25, 0.15),
    )

    assert created == 5
    first_pass = [call for call in curator.calls if call[1] is False]
    llm_pass = [call for call in curator.calls if call[1] is True]
    assert len(first_pass) == 5
    assert len(llm_pass) == 1
    assert llm_pass[0][0] == "cloud"
    await db_session.flush()
    from app.db.models import Article
    from sqlalchemy import select

    articles = list(await db_session.scalars(select(Article).where(Article.user_id == user.id)))
    llm_titled = [article for article in articles if article.title.startswith("LLM ")]
    assert len(llm_titled) == 1
    assert index.upserted >= 6
