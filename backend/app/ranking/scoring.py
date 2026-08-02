from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime

from app.core.config import Settings, get_settings
from app.db.models import Article
from app.preferences.settings import UserSettings


@dataclass(frozen=True)
class ScoreWeights:
    version: str
    semantic: float
    reputation: float
    feedback: float
    words_per_minute: int = 200

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ScoreWeights:
        settings = settings or get_settings()
        return cls(
            version=settings.score_weight_version,
            semantic=settings.score_semantic_weight,
            reputation=settings.score_reputation_weight,
            feedback=settings.score_feedback_weight,
            words_per_minute=settings.score_words_per_minute,
        )

    @classmethod
    def from_user_settings(cls, user_settings: UserSettings) -> ScoreWeights:
        return cls(
            version=f"user-settings-v{user_settings.version}",
            semantic=user_settings.score_weights.semantic,
            reputation=user_settings.score_weights.reputation,
            feedback=user_settings.score_weights.feedback,
            words_per_minute=user_settings.score_words_per_minute,
        )

    def __post_init__(self) -> None:
        if min(self.semantic, self.reputation, self.feedback) < 0:
            raise ValueError("score weights cannot be negative")
        if self.semantic + self.reputation + self.feedback <= 0:
            raise ValueError("at least one score weight must be positive")
        if self.words_per_minute < 1:
            raise ValueError("words_per_minute must be positive")


@dataclass(frozen=True)
class ScoreResult:
    score: float
    read_time_minutes: int
    breakdown: dict[str, float]
    weights_version: str
    used_fallback: bool


def calculate_read_time(text: str | None, *, words_per_minute: int = 200) -> int:
    if words_per_minute < 1:
        raise ValueError("words_per_minute must be positive")
    words = len(re.findall(r"\S+", text or ""))
    return max(1, math.ceil(words / words_per_minute))


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def calculate_score(
    *,
    semantic_similarity: float | None,
    source_reputation: float | None,
    feedback_penalty: float | None,
    text: str | None,
    weights: ScoreWeights | None = None,
) -> ScoreResult:
    weights = weights or ScoreWeights.from_settings()
    raw_components: dict[str, float | None] = {
        "semantic_similarity": None if semantic_similarity is None else _clamp(semantic_similarity),
        "source_reputation": None if source_reputation is None else _clamp(source_reputation),
        "feedback_penalty": None
        if feedback_penalty is None
        else min(1.0, max(-1.0, feedback_penalty)),
    }
    raw_weights = {
        "semantic_similarity": weights.semantic,
        "source_reputation": weights.reputation,
        "feedback_penalty": weights.feedback,
    }
    active = {name: value for name, value in raw_components.items() if value is not None and raw_weights[name] > 0}
    total_weight = sum(raw_weights[name] for name in active)
    if total_weight:
        effective_weights = {name: raw_weights[name] / total_weight for name in active}
        score = sum(
            (value if name != "feedback_penalty" else -value) * effective_weights[name]
            for name, value in active.items()
        )
    else:
        effective_weights = {}
        score = 0.0
    breakdown: dict[str, float] = {
        "semantic_similarity": float(raw_components["semantic_similarity"] or 0.0),
        "source_reputation": float(raw_components["source_reputation"] or 0.0),
        "feedback_penalty": float(raw_components["feedback_penalty"] or 0.0),
        "semantic_weight": effective_weights.get("semantic_similarity", 0.0),
        "reputation_weight": effective_weights.get("source_reputation", 0.0),
        "feedback_weight": effective_weights.get("feedback_penalty", 0.0),
        "fallback": 1.0 if semantic_similarity is None else 0.0,
    }
    return ScoreResult(
        score=_clamp(score),
        read_time_minutes=calculate_read_time(text, words_per_minute=weights.words_per_minute),
        breakdown=breakdown,
        weights_version=weights.version,
        used_fallback=semantic_similarity is None,
    )


def apply_score(article: Article, result: ScoreResult) -> Article:
    article.relevance_score = result.score
    article.read_time_minutes = result.read_time_minutes
    article.score_breakdown = result.breakdown
    return article


def rank_articles(articles: list[tuple[Article, ScoreResult]]) -> list[tuple[Article, ScoreResult]]:
    def sort_key(item: tuple[Article, ScoreResult]) -> tuple[float, float, int]:
        article, result = item
        published_timestamp = article.published_at.timestamp() if isinstance(article.published_at, datetime) else 0.0
        return (-result.score, -published_timestamp, article.id or 0)

    return sorted(articles, key=sort_key)
