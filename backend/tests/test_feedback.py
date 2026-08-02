import asyncio

import pytest
from app.core.context import TenantContext
from app.core.security import hash_password
from app.db.base import Base
from app.db.models import Article, Feedback, Source, User, UserTopic
from app.feedback.service import submit_feedback
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_feedback_is_append_only_idempotent_and_updates_only_target_component(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    user_factory,
    source_factory,
    article_factory,
) -> None:
    user = User(email="feedback-owner@example.com", password_hash=hash_password("feedback-password"), role="user")
    other = user_factory()
    db_session.add_all([user, other])
    await db_session.flush()
    source = source_factory(user.id)
    other_source = source_factory(other.id)
    db_session.add_all([source, other_source])
    await db_session.flush()
    article = article_factory(user.id, source.id)
    foreign_article = article_factory(other.id, other_source.id)
    db_session.add_all([article, foreign_article])
    await db_session.commit()

    login = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": "feedback-owner@example.com", "password": "feedback-password"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    payload = {"action_type": "article", "rating": "like", "article_id": article.id, "reason": "<b>useful</b>"}
    first = await auth_client.post("/api/v1/feedback", headers=headers, json=payload)
    repeated = await auth_client.post("/api/v1/feedback", headers=headers, json=payload)
    assert first.status_code == 201
    assert repeated.status_code == 201
    assert repeated.json()["id"] == first.json()["id"]
    assert repeated.json()["reason"] == "useful"
    current_feedback = await auth_client.get(
        f"/api/v1/feedback?article_id={article.id}",
        headers=headers,
    )
    assert current_feedback.status_code == 200
    assert [event["rating"] for event in current_feedback.json()] == ["like"]
    await db_session.refresh(article)
    assert article.feedback_version == 1

    changed = await auth_client.post(
        "/api/v1/feedback",
        headers=headers,
        json={"action_type": "article", "rating": "dislike", "article_id": article.id},
    )
    assert changed.status_code == 201
    assert changed.json()["supersedes_id"] == first.json()["id"]
    await db_session.refresh(article)
    assert article.score_breakdown["feedback_penalty"] == 1.0
    assert article.feedback_version == 2

    undone = await auth_client.post(f"/api/v1/feedback/{changed.json()['id']}/undo", headers=headers)
    assert undone.status_code == 200
    assert undone.json()["rating"] == "neutral"
    await db_session.refresh(article)
    assert article.score_breakdown["feedback_penalty"] == 0.0
    assert article.feedback_version == 3

    foreign = await auth_client.post(
        "/api/v1/feedback",
        headers=headers,
        json={"rating": "like", "article_id": foreign_article.id},
    )
    assert foreign.status_code == 404
    count = await db_session.scalar(select(func.count()).select_from(Feedback).where(Feedback.user_id == user.id))
    assert count == 3


@pytest.mark.asyncio
async def test_source_and_topic_feedback_do_not_cross_components(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    user_factory,
    source_factory,
    article_factory,
) -> None:
    user = User(email="component-owner@example.com", password_hash=hash_password("component-password"), role="user")
    db_session.add(user)
    await db_session.flush()
    source = source_factory(user.id)
    db_session.add(source)
    await db_session.flush()
    article = article_factory(user.id, source.id)
    db_session.add(article)
    await db_session.commit()
    login = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": "component-owner@example.com", "password": "component-password"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    topic_feedback = await auth_client.post(
        "/api/v1/feedback",
        headers=headers,
        json={"action_type": "topic", "rating": "dislike", "topic_name": "Linux"},
    )
    assert topic_feedback.status_code == 201
    topic = await db_session.scalar(
        select(UserTopic).where(UserTopic.user_id == user.id, UserTopic.topic_name == "linux")
    )
    assert topic is not None
    topic_weight = topic.weight_score
    topic_version = topic.preference_version

    source_feedback = await auth_client.post(
        "/api/v1/feedback",
        headers=headers,
        json={"action_type": "source", "rating": "like", "source_id": source.id},
    )
    assert source_feedback.status_code == 201
    await db_session.refresh(source)
    await db_session.refresh(topic)
    assert source.reputation_score > 0.5
    assert topic.weight_score == topic_weight
    assert topic.preference_version == topic_version

    quality_feedback = await auth_client.post(
        "/api/v1/feedback",
        headers=headers,
        json={"action_type": "quality", "rating": "dislike", "article_id": article.id},
    )
    assert quality_feedback.status_code == 201
    await db_session.refresh(topic)
    await db_session.refresh(article)
    assert topic.weight_score == topic_weight
    assert article.score_breakdown["quality_penalty"] == 1.0


@pytest.mark.asyncio
async def test_concurrent_same_mutation_keeps_one_current_event(
    tmp_path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'concurrent-feedback.db'}"
    engine = create_async_engine(database_url, connect_args={"timeout": 10})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as setup_session:
            user = User(
                email="concurrent-owner@example.com",
                password_hash=hash_password("concurrent-password"),
                role="user",
            )
            setup_session.add(user)
            await setup_session.flush()
            source = Source(
                user_id=user.id,
                name="Concurrent source",
                url="https://concurrent.example.test/feed",
            )
            setup_session.add(source)
            await setup_session.flush()
            article = Article(
                user_id=user.id,
                source_id=source.id,
                title="Concurrent article",
                original_title="Concurrent article",
                url="https://concurrent.example.test/article",
                canonical_url_hash="c" * 64,
            )
            setup_session.add(article)
            await setup_session.commit()
            user_id = user.id
            article_id = article.id

        async def submit() -> Feedback:
            async with session_factory() as session:
                return await submit_feedback(
                    session,
                    tenant=TenantContext(user_id),
                    action_type="article",
                    rating="like",
                    article_id=article_id,
                )

        events = await asyncio.gather(*(submit() for _ in range(3)))
        assert len({event.id for event in events}) == 1
        async with session_factory() as verify_session:
            current_count = await verify_session.scalar(
                select(func.count())
                .select_from(Feedback)
                .where(Feedback.user_id == user_id, Feedback.is_current.is_(True))
            )
        assert current_count == 1
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
