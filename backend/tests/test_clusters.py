from datetime import UTC, datetime, timedelta

import pytest
from app.db.models import Cluster, ClusterMember, Source
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_cluster_views_share_membership_and_respect_tenant_ownership(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    user_factory,
    source_factory,
    article_factory,
) -> None:
    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "clusters-owner@example.com", "password": "correct horse battery staple"},
    )
    user_id = registered.json()["user"]["id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    source = Source(
        user_id=user_id,
        name="Cluster source",
        url="https://clusters.example.test/feed",
        origin="dynamic",
    )
    db_session.add(source)
    await db_session.flush()
    start = datetime(2026, 8, 1, tzinfo=UTC)
    cluster = Cluster(
        user_id=user_id,
        title="Kernel release story",
        summary="A connected set of release reports.",
        status="active",
        algorithm_version="story-v1",
        window_start=start,
        window_end=start + timedelta(days=1),
    )
    empty = Cluster(user_id=user_id, title="Waiting for articles", status="active", algorithm_version="story-v1")
    db_session.add_all([cluster, empty])
    await db_session.flush()
    first = article_factory(user_id, source.id)
    first.status = "published"
    first.summary = "The first report."
    first.published_at = start
    first.cluster_id = cluster.id
    second = article_factory(user_id, source.id)
    second.status = "published"
    second.summary = None
    second.published_at = start + timedelta(hours=2)
    second.cluster_id = cluster.id
    db_session.add_all([first, second])
    await db_session.flush()
    db_session.add_all(
        [
                ClusterMember(
                    user_id=user_id,
                    cluster_id=cluster.id,
                article_id=first.id,
                similarity_score=0.92,
                membership_reason="semantic_and_temporal_match",
                algorithm_version="story-v1",
            ),
                ClusterMember(
                    user_id=user_id,
                    cluster_id=cluster.id,
                article_id=second.id,
                similarity_score=0.81,
                membership_reason="semantic_and_temporal_match",
                algorithm_version="story-v1",
            ),
        ]
    )
    foreign_user = user_factory()
    db_session.add(foreign_user)
    await db_session.flush()
    foreign_source = source_factory(foreign_user.id)
    db_session.add(foreign_source)
    await db_session.flush()
    foreign_article = article_factory(foreign_user.id, foreign_source.id)
    foreign_article.status = "published"
    foreign_cluster = Cluster(
        user_id=foreign_user.id,
        title="Foreign story",
        status="active",
        algorithm_version="story-v1",
    )
    db_session.add(foreign_cluster)
    await db_session.flush()
    foreign_article.cluster_id = foreign_cluster.id
    db_session.add(foreign_article)
    await db_session.flush()
    db_session.add(
        ClusterMember(
            user_id=foreign_user.id,
            cluster_id=foreign_cluster.id,
            article_id=foreign_article.id,
            similarity_score=0.99,
            membership_reason="semantic_and_temporal_match",
            algorithm_version="story-v1",
        )
    )
    await db_session.commit()

    response = await auth_client.get("/api/v1/clusters", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert {item["id"] for item in payload} == {empty.id, cluster.id}
    story = next(item for item in payload if item["id"] == cluster.id)
    assert story["curation_state"] == "partial"
    assert story["item_count"] == 2
    assert story["source_count"] == 1
    assert [item["article_id"] for item in story["items"]] == [first.id, second.id]
    assert story["items"][0]["similarity_score"] == 0.92
    assert story["items"][0]["source_name"] == source.name
    empty_view = next(item for item in payload if item["id"] == empty.id)
    assert empty_view["curation_state"] == "empty"

    detail = await auth_client.get(f"/api/v1/clusters/{cluster.id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json() == story
    hidden = await auth_client.get(f"/api/v1/clusters/{foreign_cluster.id}", headers=headers)
    assert hidden.status_code == 404


@pytest.mark.asyncio
async def test_ambiguous_cluster_is_exposed_as_recalculating(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    article_factory,
) -> None:
    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "clusters-state@example.com", "password": "correct horse battery staple"},
    )
    user_id = registered.json()["user"]["id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    source = Source(
        user_id=user_id,
        name="State source",
        url="https://clusters-state.example.test/feed",
        origin="dynamic",
    )
    db_session.add(source)
    await db_session.flush()
    cluster = Cluster(
        user_id=user_id,
        title="Ambiguous story",
        status="ambiguous",
        algorithm_version="story-v1",
    )
    db_session.add(cluster)
    await db_session.flush()
    article = article_factory(user_id, source.id)
    article.status = "published"
    article.summary = "A curated summary."
    article.cluster_id = cluster.id
    db_session.add(article)
    await db_session.flush()
    db_session.add(
            ClusterMember(
                user_id=user_id,
                cluster_id=cluster.id,
            article_id=article.id,
            similarity_score=0.8,
            membership_reason="semantic_and_temporal_match",
            algorithm_version="story-v1",
        )
    )
    await db_session.commit()

    response = await auth_client.get(f"/api/v1/clusters/{cluster.id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["curation_state"] == "recalculating"
