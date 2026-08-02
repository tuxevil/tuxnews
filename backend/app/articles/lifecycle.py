from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select

from app.db.models import Article
from app.db.session import SessionFactory


async def publish_extracted_articles(user_id: int) -> int:
    """Dev helper: fast-forward extracted articles to published for one tenant."""

    async with SessionFactory() as session:
        articles = list(
            await session.scalars(
                select(Article).where(
                    Article.user_id == user_id,
                    Article.status == ArticleStatus.EXTRACTED.value,
                )
            )
        )
        published = 0
        for article in articles:
            try:
                transition_article(article, ArticleStatus.CURATED)
                transition_article(article, ArticleStatus.INDEXED)
                transition_article(article, ArticleStatus.PUBLISHED)
            except InvalidArticleTransition:
                continue
            published += 1
        await session.commit()
        return published


class ArticleStatus(StrEnum):
    DISCOVERED = "discovered"
    FETCHING = "fetching"
    EXTRACTED = "extracted"
    CURATED = "curated"
    INDEXED = "indexed"
    PUBLISHED = "published"
    FAILED = "failed"


class InvalidArticleTransition(ValueError):
    """Raised when an article attempts to skip or reverse a lifecycle stage."""


VALID_TRANSITIONS: dict[ArticleStatus, frozenset[ArticleStatus]] = {
    ArticleStatus.DISCOVERED: frozenset({ArticleStatus.FETCHING, ArticleStatus.FAILED}),
    ArticleStatus.FETCHING: frozenset({ArticleStatus.EXTRACTED, ArticleStatus.FAILED}),
    ArticleStatus.EXTRACTED: frozenset({ArticleStatus.CURATED, ArticleStatus.FAILED}),
    ArticleStatus.CURATED: frozenset({ArticleStatus.INDEXED, ArticleStatus.FAILED}),
    ArticleStatus.INDEXED: frozenset({ArticleStatus.PUBLISHED, ArticleStatus.FAILED}),
    ArticleStatus.PUBLISHED: frozenset(),
    ArticleStatus.FAILED: frozenset({ArticleStatus.FETCHING}),
}


def transition_article(
    article: Article,
    target: ArticleStatus | str,
    *,
    error: str | None = None,
    now: datetime | None = None,
) -> Article:
    try:
        current = ArticleStatus(article.status)
        target_status = ArticleStatus(target)
    except ValueError as exc:
        raise InvalidArticleTransition("unknown article lifecycle state") from exc

    if current == target_status:
        if target_status == ArticleStatus.FAILED and error:
            article.status_error = error[:1000]
        return article
    if target_status not in VALID_TRANSITIONS[current]:
        raise InvalidArticleTransition(f"cannot transition article from {current} to {target_status}")

    transition_time = now or datetime.now(UTC)
    article.status = target_status.value
    if target_status == ArticleStatus.FETCHING:
        article.fetch_started_at = transition_time
        article.status_error = None
    elif target_status == ArticleStatus.EXTRACTED:
        article.extracted_at = transition_time
        article.status_error = None
    elif target_status == ArticleStatus.CURATED:
        article.curated_at = transition_time
        article.status_error = None
    elif target_status == ArticleStatus.INDEXED:
        article.indexed_at = transition_time
        article.status_error = None
    elif target_status == ArticleStatus.PUBLISHED:
        article.published_stage_at = transition_time
        article.status_error = None
    elif target_status == ArticleStatus.FAILED:
        article.status_error = (error or "article processing failed")[:1000]
    return article
