from datetime import UTC, datetime, timedelta

import pytest
from app.core.security import hash_password
from app.db.models import Article, User
from app.feed.cursor import FeedCursor, InvalidCursor, decode_cursor, encode_cursor
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_feed_cursor_is_signed_and_round_trips() -> None:
    cursor = FeedCursor(0.75, datetime(2026, 8, 1, tzinfo=UTC), 4)
    encoded = encode_cursor(cursor)
    decoded = decode_cursor(encoded)
    assert decoded == cursor
    with pytest.raises(InvalidCursor):
        decode_cursor(f"{encoded[:-1]}x")


@pytest.mark.asyncio
async def test_feed_paginates_without_cross_user_rows_and_applies_filters(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    user_factory,
    source_factory,
    article_factory,
) -> None:
    owner = User(email="feed-owner@example.com", password_hash=hash_password("feed-password"), role="user")
    other = user_factory()
    db_session.add_all([owner, other])
    await db_session.flush()
    source = source_factory(owner.id)
    other_source = source_factory(other.id)
    db_session.add_all([source, other_source])
    await db_session.flush()
    articles: list[Article] = []
    for index in range(3):
        article = article_factory(owner.id, source.id)
        article.status = "published"
        article.tags = ["linux"]
        article.relevance_score = 0.9 - (index * 0.1)
        article.published_at = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(days=index)
        articles.append(article)
    foreign = article_factory(other.id, other_source.id)
    foreign.status = "published"
    foreign.relevance_score = 1.0
    db_session.add_all([*articles, foreign])
    await db_session.commit()

    login = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": "feed-owner@example.com", "password": "feed-password"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    first = await auth_client.get("/api/v1/feed?page_size=2&tag=linux", headers=headers)
    assert first.status_code == 200
    first_items = first.json()["items"]
    assert len(first_items) == 2
    assert all(item["id"] != foreign.id for item in first_items)
    assert first_items[0]["source_id"] == source.id
    assert first_items[0]["source_name"] == source.name
    assert first_items[0]["relevance_score"] > first_items[1]["relevance_score"]
    next_cursor = first.json()["next_cursor"]
    assert next_cursor

    second = await auth_client.get(
        "/api/v1/feed?page_size=2&tag=linux",
        headers=headers,
        params={"cursor": next_cursor},
    )
    assert second.status_code == 200
    second_ids = {item["id"] for item in second.json()["items"]}
    assert second_ids.isdisjoint({item["id"] for item in first_items})
    assert second_ids == {articles[2].id}


@pytest.mark.asyncio
async def test_feed_rejects_tampered_cursor(auth_client: AsyncClient) -> None:
    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "cursor-owner@example.com", "password": "correct horse battery staple"},
    )
    response = await auth_client.get(
        "/api/v1/feed?cursor=invalid.cursor",
        headers={"Authorization": f"Bearer {registered.json()['access_token']}"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_feed_display_rank_responds_to_serendipity_without_changing_relevance(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    source_factory,
    article_factory,
) -> None:
    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "ranking-feed@example.com", "password": "correct horse battery staple"},
    )
    user_id = registered.json()["user"]["id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    preferred_source = source_factory(user_id)
    unfamiliar_source = source_factory(user_id)
    db_session.add_all([preferred_source, unfamiliar_source])
    await db_session.flush()
    preferred = article_factory(user_id, preferred_source.id)
    preferred.status = "published"
    preferred.tags = ["linux"]
    preferred.relevance_score = 0.9
    unfamiliar = article_factory(user_id, unfamiliar_source.id)
    unfamiliar.status = "published"
    unfamiliar.tags = ["astronomy"]
    unfamiliar.relevance_score = 0.6
    db_session.add_all([preferred, unfamiliar])
    await db_session.commit()

    await auth_client.patch(
        "/api/v1/preferences/topics/linux",
        headers=headers,
        json={"weight_score": 1.0},
    )
    await auth_client.post(
        "/api/v1/feedback",
        headers=headers,
        json={"action_type": "source", "rating": "like", "source_id": preferred_source.id},
    )
    zero = await auth_client.patch(
        "/api/v1/preferences/ranking",
        headers=headers,
        json={"serendipity": 0.0},
    )
    assert zero.status_code == 200
    relevance_feed = await auth_client.get("/api/v1/feed", headers=headers)
    assert [item["id"] for item in relevance_feed.json()["items"]] == [preferred.id, unfamiliar.id]
    assert relevance_feed.json()["items"][0]["display_rank"] == preferred.relevance_score

    one = await auth_client.patch(
        "/api/v1/preferences/ranking",
        headers=headers,
        json={"serendipity": 1.0},
    )
    assert one.status_code == 200
    exploration_feed = await auth_client.get("/api/v1/feed", headers=headers)
    assert [item["id"] for item in exploration_feed.json()["items"]] == [unfamiliar.id, preferred.id]
    assert exploration_feed.json()["items"][0]["score_breakdown"]["source_novelty"] == 1.0
