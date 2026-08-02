from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import record_audit
from app.clustering.domain import (
    ClusterRules,
    ClusterStatus,
    cluster_status,
    evaluate_membership,
    should_merge_clusters,
)
from app.core.context import TenantContext
from app.db.models import Article, Cluster, ClusterMember, Source
from app.embeddings.qdrant_index import EmbeddingHit, EmbeddingIndex

EmbedFunction = Callable[[str], Awaitable[Sequence[float]]]


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


async def _cluster_articles(
    session: AsyncSession,
    cluster: Cluster,
    *,
    tenant: TenantContext,
) -> list[Article]:
    return list(
        await session.scalars(
            select(Article)
            .join(ClusterMember, ClusterMember.article_id == Article.id)
            .where(
                ClusterMember.cluster_id == cluster.id,
                ClusterMember.user_id == tenant.tenant_id,
                ClusterMember.is_current.is_(True),
                Article.user_id == tenant.tenant_id,
            )
        )
    )


async def _safe_embed(embed: EmbedFunction | None, article: Article) -> Sequence[float] | None:
    if embed is None:
        return None
    try:
        return await embed(article.content_clean or article.summary or "")
    except Exception:
        return None


async def _safe_search(
    index: EmbeddingIndex | Any | None,
    tenant: TenantContext,
    vector: Sequence[float],
) -> tuple[EmbeddingHit, ...]:
    if index is None:
        return ()
    try:
        return await index.search(user_id=tenant.tenant_id, vector=vector, limit=10)
    except Exception:
        return ()


async def _cross_similarity(
    session: AsyncSession,
    left: Cluster,
    right: Cluster,
    *,
    tenant: TenantContext,
    index: EmbeddingIndex | Any | None,
    embed: EmbedFunction | None,
    sample_size: int = 3,
) -> float | None:
    left_articles = (await _cluster_articles(session, left, tenant=tenant))[:sample_size]
    right_articles = (await _cluster_articles(session, right, tenant=tenant))[:sample_size]
    left_ids = {article.id for article in left_articles}
    right_ids = {article.id for article in right_articles}
    if not left_ids or not right_ids:
        return None
    left_best = 0.0
    right_best = 0.0
    for article in left_articles:
        vector = await _safe_embed(embed, article)
        if vector is None:
            continue
        for hit in await _safe_search(index, tenant, vector):
            if hit.article_id in right_ids:
                left_best = max(left_best, hit.score)
    for article in right_articles:
        vector = await _safe_embed(embed, article)
        if vector is None:
            continue
        for hit in await _safe_search(index, tenant, vector):
            if hit.article_id in left_ids:
                right_best = max(right_best, hit.score)
    if left_best == 0.0 or right_best == 0.0:
        return None
    return min(left_best, right_best)


async def _merge_clusters(
    session: AsyncSession,
    keep: Cluster,
    absorb: Cluster,
    *,
    tenant: TenantContext,
    rules: ClusterRules,
    now: datetime,
) -> int:
    members = list(
        await session.scalars(
            select(ClusterMember).where(
                ClusterMember.cluster_id == absorb.id,
                ClusterMember.user_id == tenant.tenant_id,
                ClusterMember.is_current.is_(True),
            )
        )
    )
    moved = 0
    for member in members:
        existing = await _current_member(
            session,
            tenant_id=tenant.tenant_id,
            article_id=member.article_id,
            algorithm_version=rules.algorithm_version,
        )
        member.is_current = False
        article = await session.scalar(
            select(Article).where(
                Article.id == member.article_id,
                Article.user_id == tenant.tenant_id,
            )
        )
        if existing is not None and existing.cluster_id == keep.id:
            existing.similarity_score = max(existing.similarity_score, member.similarity_score)
            if article is not None:
                article.cluster_id = keep.id
            continue
        session.add(
            ClusterMember(
                user_id=tenant.tenant_id,
                cluster_id=keep.id,
                article_id=member.article_id,
                similarity_score=member.similarity_score,
                membership_reason="merged_cluster",
                algorithm_version=rules.algorithm_version,
                is_current=True,
            )
        )
        if article is not None:
            article.cluster_id = keep.id
        moved += 1
    if absorb.window_start is not None and (
        keep.window_start is None or _utc(absorb.window_start) < _utc(keep.window_start)
    ):
        keep.window_start = absorb.window_start
    if absorb.window_end is not None and (
        keep.window_end is None or _utc(absorb.window_end) > _utc(keep.window_end)
    ):
        keep.window_end = absorb.window_end
    keep.status = ClusterStatus.ACTIVE.value
    keep.reconciled_at = now
    absorb.status = ClusterStatus.EMPTY.value
    absorb.reconciled_at = now
    return moved


async def _merge_similar_clusters(
    session: AsyncSession,
    cluster: Cluster,
    *,
    tenant: TenantContext,
    rules: ClusterRules,
    index: EmbeddingIndex | Any | None,
    embed: EmbedFunction | None,
    now: datetime,
    max_merges: int = 5,
) -> int:
    merged = 0
    window = timedelta(hours=rules.window_hours)
    for _ in range(max_merges):
        candidates: list[tuple[float, Cluster]] = []
        others = list(
            await session.scalars(
                select(Cluster).where(
                    Cluster.user_id == tenant.tenant_id,
                    Cluster.id != cluster.id,
                    Cluster.algorithm_version == rules.algorithm_version,
                    Cluster.status.in_([ClusterStatus.ACTIVE.value, ClusterStatus.STALE.value]),
                )
            )
        )
        for other in others:
            if cluster.window_start is None or other.window_start is None:
                continue
            if abs(_utc(other.window_start) - _utc(cluster.window_start)) > window:
                continue
            similarity = await _cross_similarity(
                session,
                cluster,
                other,
                tenant=tenant,
                index=index,
                embed=embed,
            )
            if similarity is not None and should_merge_clusters(similarity, similarity, rules):
                candidates.append((similarity, other))
        if not candidates:
            break
        _, best = max(candidates, key=lambda candidate: (candidate[0], -candidate[1].id))
        if len(await _cluster_articles(session, best, tenant=tenant)) > len(
            await _cluster_articles(session, cluster, tenant=tenant)
        ):
            keep, absorb = best, cluster
            moved = await _merge_clusters(session, keep, absorb, tenant=tenant, rules=rules, now=now)
            await session.flush()
            record_audit(
                session,
                user_id=tenant.tenant_id,
                action="cluster.merged",
                resource_type="cluster",
                resource_id=str(absorb.id),
                outcome="success",
                actor_type="job",
                actor_id="reconcile_cluster",
                details={"absorbed_into": keep.id, "moved_members": moved},
            )
            await session.commit()
            return merged + 1
        moved = await _merge_clusters(session, cluster, best, tenant=tenant, rules=rules, now=now)
        await session.flush()
        record_audit(
            session,
            user_id=tenant.tenant_id,
            action="cluster.merged",
            resource_type="cluster",
            resource_id=str(best.id),
            outcome="success",
            actor_type="job",
            actor_id="reconcile_cluster",
            details={"absorbed_into": cluster.id, "moved_members": moved},
        )
        await session.commit()
        merged += 1
    return merged


async def _reclaim_unassigned_articles(
    session: AsyncSession,
    *,
    tenant: TenantContext,
    rules: ClusterRules,
    index: EmbeddingIndex | Any | None,
    embed: EmbedFunction | None,
    limit: int = 20,
) -> int:
    unassigned = list(
        await session.scalars(
            select(Article)
            .where(
                Article.user_id == tenant.tenant_id,
                Article.status == "published",
                Article.cluster_id.is_(None),
            )
            .order_by(Article.id)
            .limit(limit)
        )
    )
    reclaimed = 0
    for article in unassigned:
        vector = await _safe_embed(embed, article)
        if vector is None:
            continue
        await assign_article(session, article, tenant=tenant, vector=vector, index=index, rules=rules)
        reclaimed += 1
    return reclaimed


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
    index: EmbeddingIndex | Any | None = None,
    embed: EmbedFunction | None = None,
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
    articles_by_id = {article.id: article for article in articles}
    pruned = 0
    for member in members:
        article = articles_by_id.get(member.article_id)
        if article is None:
            continue
        decision = evaluate_membership(
            article_time=_article_time(article),
            cluster_start=cluster.window_start or _article_time(article),
            cluster_end=cluster.window_end or _article_time(article),
            similarity_score=member.similarity_score,
            rules=rules,
        )
        if decision.reason == "outside_temporal_window":
            member.is_current = False
            if article.cluster_id == cluster.id:
                article.cluster_id = None
            pruned += 1
    now_value = now or datetime.now(UTC)
    merged = 0
    reclaimed = 0
    if index is not None or embed is not None:
        merged = await _merge_similar_clusters(
            session,
            cluster,
            tenant=tenant,
            rules=rules,
            index=index,
            embed=embed,
            now=now_value,
        )
        reclaimed = await _reclaim_unassigned_articles(
            session,
            tenant=tenant,
            rules=rules,
            index=index,
            embed=embed,
        )
    remaining_members = list(
        await session.scalars(
            select(ClusterMember).where(
                ClusterMember.user_id == tenant.tenant_id,
                ClusterMember.cluster_id == cluster_id,
                ClusterMember.algorithm_version == rules.algorithm_version,
                ClusterMember.is_current.is_(True),
            )
        )
    )
    remaining_ids = [member.article_id for member in remaining_members]
    remaining_articles = (
        list(
            await session.scalars(
                select(Article).where(Article.id.in_(remaining_ids), Article.user_id == tenant.tenant_id)
            )
        )
        if remaining_ids
        else []
    )
    event_times = [_article_time(article) for article in remaining_articles]
    if event_times:
        cluster.window_start = min(event_times)
        cluster.window_end = max(event_times)
    status = cluster_status(
        member_count=len(remaining_members),
        has_ambiguity=False,
        last_event=max((_article_time(article) for article in remaining_articles), default=None),
        now=now_value,
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
        details={
            "status": status.value,
            "member_count": len(remaining_members),
            "pruned": pruned,
            "merged": merged,
            "reclaimed": reclaimed,
        },
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
