from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import record_audit
from app.clustering.domain import ClusterRules, ClusterStatus, cluster_status, evaluate_membership
from app.core.context import TenantContext
from app.db.models import Article, Cluster, ClusterMember, Source
from app.embeddings.qdrant_index import EmbeddingHit, EmbeddingIndex


@dataclass(frozen=True)
class ClusterAssignment:
    cluster_id: int
    member_id: int
    created_cluster: bool
    used_fallback: bool
    reason: str
    algorithm_version: str


@dataclass(frozen=True)
class ClusterItemView:
    article_id: int
    title: str
    url: str
    source_id: int
    source_name: str
    summary: str | None
    tags: list[str]
    published_at: datetime | None
    status: str
    similarity_score: float
    membership_reason: str


@dataclass(frozen=True)
class ClusterView:
    id: int
    title: str
    summary: str | None
    status: str
    curation_state: str
    algorithm_version: str
    window_start: datetime | None
    window_end: datetime | None
    reconciled_at: datetime | None
    item_count: int
    source_count: int
    items: list[ClusterItemView]


def _article_time(article: Article) -> datetime:
    value = article.published_at or article.discovered_at or article.created_at
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _current_member(
    session: AsyncSession,
    *,
    tenant_id: int,
    article_id: int,
    algorithm_version: str,
) -> ClusterMember | None:
    return await session.scalar(
        select(ClusterMember).where(
            ClusterMember.user_id == tenant_id,
            ClusterMember.article_id == article_id,
            ClusterMember.algorithm_version == algorithm_version,
            ClusterMember.is_current.is_(True),
        )
    )


async def _create_cluster(
    session: AsyncSession,
    article: Article,
    *,
    rules: ClusterRules,
    reason: str,
    similarity_score: float,
) -> ClusterAssignment:
    timestamp = _article_time(article)
    cluster = Cluster(
        user_id=article.user_id,
        title=article.title,
        summary=article.summary,
        algorithm_version=rules.algorithm_version,
        status=ClusterStatus.ACTIVE.value,
        window_start=timestamp,
        window_end=timestamp,
        reconciled_at=datetime.now(UTC),
    )
    session.add(cluster)
    await session.flush()
    member = ClusterMember(
        user_id=article.user_id,
        cluster_id=cluster.id,
        article_id=article.id,
        similarity_score=similarity_score,
        membership_reason=reason,
        algorithm_version=rules.algorithm_version,
        is_current=True,
    )
    session.add(member)
    article.cluster_id = cluster.id
    await session.flush()
    return ClusterAssignment(
        cluster_id=cluster.id,
        member_id=member.id,
        created_cluster=True,
        used_fallback=reason != "semantic_and_temporal_match",
        reason=reason,
        algorithm_version=rules.algorithm_version,
    )


async def _candidate_clusters(
    session: AsyncSession,
    *,
    article: Article,
    hits: Sequence[EmbeddingHit],
    rules: ClusterRules,
) -> list[tuple[float, Cluster]]:
    candidates: list[tuple[float, Cluster]] = []
    for hit in hits:
        if hit.article_id == article.id:
            continue
        if hit.payload.get("user_id") != article.user_id:
            continue
        row = await session.execute(
            select(Cluster, Article)
            .join(ClusterMember, ClusterMember.cluster_id == Cluster.id)
            .join(Article, Article.id == ClusterMember.article_id)
            .where(
                Cluster.user_id == article.user_id,
                ClusterMember.user_id == article.user_id,
                Cluster.algorithm_version == rules.algorithm_version,
                ClusterMember.article_id == hit.article_id,
                ClusterMember.is_current.is_(True),
                Article.user_id == article.user_id,
            )
            .order_by(Cluster.id)
        )
        for cluster, candidate_article in row:
            decision = evaluate_membership(
                article_time=_article_time(article),
                cluster_start=cluster.window_start or _article_time(candidate_article),
                cluster_end=cluster.window_end or _article_time(candidate_article),
                similarity_score=hit.score,
                rules=rules,
            )
            if decision.accepted:
                candidates.append((decision.similarity_score, cluster))
    return candidates


async def assign_article(
    session: AsyncSession,
    article: Article,
    *,
    tenant: TenantContext,
    vector: Sequence[float] | None = None,
    index: EmbeddingIndex | Any | None = None,
    rules: ClusterRules | None = None,
    correlation_id: str | None = None,
    actor_type: str = "job",
    actor_id: str | None = "assign_article",
) -> ClusterAssignment:
    if tenant.tenant_id != article.user_id:
        raise ValueError("article is outside the tenant context")
    rules = rules or ClusterRules()
    existing = await _current_member(
        session,
        tenant_id=tenant.tenant_id,
        article_id=article.id,
        algorithm_version=rules.algorithm_version,
    )
    if existing is not None:
        return ClusterAssignment(
            cluster_id=existing.cluster_id,
            member_id=existing.id,
            created_cluster=False,
            used_fallback=False,
            reason="existing_membership",
            algorithm_version=rules.algorithm_version,
        )

    used_fallback = False
    hits: tuple[EmbeddingHit, ...] = ()
    if index is not None and vector is not None:
        try:
            hits = await index.search(user_id=article.user_id, vector=vector, limit=20)
        except Exception:
            # Qdrant is reconstructible infrastructure; PostgreSQL still receives a standalone story.
            used_fallback = True

    candidates = await _candidate_clusters(session, article=article, hits=hits, rules=rules)
    if not candidates:
        reason = "qdrant_unavailable_standalone" if used_fallback else "standalone_no_matching_cluster"
        assignment = await _create_cluster(
            session,
            article,
            rules=rules,
            reason=reason,
            similarity_score=0.0,
        )
        assignment = ClusterAssignment(
            cluster_id=assignment.cluster_id,
            member_id=assignment.member_id,
            created_cluster=assignment.created_cluster,
            used_fallback=used_fallback or assignment.used_fallback,
            reason=assignment.reason,
            algorithm_version=assignment.algorithm_version,
        )
        record_audit(
            session,
            user_id=tenant.tenant_id,
            action="cluster.assignment.created",
            resource_type="cluster",
            resource_id=str(assignment.cluster_id),
            outcome="fallback" if assignment.used_fallback else "success",
            correlation_id=correlation_id,
            actor_type=actor_type,
            actor_id=actor_id,
            details={
                "member_id": assignment.member_id,
                "reason": assignment.reason,
                "algorithm_version": assignment.algorithm_version,
            },
        )
        await session.commit()
        return assignment

    similarity, cluster = max(candidates, key=lambda candidate: (candidate[0], -candidate[1].id))
    member = ClusterMember(
        user_id=tenant.tenant_id,
        cluster_id=cluster.id,
        article_id=article.id,
        similarity_score=similarity,
        membership_reason="semantic_and_temporal_match",
        algorithm_version=rules.algorithm_version,
        is_current=True,
    )
    session.add(member)
    timestamp = _article_time(article)
    cluster.window_start = min(_utc(cluster.window_start) if cluster.window_start else timestamp, timestamp)
    cluster.window_end = max(_utc(cluster.window_end) if cluster.window_end else timestamp, timestamp)
    cluster.status = ClusterStatus.ACTIVE.value
    cluster.reconciled_at = datetime.now(UTC)
    article.cluster_id = cluster.id
    await session.flush()
    record_audit(
        session,
        user_id=tenant.tenant_id,
        action="cluster.assignment.added",
        resource_type="cluster",
        resource_id=str(cluster.id),
        outcome="fallback" if used_fallback else "success",
        correlation_id=correlation_id,
        actor_type=actor_type,
        actor_id=actor_id,
        details={
            "member_id": member.id,
            "article_id": article.id,
            "similarity_score": similarity,
            "algorithm_version": rules.algorithm_version,
        },
    )
    await session.commit()
    await session.refresh(member)
    return ClusterAssignment(
        cluster_id=cluster.id,
        member_id=member.id,
        created_cluster=False,
        used_fallback=used_fallback,
        reason="semantic_and_temporal_match",
        algorithm_version=rules.algorithm_version,
    )


async def reconcile_cluster(
    session: AsyncSession,
    cluster_id: int,
    *,
    tenant: TenantContext,
    rules: ClusterRules | None = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
    actor_type: str = "job",
    actor_id: str | None = "reconcile_cluster",
) -> ClusterStatus | None:
    rules = rules or ClusterRules()
    cluster = await session.scalar(
        select(Cluster).where(
            Cluster.id == cluster_id,
            Cluster.user_id == tenant.tenant_id,
            Cluster.algorithm_version == rules.algorithm_version,
        )
    )
    if cluster is None:
        return None
    members = list(
        await session.scalars(
            select(ClusterMember).where(
                ClusterMember.user_id == tenant.tenant_id,
                ClusterMember.cluster_id == cluster_id,
                ClusterMember.algorithm_version == rules.algorithm_version,
                ClusterMember.is_current.is_(True),
            )
        )
    )
    article_ids = [member.article_id for member in members]
    articles = (
        list(
            await session.scalars(
                select(Article).where(Article.id.in_(article_ids), Article.user_id == tenant.tenant_id)
            )
        )
        if article_ids
        else []
    )
    event_times = [_article_time(article) for article in articles]
    if event_times:
        cluster.window_start = min(event_times)
        cluster.window_end = max(event_times)
    status = cluster_status(
        member_count=len(members),
        has_ambiguity=False,
        last_event=max((_article_time(article) for article in articles), default=None),
        now=now or datetime.now(UTC),
    )
    cluster.status = status.value
    cluster.reconciled_at = datetime.now(UTC)
    await session.flush()
    record_audit(
        session,
        user_id=tenant.tenant_id,
        action="cluster.reconciled",
        resource_type="cluster",
        resource_id=str(cluster.id),
        outcome="success",
        correlation_id=correlation_id,
        actor_type=actor_type,
        actor_id=actor_id,
        details={"status": status.value, "member_count": len(members)},
    )
    await session.commit()
    return status


async def list_cluster_views(
    session: AsyncSession,
    *,
    tenant: TenantContext,
    cluster_id: int | None = None,
) -> list[ClusterView]:
    cluster_filters = [Cluster.user_id == tenant.tenant_id]
    if cluster_id is not None:
        cluster_filters.append(Cluster.id == cluster_id)
    clusters = list(
        await session.scalars(
            select(Cluster)
            .where(*cluster_filters)
            .order_by(Cluster.window_end.desc().nullslast(), Cluster.id.desc())
        )
    )
    if not clusters:
        return []

    cluster_ids = [cluster.id for cluster in clusters]
    memberships = await session.execute(
        select(ClusterMember, Article, Source)
        .join(Cluster, Cluster.id == ClusterMember.cluster_id)
        .join(Article, Article.id == ClusterMember.article_id)
        .join(Source, Source.id == Article.source_id)
        .where(
            ClusterMember.cluster_id.in_(cluster_ids),
            ClusterMember.user_id == tenant.tenant_id,
            ClusterMember.is_current.is_(True),
            Article.user_id == Cluster.user_id,
            Source.user_id == Cluster.user_id,
            Article.status == "published",
        )
    )
    grouped: dict[int, list[ClusterItemView]] = {cluster_id: [] for cluster_id in cluster_ids}
    for member, article, source in memberships:
        grouped[member.cluster_id].append(
            ClusterItemView(
                article_id=article.id,
                title=article.title,
                url=article.url,
                source_id=source.id,
                source_name=source.name,
                summary=article.summary,
                tags=list(article.tags),
                published_at=article.published_at,
                status=article.status,
                similarity_score=member.similarity_score,
                membership_reason=member.membership_reason,
            )
        )

    views: list[ClusterView] = []
    for cluster in clusters:
        items = sorted(
            grouped[cluster.id],
            key=lambda item: (
                item.published_at is None,
                _utc(item.published_at) if item.published_at else datetime.max.replace(tzinfo=UTC),
                item.article_id,
            ),
        )
        if not items:
            curation_state = "empty"
        elif cluster.status == ClusterStatus.AMBIGUOUS.value:
            curation_state = "recalculating"
        elif any(item.summary is None for item in items):
            curation_state = "partial"
        else:
            curation_state = "ready"
        views.append(
            ClusterView(
                id=cluster.id,
                title=cluster.title,
                summary=cluster.summary,
                status=cluster.status,
                curation_state=curation_state,
                algorithm_version=cluster.algorithm_version,
                window_start=cluster.window_start,
                window_end=cluster.window_end,
                reconciled_at=cluster.reconciled_at,
                item_count=len(items),
                source_count=len({item.source_id for item in items}),
                items=items,
            )
        )
    return views
