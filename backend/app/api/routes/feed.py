from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import IdentityContext, ownership_filter, require_scope
from app.api.schemas import FeedItem, FeedResponse
from app.articles.lifecycle import ArticleStatus
from app.core.permissions import Scope
from app.db.models import Article, Source
from app.db.session import get_session
from app.feed.cursor import FeedCursor, InvalidCursor, decode_cursor, encode_cursor
from app.ranking.display import RankedArticle, load_ranking_context, rank_articles_for_display

router = APIRouter(prefix="/api/v1/feed", tags=["feed"])
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _feed_item(item: RankedArticle) -> FeedItem:
    article = item.article
    return FeedItem.model_validate(article).model_copy(
        update={
            "source_id": article.source_id,
            "source_name": item.source_name,
            "score_breakdown": item.score_breakdown,
            "display_rank": item.result.display_rank,
            "cluster_id": article.cluster_id,
        }
    )


def _published_at(article: Article) -> datetime:
    if article.published_at is None:
        return EPOCH
    if article.published_at.tzinfo is None:
        return article.published_at.replace(tzinfo=UTC)
    return article.published_at.astimezone(UTC)


@router.get("", response_model=FeedResponse)
async def get_feed(
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_READ.value)),
    session: AsyncSession = Depends(get_session),
    cursor: str | None = None,
    page_size: int = Query(default=20, ge=1, le=50),
    source_id: int | None = Query(default=None, ge=1),
    tag: str | None = Query(default=None, min_length=1, max_length=80),
    article_status: ArticleStatus = Query(default=ArticleStatus.PUBLISHED, alias="status"),
    cluster_id: int | None = Query(default=None, ge=1),
    published_after: datetime | None = None,
    published_before: datetime | None = None,
    min_score: float | None = Query(default=None, ge=0, le=1),
) -> FeedResponse:
    filters = [ownership_filter(Article, identity), Article.status == article_status.value]
    if source_id is not None:
        filters.append(Article.source_id == source_id)
    if tag is not None:
        filters.append(Article.tags.contains([tag]))
    if cluster_id is not None:
        filters.append(Article.cluster_id == cluster_id)
    if published_after is not None:
        filters.append(Article.published_at >= published_after.astimezone(UTC))
    if published_before is not None:
        filters.append(Article.published_at <= published_before.astimezone(UTC))
    if min_score is not None:
        filters.append(Article.relevance_score >= min_score)

    decoded = None
    if cursor is not None:
        try:
            decoded = decode_cursor(cursor)
        except InvalidCursor as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid cursor") from exc

    result = await session.execute(
        select(Article)
        .add_columns(Source.name)
        .join(Source, Source.id == Article.source_id)
        .where(*filters)
        .where(Source.user_id == identity.tenant.tenant_id)
        .where(Source.is_muted.is_(False))
        .order_by(Article.id)
    )
    rows = list(result)
    ranking_context = await load_ranking_context(session, identity.user.id)
    ranked = rank_articles_for_display(
        [(row[0], row[1]) for row in rows],
        context=ranking_context,
        serendipity=identity.user.serendipity_score,
    )
    if decoded is not None:
        ranked = [
            item
            for item in ranked
            if item.result.display_rank < decoded.score
            or (
                item.result.display_rank == decoded.score
                and (
                    _published_at(item.article) < decoded.published_at
                    or (
                        _published_at(item.article) == decoded.published_at
                        and (item.article.id or 0) < decoded.article_id
                    )
                )
            )
        ]
    page = ranked[: page_size + 1]
    has_next = len(page) > page_size
    page = page[:page_size]
    next_cursor = None
    if has_next and page:
        last = page[-1]
        next_cursor = encode_cursor(
            FeedCursor(
                score=last.result.display_rank,
                published_at=_published_at(last.article),
                article_id=last.article.id,
            )
        )
    response = FeedResponse(
        items=[_feed_item(item) for item in page],
        next_cursor=next_cursor,
    )
    return response
