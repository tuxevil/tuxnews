from datetime import UTC, datetime

import pytest
from app.articles.lifecycle import ArticleStatus, InvalidArticleTransition, transition_article
from app.db.models import Article
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_article_transitions_record_stage_timestamps(
    db_session: AsyncSession,
    user_factory,
    source_factory,
    article_factory,
) -> None:
    user = user_factory()
    db_session.add(user)
    await db_session.flush()
    source = source_factory(user.id)
    db_session.add(source)
    await db_session.flush()
    article = article_factory(user.id, source.id)
    db_session.add(article)
    await db_session.flush()
    transition_time = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    transition_article(article, ArticleStatus.FETCHING, now=transition_time)
    transition_article(article, ArticleStatus.EXTRACTED, now=transition_time)
    transition_article(article, ArticleStatus.CURATED, now=transition_time)
    transition_article(article, ArticleStatus.INDEXED, now=transition_time)
    transition_article(article, ArticleStatus.PUBLISHED, now=transition_time)
    await db_session.commit()

    assert article.status == ArticleStatus.PUBLISHED
    assert article.discovered_at.tzinfo is not None
    assert article.fetch_started_at == transition_time
    assert article.extracted_at == transition_time
    assert article.curated_at == transition_time
    assert article.indexed_at == transition_time
    assert article.published_stage_at == transition_time


def test_invalid_transition_does_not_mutate_article() -> None:
    article = Article(
        user_id=1,
        source_id=1,
        title="Article",
        original_title="Article",
        url="https://example.com/article",
        canonical_url_hash="a" * 64,
        status=ArticleStatus.DISCOVERED.value,
    )

    with pytest.raises(InvalidArticleTransition):
        transition_article(article, ArticleStatus.PUBLISHED)
    assert article.status == ArticleStatus.DISCOVERED
    assert article.published_stage_at is None


def test_failed_article_can_retry_and_error_is_bounded() -> None:
    article = Article(
        user_id=1,
        source_id=1,
        title="Article",
        original_title="Article",
        url="https://example.com/article",
        canonical_url_hash="b" * 64,
        status=ArticleStatus.DISCOVERED.value,
    )
    transition_article(article, ArticleStatus.FAILED, error="x" * 2000)
    assert article.status == ArticleStatus.FAILED
    assert article.status_error == "x" * 1000
    transition_article(article, ArticleStatus.FETCHING)
    assert article.status == ArticleStatus.FETCHING
    assert article.status_error is None


@pytest.mark.asyncio
async def test_database_constraint_rejects_unknown_article_status(
    db_session: AsyncSession,
    user_factory,
    source_factory,
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
        title="Invalid state",
        original_title="Invalid state",
        url="https://example.com/invalid-state",
        canonical_url_hash="c" * 64,
        status="not-a-state",
    )
    db_session.add(article)
    with pytest.raises(IntegrityError):
        await db_session.flush()
