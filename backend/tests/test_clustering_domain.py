from datetime import UTC, datetime, timedelta

import pytest
from app.clustering.domain import (
    ClusterRules,
    ClusterStatus,
    cluster_status,
    evaluate_membership,
    should_merge_clusters,
)


def test_membership_is_reproducible_and_explains_late_articles() -> None:
    rules = ClusterRules()
    start = datetime(2026, 8, 1, tzinfo=UTC)
    late = evaluate_membership(
        article_time=start + timedelta(hours=71),
        cluster_start=start,
        cluster_end=start + timedelta(hours=12),
        similarity_score=0.82,
        rules=rules,
    )
    assert late == evaluate_membership(
        article_time=start + timedelta(hours=71),
        cluster_start=start,
        cluster_end=start + timedelta(hours=12),
        similarity_score=0.82,
        rules=rules,
    )
    assert late.accepted
    assert late.reason == "semantic_and_temporal_match"
    assert late.algorithm_version == "story-v1"


@pytest.mark.parametrize(
    ("similarity", "reason"),
    [(0.2, "below_membership_threshold"), (0.8, "outside_temporal_window")],
)
def test_membership_rejects_ambiguous_candidates(similarity: float, reason: str) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    article_time = start if similarity < 0.5 else start + timedelta(days=4)
    decision = evaluate_membership(
        article_time=article_time,
        cluster_start=start,
        cluster_end=start + timedelta(hours=1),
        similarity_score=similarity,
    )
    assert not decision.accepted
    assert decision.reason == reason


@pytest.mark.parametrize(
    ("similarity", "accepted"),
    [(0.78, True), (0.779999, False), (1.0, True), (0.0, False)],
)
def test_membership_threshold_boundary_is_stable(similarity: float, accepted: bool) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    decision = evaluate_membership(
        article_time=start,
        cluster_start=start,
        cluster_end=start,
        similarity_score=similarity,
    )
    assert decision.accepted is accepted


@pytest.mark.parametrize(
    ("offset", "accepted"),
    [(timedelta(hours=72), True), (timedelta(hours=72, microseconds=1), False)],
)
def test_membership_temporal_window_boundary_is_stable(offset: timedelta, accepted: bool) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    decision = evaluate_membership(
        article_time=start + offset,
        cluster_start=start,
        cluster_end=start,
        similarity_score=0.9,
    )
    assert decision.accepted is accepted


def test_cluster_states_cover_empty_ambiguous_stale_and_active() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    assert cluster_status(member_count=0, has_ambiguity=False, last_event=None, now=now) == ClusterStatus.EMPTY
    assert cluster_status(member_count=2, has_ambiguity=True, last_event=now, now=now) == ClusterStatus.AMBIGUOUS
    assert (
        cluster_status(member_count=2, has_ambiguity=False, last_event=now - timedelta(days=4), now=now)
        == ClusterStatus.STALE
    )
    assert cluster_status(member_count=2, has_ambiguity=False, last_event=now, now=now) == ClusterStatus.ACTIVE


def test_merge_threshold_is_stricter_than_membership() -> None:
    assert should_merge_clusters(0.95, 0.92)
    assert not should_merge_clusters(0.88, 0.95)


def test_rules_reject_invalid_windows_and_thresholds() -> None:
    with pytest.raises(ValueError):
        ClusterRules(window_hours=24)
    with pytest.raises(ValueError):
        ClusterRules(membership_threshold=0.95, merge_threshold=0.8)
