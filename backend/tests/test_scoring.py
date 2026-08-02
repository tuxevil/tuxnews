from datetime import UTC, datetime

import pytest
from app.core.config import Settings
from app.db.models import Article
from app.ranking.display import calculate_display_rank, calculate_exploration_score, validate_serendipity
from app.ranking.scoring import (
    ScoreWeights,
    apply_score,
    calculate_read_time,
    calculate_score,
    rank_articles,
)


def test_read_time_uses_stable_200_wpm_ceiling() -> None:
    assert calculate_read_time("one two three") == 1
    assert calculate_read_time("word " * 201) == 2


def test_score_is_deterministic_explainable_and_clamped() -> None:
    weights = ScoreWeights("test-v1", 0.6, 0.25, 0.15)
    first = calculate_score(
        semantic_similarity=1.5,
        source_reputation=0.8,
        feedback_penalty=0.2,
        text="article text",
        weights=weights,
    )
    second = calculate_score(
        semantic_similarity=1.5,
        source_reputation=0.8,
        feedback_penalty=0.2,
        text="article text",
        weights=weights,
    )
    assert first == second
    assert 0 <= first.score <= 1
    assert first.breakdown["semantic_similarity"] == 1.0
    assert first.weights_version == "test-v1"


def test_missing_embedding_uses_reputation_fallback_without_breaking() -> None:
    result = calculate_score(
        semantic_similarity=None,
        source_reputation=0.8,
        feedback_penalty=None,
        text="article",
    )

    assert result.used_fallback is True
    assert result.score == 0.8
    assert result.breakdown["semantic_weight"] == 0.0


def test_feedback_penalty_only_changes_feedback_component() -> None:
    weights = ScoreWeights("test-v1", 0.5, 0.5, 0.5)
    without_feedback = calculate_score(
        semantic_similarity=0.8,
        source_reputation=0.8,
        feedback_penalty=None,
        text="article",
        weights=weights,
    )
    with_feedback = calculate_score(
        semantic_similarity=0.8,
        source_reputation=0.8,
        feedback_penalty=0.4,
        text="article",
        weights=weights,
    )
    assert with_feedback.score < without_feedback.score
    assert with_feedback.breakdown["semantic_similarity"] == without_feedback.breakdown["semantic_similarity"]
    assert with_feedback.breakdown["source_reputation"] == without_feedback.breakdown["source_reputation"]


def test_apply_score_and_ranking_are_deterministic() -> None:
    first = Article(
        id=1,
        user_id=1,
        source_id=1,
        title="First",
        original_title="First",
        url="https://example.com/1",
        canonical_url_hash="1" * 64,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = Article(
        id=2,
        user_id=1,
        source_id=1,
        title="Second",
        original_title="Second",
        url="https://example.com/2",
        canonical_url_hash="2" * 64,
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    low = calculate_score(semantic_similarity=0.5, source_reputation=0.5, feedback_penalty=0, text="x")
    high = calculate_score(semantic_similarity=0.9, source_reputation=0.9, feedback_penalty=0, text="x")
    apply_score(first, low)
    assert first.relevance_score == low.score
    assert [article.id for article, _ in rank_articles([(first, low), (second, high)])] == [2, 1]


def test_score_settings_are_loaded_from_validated_configuration() -> None:
    weights = ScoreWeights.from_settings(Settings(score_weight_version="v2", score_words_per_minute=100))
    assert weights.version == "v2"
    assert weights.words_per_minute == 100


def test_invalid_weights_are_rejected() -> None:
    with pytest.raises(ValueError):
        ScoreWeights("invalid", 0, 0, 0)


def test_display_rank_extremes_keep_relevance_and_exploration_separate() -> None:
    relevance_only = calculate_display_rank(
        relevance_score=0.8,
        tags=["linux"],
        source_signal="like",
        topic_weights={"linux": 1.0},
        serendipity=0.0,
    )
    exploration_only = calculate_display_rank(
        relevance_score=0.8,
        tags=["astronomy"],
        source_signal=None,
        topic_weights={},
        serendipity=1.0,
    )

    assert relevance_only.display_rank == 0.8
    assert relevance_only.exploration_score == 0.0
    assert exploration_only.display_rank == 1.0
    assert exploration_only.breakdown["relevance_weight"] == 0.0


def test_exploration_score_is_explainable_and_deterministic() -> None:
    first = calculate_exploration_score(
        tags=["Linux", "systems"],
        source_signal=None,
        topic_weights={"linux": 0.4},
    )
    second = calculate_exploration_score(
        tags=["Linux", "systems"],
        source_signal=None,
        topic_weights={"linux": 0.4},
    )

    assert first == second
    assert first.source_novelty == 1.0
    assert first.topic_novelty == 0.8
    assert first.score == 0.9
    assert first == calculate_exploration_score(
        tags=["systems", "Linux", "Linux"],
        source_signal=None,
        topic_weights={"linux": 0.4},
    )


@pytest.mark.parametrize(
    ("source_signal", "expected"),
    [(None, 1.0), ("like", 0.0), ("dislike", 0.25), ("neutral", 0.5)],
)
def test_source_novelty_signal_is_bounded(source_signal: str | None, expected: float) -> None:
    result = calculate_exploration_score(tags=["new"], source_signal=source_signal, topic_weights={})
    assert result.source_novelty == expected


def test_serendipity_is_bounded() -> None:
    assert validate_serendipity(0.0) == 0.0
    assert validate_serendipity(1.0) == 1.0
    with pytest.raises(ValueError):
        validate_serendipity(-0.01)
    with pytest.raises(ValueError):
        validate_serendipity(1.01)
