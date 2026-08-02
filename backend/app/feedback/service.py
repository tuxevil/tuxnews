from __future__ import annotations

from typing import Literal

import nh3
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import record_audit
from app.core.config import get_settings
from app.core.context import ActorContext, TenantContext
from app.db.models import Article, Feedback, Source, User, UserTopic
from app.preferences.settings import resolve_user_settings
from app.ranking.scoring import ScoreWeights, apply_score, calculate_score

Rating = Literal["like", "dislike", "neutral"]
ActionType = Literal["article", "source", "topic", "quality"]


def _rating_delta(rating: str) -> float:
    return {"like": 1.0, "dislike": -1.0, "neutral": 0.0}.get(rating, 0.0)


def _feedback_penalty(rating: str) -> float:
    return 1.0 if rating == "dislike" else 0.0


async def _current_feedback(
    session: AsyncSession,
    *,
    user_id: int,
    action_type: str,
    article_id: int | None,
    source_id: int | None,
    topic_name: str | None,
) -> Feedback | None:
    return await session.scalar(
        select(Feedback)
        .where(
            Feedback.user_id == user_id,
            Feedback.action_type == action_type,
            Feedback.article_id == article_id,
            Feedback.source_id == source_id,
            Feedback.topic_name == topic_name,
            Feedback.is_current.is_(True),
        )
        .with_for_update()
    )


async def _recalculate_source_reputation(session: AsyncSession, source_id: int, user_id: int) -> None:
    source = await session.scalar(
        select(Source).where(Source.id == source_id, Source.user_id == user_id).with_for_update()
    )
    if source is None:
        return
    events = list(
        await session.scalars(
            select(Feedback).where(
                Feedback.user_id == user_id,
                Feedback.source_id == source_id,
                Feedback.action_type == "source",
                Feedback.is_current.is_(True),
            )
        )
    )
    source.reputation_score = min(1.0, max(0.0, 0.5 + sum(_rating_delta(event.rating) for event in events) * 0.05))
    source.reputation_version = (source.reputation_version or 0) + 1


async def _recalculate_topic_weight(session: AsyncSession, user_id: int, topic_name: str) -> None:
    events = list(
        await session.scalars(
            select(Feedback).where(
                Feedback.user_id == user_id,
                Feedback.topic_name == topic_name,
                Feedback.action_type == "topic",
                Feedback.is_current.is_(True),
            )
        )
    )
    topic = await session.scalar(
        select(UserTopic)
        .where(UserTopic.user_id == user_id, UserTopic.topic_name == topic_name)
        .with_for_update()
    )
    if topic is None:
        topic = UserTopic(user_id=user_id, topic_name=topic_name, preference_version=1)
        session.add(topic)
    else:
        topic.preference_version = (topic.preference_version or 0) + 1
    topic.weight_score = sum(_rating_delta(event.rating) for event in events) * 0.1


async def _recalculate_article_feedback(
    session: AsyncSession,
    article_id: int,
    user_id: int,
    *,
    weights: ScoreWeights,
) -> None:
    article = await session.scalar(
        select(Article).where(Article.id == article_id, Article.user_id == user_id).with_for_update()
    )
    if article is None:
        return
    events = list(
        await session.scalars(
            select(Feedback).where(
                Feedback.user_id == user_id,
                Feedback.article_id == article_id,
                Feedback.action_type.in_(["article", "quality"]),
                Feedback.is_current.is_(True),
            )
        )
    )
    breakdown = dict(article.score_breakdown or {})
    article_feedback = next(
        (event for event in events if event.action_type == "article"),
        None,
    )
    quality_feedback = next(
        (event for event in events if event.action_type == "quality"),
        None,
    )
    article_penalty = _feedback_penalty(article_feedback.rating) if article_feedback else 0.0
    quality_penalty = _feedback_penalty(quality_feedback.rating) if quality_feedback else 0.0
    result = calculate_score(
        semantic_similarity=breakdown.get("semantic_similarity"),
        source_reputation=breakdown.get("source_reputation"),
        feedback_penalty=max(article_penalty, quality_penalty),
        text=article.content_clean,
        weights=weights,
    )
    apply_score(article, result)
    next_version = (article.feedback_version or 0) + 1
    article.score_breakdown = {
        **result.breakdown,
        "quality_penalty": quality_penalty,
        "feedback_version": float(next_version),
    }
    article.feedback_version = next_version


async def _validate_target(
    session: AsyncSession,
    *,
    tenant_id: int,
    action_type: ActionType,
    article_id: int | None,
    source_id: int | None,
    topic_name: str | None,
) -> None:
    if action_type in {"article", "quality"}:
        if article_id is None:
            raise ValueError("article feedback requires an article target")
        article = await session.scalar(
            select(Article).where(Article.id == article_id, Article.user_id == tenant_id)
        )
        if article is None:
            raise ValueError("feedback target not found")
    elif action_type == "source":
        if source_id is None:
            raise ValueError("source feedback requires a source target")
        source = await session.scalar(
            select(Source).where(Source.id == source_id, Source.user_id == tenant_id)
        )
        if source is None:
            raise ValueError("feedback target not found")
    elif action_type == "topic" and not topic_name:
        raise ValueError("topic feedback requires a topic target")


async def submit_feedback(
    session: AsyncSession,
    *,
    tenant: TenantContext,
    action_type: ActionType,
    rating: Rating,
    article_id: int | None = None,
    source_id: int | None = None,
    topic_name: str | None = None,
    reason: str | None = None,
    correlation_id: str | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
    actor: ActorContext | None = None,
) -> Feedback:
    user_id = tenant.tenant_id
    user = await session.get(User, user_id)
    if user is None:
        raise ValueError("feedback user not found")
    user_settings = resolve_user_settings(user.settings_json, get_settings())
    weights = ScoreWeights.from_user_settings(user_settings)
    clean_reason = nh3.clean(reason, tags=set()).strip()[:500] if reason else None
    normalized_topic = topic_name.strip().lower() if topic_name else None
    await _validate_target(
        session,
        tenant_id=user_id,
        action_type=action_type,
        article_id=article_id,
        source_id=source_id,
        topic_name=normalized_topic,
    )
    for _ in range(3):
        current = await _current_feedback(
            session,
            user_id=user_id,
            action_type=action_type,
            article_id=article_id,
            source_id=source_id,
            topic_name=normalized_topic,
        )
        if current is not None and current.rating == rating and current.reason == clean_reason:
            return current
        if current is not None:
            current.is_current = False
        event = Feedback(
            user_id=user_id,
            article_id=article_id,
            source_id=source_id,
            topic_name=normalized_topic,
            rating=rating,
            action_type=action_type,
            reason=clean_reason,
            supersedes_id=current.id if current else None,
            is_current=True,
        )
        session.add(event)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            continue
        if action_type == "source" and source_id is not None:
            await _recalculate_source_reputation(session, source_id, user_id)
        elif action_type == "topic" and normalized_topic is not None:
            await _recalculate_topic_weight(session, user_id, normalized_topic)
        elif action_type in {"article", "quality"} and article_id is not None:
            await _recalculate_article_feedback(session, article_id, user_id, weights=weights)
        record_audit(
            session,
            user_id=user_id,
            action="feedback.created",
            resource_type=action_type,
            resource_id=str(article_id or source_id or normalized_topic),
            outcome="success",
            correlation_id=correlation_id,
            actor_type=actor_type,
            actor_id=actor_id,
            actor=actor,
            details={
                "rating": rating,
                "current": True,
                "supersedes_id": current.id if current else None,
            },
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            continue
        return event
    raise RuntimeError("feedback update conflicted with another request")
