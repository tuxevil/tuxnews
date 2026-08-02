from datetime import UTC, datetime, timedelta

import pytest
from app.clustering.service import assign_article, reconcile_cluster
from app.core.context import TenantContext
from app.db.models import Cluster, ClusterMember
from app.embeddings.qdrant_index import EmbeddingHit
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


class FakeIndex:
    def __init__(self, hits: tuple[EmbeddingHit, ...] = (), *, fail: bool = False) -> None:
        self.hits = hits
        self.fail = fail

    async def search(self, **_: object) -> tuple[EmbeddingHit, ...]:
        if self.fail:
            raise RuntimeError("qdrant unavailable")
        return self.hits


@pytest.mark.asyncio
async def test_incremental_assignment_is_tenant_scoped_and_idempotent(
    db_session: AsyncSession,
    user_factory,
    source_factory,
    article_factory,
) -> None:
    owner = user_factory()
    other = user_factory()
    db_session.add_all([owner, other])
    await db_session.flush()
    source = source_factory(owner.id)
    other_source = source_factory(other.id)
    db_session.add_all([source, other_source])
    await db_session.flush()
    first = article_factory(owner.id, source.id)
    first.published_at = datetime(2026, 8, 1, tzinfo=UTC)
    second = article_factory(owner.id, source.id)
    second.published_at = datetime(2026, 8, 2, tzinfo=UTC)
    foreign = article_factory(other.id, other_source.id)
    foreign.published_at = datetime(2026, 8, 1, tzinfo=UTC)
    db_session.add_all([first, second, foreign])
    await db_session.commit()

    first_assignment = await assign_article(db_session, first, tenant=TenantContext(owner.id))
    with pytest.raises(ValueError, match="outside the tenant"):
        await assign_article(db_session, first, tenant=TenantContext(other.id))
    second_assignment = await assign_article(
        db_session,
        second,
        tenant=TenantContext(owner.id),
        vector=[0.1],
        index=FakeIndex((EmbeddingHit(first.id, 0.91, {"user_id": owner.id}),)),
    )
    repeated = await assign_article(
        db_session,
        second,
        tenant=TenantContext(owner.id),
        vector=[0.1],
        index=FakeIndex((EmbeddingHit(first.id, 0.91, {"user_id": owner.id}),)),
    )
    foreign_assignment = await assign_article(
        db_session,
        foreign,
        tenant=TenantContext(other.id),
        vector=[0.1],
        index=FakeIndex((EmbeddingHit(first.id, 0.99, {"user_id": owner.id}),)),
    )

    assert second_assignment.cluster_id == first_assignment.cluster_id
    assert repeated.member_id == second_assignment.member_id
    assert foreign_assignment.cluster_id != first_assignment.cluster_id
    member_count = await db_session.scalar(select(func.count()).select_from(ClusterMember))
    assert member_count == 3


@pytest.mark.asyncio
async def test_qdrant_failure_creates_standalone_and_reconciliation_is_stable(
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
    article.published_at = datetime.now(UTC) - timedelta(days=4)
    db_session.add(article)
    await db_session.commit()

    assignment = await assign_article(
        db_session,
        article,
        tenant=TenantContext(user.id),
        vector=[0.1],
        index=FakeIndex(fail=True),
    )
    status = await reconcile_cluster(
        db_session,
        assignment.cluster_id,
        tenant=TenantContext(user.id),
        now=datetime.now(UTC),
    )
    cluster = await db_session.scalar(select(Cluster).where(Cluster.id == assignment.cluster_id))

    assert assignment.used_fallback is True
    assert status is not None
    assert cluster is not None and cluster.status == "stale"


@pytest.mark.asyncio
async def test_late_article_reprocessing_keeps_membership_and_status_reproducible(
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
    first = article_factory(user.id, source.id)
    first.published_at = datetime(2026, 8, 1, tzinfo=UTC)
    late = article_factory(user.id, source.id)
    late.published_at = datetime(2026, 8, 3, tzinfo=UTC)
    db_session.add_all([first, late])
    await db_session.commit()

    first_assignment = await assign_article(db_session, first, tenant=TenantContext(user.id))
    late_assignment = await assign_article(
        db_session,
        late,
        tenant=TenantContext(user.id),
        vector=[0.1],
        index=FakeIndex((EmbeddingHit(first.id, 0.78, {"user_id": user.id}),)),
    )
    repeated = await assign_article(
        db_session,
        late,
        tenant=TenantContext(user.id),
        vector=[0.1],
        index=FakeIndex((EmbeddingHit(first.id, 0.78, {"user_id": user.id}),)),
    )
    status = await reconcile_cluster(
        db_session,
        late_assignment.cluster_id,
        tenant=TenantContext(user.id),
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert late_assignment.cluster_id == first_assignment.cluster_id
    assert repeated.cluster_id == late_assignment.cluster_id
    assert repeated.member_id == late_assignment.member_id
    assert repeated.reason == "existing_membership"
    assert status is not None
