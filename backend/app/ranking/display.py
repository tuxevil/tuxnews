"""Deterministic, user-scoped mixing of relevance and exploration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Article, Feedback, UserTopic

DEFAULT_SERENDIPITY = 0.25
MIN_SERENDIPITY = 0.0
MAX_SERENDIPITY = 1.0


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def validate_serendipity(value: float) -> float:
    if not isfinite(value) or not MIN_SERENDIPITY <= value <= MAX_SERENDIPITY:
        raise ValueError("serendipity must be between 0 and 1")
    return value


@dataclass(frozen=True)
class RankingContext:
    topic_weights: Mapping[str, float]
    source_signals: Mapping[int, str]


@dataclass(frozen=True)
class ExplorationResult:
    score: float
    source_novelty: float
    topic_novelty: float


@dataclass(frozen=True)
class DisplayRankResult:
    display_rank: float
    exploration_score: float
    source_novelty: float
    topic_novelty: float
    breakdown: dict[str, float]


@dataclass(frozen=True)
class RankedArticle:
    article: Article
    source_name: str
    result: DisplayRankResult
    score_breakdown: dict[str, float]


async def load_ranking_context(session: AsyncSession, user_id: int) -> RankingContext:
    topics = await session.execute(
        select(UserTopic.topic_name, UserTopic.weight_score).where(UserTopic.user_id == user_id)
    )
    topic_weights = {str(name).strip().lower(): float(weight) for name, weight in topics}
    source_feedback = await session.execute(
        select(Feedback.source_id, Feedback.rating).where(
            Feedback.user_id == user_id,
            Feedback.action_type == "source",
            Feedback.source_id.is_not(None),
            Feedback.is_current.is_(True),
        )
    )
    source_signals = {int(source_id): str(rating) for source_id, rating in source_feedback if source_id is not None}
    return RankingContext(topic_weights=topic_weights, source_signals=source_signals)


def calculate_exploration_score(
    *,
    tags: Sequence[str],
    source_signal: str | None,
    topic_weights: Mapping[str, float],
) -> ExplorationResult:
    source_novelty = (
        1.0
        if source_signal is None
        else {"like": 0.0, "dislike": 0.25, "neutral": 0.5}.get(source_signal, 1.0)
    )
    normalized_tags = {tag.strip().lower() for tag in tags if isinstance(tag, str) and tag.strip()}
    if normalized_tags:
        topic_signals = [abs(_clamp(float(topic_weights.get(tag, 0.0)))) for tag in normalized_tags]
        topic_novelty = _clamp(1.0 - (sum(topic_signals) / len(topic_signals)))
    else:
        topic_novelty = 0.5
    score = round((source_novelty + topic_novelty) / 2, 8)
    return ExplorationResult(
        score=score,
        source_novelty=round(source_novelty, 8),
        topic_novelty=round(topic_novelty, 8),
    )


def calculate_display_rank(
    *,
    relevance_score: float,
    tags: Sequence[str],
    source_signal: str | None,
    topic_weights: Mapping[str, float],
    serendipity: float,
) -> DisplayRankResult:
    serendipity = validate_serendipity(serendipity)
    relevance = _clamp(float(relevance_score))
    exploration = calculate_exploration_score(
        tags=tags,
        source_signal=source_signal,
        topic_weights=topic_weights,
    )
    display_rank = round(
        (relevance * (1.0 - serendipity)) + (exploration.score * serendipity),
        8,
    )
    return DisplayRankResult(
        display_rank=display_rank,
        exploration_score=exploration.score,
        source_novelty=exploration.source_novelty,
        topic_novelty=exploration.topic_novelty,
        breakdown={
            "relevance_score": relevance,
            "exploration_score": exploration.score,
            "source_novelty": exploration.source_novelty,
            "topic_novelty": exploration.topic_novelty,
            "serendipity": serendipity,
            "relevance_weight": round(1.0 - serendipity, 8),
            "exploration_weight": serendipity,
            "display_rank": display_rank,
        },
    )


def rank_articles_for_display(
    rows: Sequence[tuple[Article, str]],
    *,
    context: RankingContext,
    serendipity: float,
) -> list[RankedArticle]:
    ranked: list[RankedArticle] = []
    for article, source_name in rows:
        result = calculate_display_rank(
            relevance_score=article.relevance_score,
            tags=article.tags,
            source_signal=context.source_signals.get(article.source_id),
            topic_weights=context.topic_weights,
            serendipity=serendipity,
        )
        base_breakdown = {
            str(key): float(value)
            for key, value in (article.score_breakdown or {}).items()
            if isinstance(value, (int, float)) and isfinite(float(value))
        }
        base_breakdown.update(result.breakdown)
        ranked.append(
            RankedArticle(
                article=article,
                source_name=source_name,
                result=result,
                score_breakdown=base_breakdown,
            )
        )

    def sort_key(item: RankedArticle) -> tuple[float, float, int]:
        published_timestamp = (
            item.article.published_at.timestamp() if isinstance(item.article.published_at, datetime) else 0.0
        )
        return (-item.result.display_rank, -published_timestamp, -(item.article.id or 0))

    return sorted(ranked, key=sort_key)
