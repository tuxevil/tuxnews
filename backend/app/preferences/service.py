from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import record_audit
from app.core.config import Settings, get_settings
from app.core.context import ActorContext
from app.db.models import Feedback, Source, User, UserTopic
from app.preferences.settings import (
    UserSettings,
    UserSettingsUpdate,
    resolve_user_settings,
    update_user_settings_document,
)
from app.ranking.display import validate_serendipity


async def list_topics(session: AsyncSession, user_id: int) -> list[UserTopic]:
    result = await session.scalars(
        select(UserTopic).where(UserTopic.user_id == user_id).order_by(UserTopic.weight_score.desc(), UserTopic.id)
    )
    return list(result)


async def list_sources(session: AsyncSession, user_id: int) -> list[Source]:
    result = await session.scalars(select(Source).where(Source.user_id == user_id).order_by(Source.id))
    return list(result)


async def get_user_settings(
    session: AsyncSession,
    *,
    user_id: int,
    settings: Settings | None = None,
) -> UserSettings | None:
    user = await session.get(User, user_id)
    if user is None:
        return None
    return resolve_user_settings(user.settings_json, settings or get_settings())


async def update_user_settings(
    session: AsyncSession,
    *,
    user_id: int,
    update: UserSettingsUpdate,
    actor: ActorContext | None = None,
    correlation_id: str | None = None,
    settings: Settings | None = None,
) -> UserSettings | None:
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        return None
    document, resolved, changed = update_user_settings_document(
        user.settings_json,
        update,
        settings or get_settings(),
    )
    user.settings_json = document
    record_audit(
        session,
        user_id=user_id,
        action="preferences.settings.updated",
        resource_type="user_settings",
        resource_id=str(user_id),
        outcome="success",
        correlation_id=correlation_id,
        actor=actor,
        details={"changed": changed, "version": resolved.version},
    )
    await session.commit()
    return resolved


async def update_topic(
    session: AsyncSession,
    *,
    user_id: int,
    topic_name: str,
    weight_score: float,
    actor: ActorContext | None = None,
    correlation_id: str | None = None,
) -> UserTopic:
    topic = await session.scalar(
        select(UserTopic)
        .where(UserTopic.user_id == user_id, UserTopic.topic_name == topic_name)
        .with_for_update()
    )
    if topic is None:
        topic = UserTopic(
            user_id=user_id,
            topic_name=topic_name,
            weight_score=weight_score,
            preference_version=1,
        )
        session.add(topic)
        previous_weight = None
    else:
        previous_weight = topic.weight_score
        if topic.weight_score == weight_score:
            return topic
        topic.weight_score = weight_score
        topic.preference_version = (topic.preference_version or 0) + 1
    record_audit(
        session,
        user_id=user_id,
        action="preferences.topic.updated",
        resource_type="topic",
        resource_id=topic_name,
        outcome="success",
        correlation_id=correlation_id,
        actor=actor,
        details={
            "previous_weight": previous_weight,
            "weight": weight_score,
            "version": topic.preference_version,
        },
    )
    await session.commit()
    return topic


async def _deactivate_feedback(
    session: AsyncSession,
    *,
    user_id: int,
    action_type: str,
    topic_name: str | None = None,
    source_id: int | None = None,
) -> int:
    result = await session.scalars(
        select(Feedback).where(
            Feedback.user_id == user_id,
            Feedback.action_type == action_type,
            Feedback.topic_name == topic_name,
            Feedback.source_id == source_id,
            Feedback.is_current.is_(True),
        )
    )
    events = list(result)
    for event in events:
        event.is_current = False
    return len(events)


async def reset_topic(
    session: AsyncSession,
    *,
    user_id: int,
    topic_name: str,
    actor: ActorContext | None = None,
    correlation_id: str | None = None,
) -> bool:
    topic = await session.scalar(
        select(UserTopic)
        .where(UserTopic.user_id == user_id, UserTopic.topic_name == topic_name)
        .with_for_update()
    )
    deactivated = await _deactivate_feedback(
        session,
        user_id=user_id,
        action_type="topic",
        topic_name=topic_name,
    )
    if topic is None and deactivated == 0:
        await session.rollback()
        return False
    if topic is not None:
        await session.delete(topic)
    record_audit(
        session,
        user_id=user_id,
        action="preferences.topic.reset",
        resource_type="topic",
        resource_id=topic_name,
        outcome="success",
        correlation_id=correlation_id,
        actor=actor,
        details={"deactivated_feedback": deactivated},
    )
    await session.commit()
    return True


async def update_source_mute(
    session: AsyncSession,
    *,
    user_id: int,
    source_id: int,
    is_muted: bool,
    actor: ActorContext | None = None,
    correlation_id: str | None = None,
) -> Source | None:
    source = await session.scalar(
        select(Source).where(Source.id == source_id, Source.user_id == user_id).with_for_update()
    )
    if source is None:
        return None
    if source.is_muted == is_muted:
        return source
    source.is_muted = is_muted
    source.preference_version = (source.preference_version or 0) + 1
    record_audit(
        session,
        user_id=user_id,
        action="preferences.source.muted" if is_muted else "preferences.source.unmuted",
        resource_type="source",
        resource_id=str(source_id),
        outcome="success",
        correlation_id=correlation_id,
        actor=actor,
        details={"is_muted": is_muted, "version": source.preference_version},
    )
    await session.commit()
    return source


async def update_serendipity(
    session: AsyncSession,
    *,
    user_id: int,
    serendipity: float,
    actor: ActorContext | None = None,
    correlation_id: str | None = None,
) -> User | None:
    serendipity = validate_serendipity(serendipity)
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        return None
    if user.serendipity_score == serendipity:
        return user
    previous = user.serendipity_score
    user.serendipity_score = serendipity
    user.ranking_preference_version = (user.ranking_preference_version or 0) + 1
    record_audit(
        session,
        user_id=user_id,
        action="preferences.ranking.updated",
        resource_type="ranking",
        resource_id=str(user_id),
        outcome="success",
        correlation_id=correlation_id,
        actor=actor,
        details={
            "previous_serendipity": previous,
            "serendipity": serendipity,
            "version": user.ranking_preference_version,
        },
    )
    await session.commit()
    return user


async def reset_source(
    session: AsyncSession,
    *,
    user_id: int,
    source_id: int,
    actor: ActorContext | None = None,
    correlation_id: str | None = None,
) -> bool:
    source = await session.scalar(
        select(Source).where(Source.id == source_id, Source.user_id == user_id).with_for_update()
    )
    if source is None:
        return False
    deactivated = await _deactivate_feedback(
        session,
        user_id=user_id,
        action_type="source",
        source_id=source_id,
    )
    source.is_muted = False
    source.reputation_score = 0.5
    source.reputation_version = (source.reputation_version or 0) + 1
    source.preference_version = (source.preference_version or 0) + 1
    record_audit(
        session,
        user_id=user_id,
        action="preferences.source.reset",
        resource_type="source",
        resource_id=str(source_id),
        outcome="success",
        correlation_id=correlation_id,
        actor=actor,
        details={
            "deactivated_feedback": deactivated,
            "reputation_version": source.reputation_version,
            "preference_version": source.preference_version,
        },
    )
    await session.commit()
    return True


def profile_version(
    topics: Sequence[UserTopic],
    sources: Sequence[Source],
    ranking_preference_version: int = 0,
) -> int:
    versions = [0]
    versions.extend(topic.preference_version for topic in topics)
    versions.extend(source.preference_version for source in sources)
    versions.append(ranking_preference_version)
    return max(versions)
