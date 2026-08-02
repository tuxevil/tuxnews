from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.archive.markdown import ArchiveMetadata, StoryContext, StoryMember, export_article
from app.archive.paths import ArchivePathError, AtomicArchiveWriter, tenant_relative_path
from app.audit.service import record_audit
from app.core.config import Settings, get_settings
from app.core.context import TenantContext
from app.db.models import ArchiveExport, Article, Cluster, ClusterMember, Feedback, Source
from app.observability import metrics


@dataclass(frozen=True)
class ArchiveArticleView:
    export: ArchiveExport
    article: Article
    source_name: str
    content_markdown: str


async def save_article_markdown(
    session: AsyncSession,
    *,
    tenant: TenantContext,
    article_id: int,
    settings: Settings | None = None,
    correlation_id: str | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> ArchiveExport | None:
    timer = metrics.timer("archive.save")
    try:
        export = await _save_article_markdown(
            session,
            tenant=tenant,
            article_id=article_id,
            settings=settings,
            correlation_id=correlation_id,
            actor_type=actor_type,
            actor_id=actor_id,
        )
    except Exception:
        timer.finish(success=False)
        raise
    timer.finish(success=True)
    return export


async def _save_article_markdown(
    session: AsyncSession,
    *,
    tenant: TenantContext,
    article_id: int,
    settings: Settings | None = None,
    correlation_id: str | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> ArchiveExport | None:
    user_id = tenant.tenant_id
    row = await session.execute(
        select(Article, Source.name, Source.url)
        .join(Source, Source.id == Article.source_id)
        .where(Article.id == article_id, Article.user_id == user_id, Source.user_id == user_id)
    )
    result = row.one_or_none()
    if result is None:
        return None
    article, source_name, source_url = result
    active_settings = settings or get_settings()
    writer = AtomicArchiveWriter(Path(active_settings.archive_root))
    try:
        feedback_rating = await session.scalar(
            select(Feedback.rating)
            .where(
                Feedback.user_id == user_id,
                Feedback.article_id == article_id,
                Feedback.action_type == "article",
                Feedback.is_current.is_(True),
            )
            .order_by(Feedback.updated_at.desc(), Feedback.id.desc())
            .limit(1)
        )
        cluster = await session.get(Cluster, article.cluster_id) if article.cluster_id is not None else None
        story: StoryContext | None = None
        if cluster is not None:
            member_rows = await session.execute(
                select(ClusterMember, Article, Source.name)
                .join(Article, Article.id == ClusterMember.article_id)
                .join(Source, Source.id == Article.source_id)
                .where(
                    ClusterMember.cluster_id == cluster.id,
                    ClusterMember.user_id == user_id,
                    ClusterMember.is_current.is_(True),
                    Article.user_id == user_id,
                )
                .order_by(Article.published_at.asc(), Article.id.asc())
            )
            members = tuple(
                StoryMember(
                    article_id=member_article.id,
                    title=member_article.title,
                    source_name=source_name_value,
                    url=member_article.url,
                    published_at=member_article.published_at,
                )
                for _member, member_article, source_name_value in member_rows.all()
            )
            story = StoryContext(
                cluster_id=cluster.id,
                title=cluster.title,
                summary=cluster.summary,
                members=members,
            )
        metadata = ArchiveMetadata(
            source_name=source_name,
            source_url=source_url,
            rating=feedback_rating,
            cluster=cluster.title if cluster is not None else None,
            cluster_id=cluster.id if cluster is not None else None,
            date_saved=datetime.now(UTC),
        )
        export = await export_article(
            session,
            article,
            metadata,
            writer,
            story=story,
        )
    except ArchivePathError as exc:
        raise ValueError("archive operation failed") from exc
    record_audit(
        session,
        user_id=user_id,
        action="archive.article.saved",
        resource_type="article",
        resource_id=str(article_id),
        outcome="success",
        correlation_id=correlation_id,
        actor_type=actor_type,
        actor_id=actor_id,
        details={"path": export.path, "checksum": export.checksum},
    )
    await session.commit()
    return export


async def latest_archive(
    session: AsyncSession,
    *,
    tenant: TenantContext,
    settings: Settings | None = None,
) -> ArchiveArticleView | None:
    user_id = tenant.tenant_id
    row = await session.execute(
        select(ArchiveExport, Article, Source.name)
        .join(Article, Article.id == ArchiveExport.article_id)
        .join(Source, Source.id == Article.source_id)
        .where(
            ArchiveExport.user_id == user_id,
            Article.user_id == user_id,
            Source.user_id == user_id,
            ArchiveExport.status == "ready",
        )
        .order_by(ArchiveExport.updated_at.desc(), ArchiveExport.id.desc())
        .limit(1)
    )
    record = row.one_or_none()
    if record is None:
        return None
    export, article, source_name = record
    writer = AtomicArchiveWriter(Path((settings or get_settings()).archive_root))
    try:
        content_markdown = writer.read_text(tenant_relative_path(user_id, Path(export.path)))
    except ArchivePathError as exc:
        raise ValueError("archive resource is unavailable") from exc
    return ArchiveArticleView(
        export=export,
        article=article,
        source_name=source_name,
        content_markdown=content_markdown,
    )
