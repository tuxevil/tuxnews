import pytest
from app.db.models import AuditEvent, Feedback, Source
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_profile_can_edit_reset_and_mute_without_erasing_history(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    article_factory,
) -> None:
    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "preferences-owner@example.com", "password": "correct horse battery staple"},
    )
    assert registered.status_code == 201
    user_id = registered.json()["user"]["id"]
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    source = Source(
        user_id=user_id,
        name="Preference source",
        url="https://preferences.example.test/feed",
        origin="dynamic",
    )
    db_session.add(source)
    await db_session.flush()
    article = article_factory(user_id, source.id)
    article.status = "published"
    db_session.add(article)
    await db_session.commit()

    initial = await auth_client.get("/api/v1/preferences", headers=headers)
    assert initial.status_code == 200
    assert initial.json()["topics"] == []
    assert initial.json()["sources"][0]["is_muted"] is False
    assert initial.json()["ranking"]["serendipity"] == 0.25

    feedback = await auth_client.post(
        "/api/v1/feedback",
        headers=headers,
        json={"action_type": "topic", "rating": "like", "topic_name": "Linux"},
    )
    assert feedback.status_code == 201

    edited_topic = await auth_client.patch(
        "/api/v1/preferences/topics/linux",
        headers=headers,
        json={"weight_score": 0.8},
    )
    assert edited_topic.status_code == 200
    assert edited_topic.json()["weight_score"] == 0.8
    assert edited_topic.json()["preference_version"] == 2

    muted = await auth_client.patch(
        f"/api/v1/preferences/sources/{source.id}",
        headers=headers,
        json={"is_muted": True},
    )
    assert muted.status_code == 200
    assert muted.json()["is_muted"] is True

    feed = await auth_client.get("/api/v1/feed", headers=headers)
    assert feed.status_code == 200
    assert feed.json()["items"] == []

    missing_confirmation = await auth_client.post(
        "/api/v1/preferences/topics/linux/reset",
        headers=headers,
        json={"confirm": False},
    )
    assert missing_confirmation.status_code == 422

    reset_topic = await auth_client.post(
        "/api/v1/preferences/topics/linux/reset",
        headers=headers,
        json={"confirm": True},
    )
    assert reset_topic.status_code == 204
    reset_source = await auth_client.post(
        f"/api/v1/preferences/sources/{source.id}/reset?confirm=true",
        headers=headers,
    )
    assert reset_source.status_code == 204

    profile = await auth_client.get("/api/v1/preferences", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["topics"] == []
    assert profile.json()["sources"][0]["is_muted"] is False
    assert profile.json()["sources"][0]["reputation_score"] == 0.5

    events = list(await db_session.scalars(select(Feedback).where(Feedback.user_id == user_id)))
    assert len(events) == 1
    assert events[0].is_current is False
    audits = list(
        await db_session.scalars(
            select(AuditEvent).where(AuditEvent.user_id == user_id).order_by(AuditEvent.id)
        )
    )
    assert [audit.action for audit in audits] == [
        "source.static_sync",
        "feedback.created",
        "preferences.topic.updated",
        "preferences.source.muted",
        "preferences.topic.reset",
        "preferences.source.reset",
    ]


@pytest.mark.asyncio
async def test_preferences_are_isolated_by_user(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    user_factory,
    source_factory,
) -> None:
    owner = user_factory()
    other = user_factory()
    db_session.add_all([owner, other])
    await db_session.flush()
    owner_source = source_factory(owner.id)
    db_session.add(owner_source)
    await db_session.commit()

    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "preferences-other@example.com", "password": "correct horse battery staple"},
    )
    assert registered.status_code == 201
    other_headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    hidden = await auth_client.patch(
        f"/api/v1/preferences/sources/{owner_source.id}",
        headers=other_headers,
        json={"is_muted": True},
    )
    assert hidden.status_code == 404
    profile = await auth_client.get("/api/v1/preferences", headers=other_headers)
    assert profile.status_code == 200
    sources = profile.json()["sources"]
    assert all(source["id"] != owner_source.id for source in sources)
    assert sources and all(source["origin"] == "static" for source in sources)

@pytest.mark.asyncio
async def test_ranking_preference_is_persisted_bounded_and_audited(
    auth_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "ranking-owner@example.com", "password": "correct horse battery staple"},
    )
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    updated = await auth_client.patch(
        "/api/v1/preferences/ranking",
        headers=headers,
        json={"serendipity": 1.0},
    )
    assert updated.status_code == 200
    assert updated.json() == {"serendipity": 1.0, "preference_version": 1}

    profile = await auth_client.get("/api/v1/preferences", headers=headers)
    assert profile.json()["ranking"] == {"serendipity": 1.0, "preference_version": 1}
    invalid = await auth_client.patch(
        "/api/v1/preferences/ranking",
        headers=headers,
        json={"serendipity": 1.01},
    )
    assert invalid.status_code == 422
    audits = list(
        await db_session.scalars(
            select(AuditEvent).where(AuditEvent.user_id == registered.json()["user"]["id"])
        )
    )
    assert [audit.action for audit in audits] == ["source.static_sync", "preferences.ranking.updated"]


@pytest.mark.asyncio
async def test_preference_routes_require_authentication(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/api/v1/preferences")
    assert response.status_code == 401
