from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from app.archive.service import ArchiveArticleView, latest_archive, save_article_markdown
from app.briefings.service import BriefingView, get_today_briefing_view
from app.core.config import Settings
from app.core.context import TenantContext
from app.db.models import ArchiveExport, Article, Feedback, Source
from app.db.session import SessionFactory
from app.feedback.service import Rating, submit_feedback
from app.sources.service import add_rss_source


async def get_daily_briefing(tenant: TenantContext, *, timezone_name: str) -> BriefingView | None:
    async with SessionFactory() as session:
        return await get_today_briefing_view(session, user_id=tenant.tenant_id, timezone_name=timezone_name)


async def save_article(
    tenant: TenantContext,
    *,
    article_id: int,
    settings: Settings | None = None,
    correlation_id: str | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> ArchiveExport | None:
    async with SessionFactory() as session:
        return await save_article_markdown(
            session,
            tenant=tenant,
            article_id=article_id,
            settings=settings,
            correlation_id=correlation_id,
            actor_type=actor_type,
            actor_id=actor_id,
        )


async def add_source(
    tenant: TenantContext,
    *,
    name: str,
    url: str,
    tags: Sequence[str],
    settings: Settings | None = None,
    correlation_id: str | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> tuple[Source, bool]:
    async with SessionFactory() as session:
        return await add_rss_source(
            session,
            tenant=tenant,
            name=name,
            url=url,
            tags=tags,
            settings=settings,
            correlation_id=correlation_id,
            actor_type=actor_type,
            actor_id=actor_id,
        )


async def rate_article(
    tenant: TenantContext,
    *,
    article_id: int,
    rating: Rating,
    correlation_id: str | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> Feedback | None:
    async with SessionFactory() as session:
        article = await session.scalar(
            select(Article).where(Article.id == article_id, Article.user_id == tenant.tenant_id)
        )
        if article is None:
            return None
        return await submit_feedback(
            session,
            tenant=tenant,
            action_type="article",
            rating=rating,
            article_id=article_id,
            correlation_id=correlation_id,
            actor_type=actor_type,
            actor_id=actor_id,
        )


async def get_latest_archive(tenant: TenantContext, *, settings: Settings | None = None) -> ArchiveArticleView | None:
    async with SessionFactory() as session:
        return await latest_archive(session, tenant=tenant, settings=settings)
