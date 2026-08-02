from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class ClusterStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    EMPTY = "empty"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ClusterRules:
    algorithm_version: str = "story-v1"
    window_hours: int = 72
    membership_threshold: float = 0.78
    merge_threshold: float = 0.9

    def __post_init__(self) -> None:
        if self.window_hours < 48 or self.window_hours > 72:
            raise ValueError("story windows must be between 48 and 72 hours")
        if not 0 <= self.membership_threshold <= 1:
            raise ValueError("membership threshold must be between 0 and 1")
        if not 0 <= self.merge_threshold <= 1:
            raise ValueError("merge threshold must be between 0 and 1")
        if self.merge_threshold < self.membership_threshold:
            raise ValueError("merge threshold cannot be below membership threshold")


@dataclass(frozen=True)
class MembershipDecision:
    accepted: bool
    similarity_score: float
    reason: str
    algorithm_version: str


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def evaluate_membership(
    *,
    article_time: datetime,
    cluster_start: datetime,
    cluster_end: datetime,
    similarity_score: float,
    rules: ClusterRules | None = None,
) -> MembershipDecision:
    rules = rules or ClusterRules()
    similarity = min(1.0, max(0.0, similarity_score))
    article_time = _as_utc(article_time)
    start = _as_utc(cluster_start)
    end = _as_utc(cluster_end)
    if start > end:
        raise ValueError("cluster window start cannot be after its end")
    if similarity < rules.membership_threshold:
        return MembershipDecision(False, similarity, "below_membership_threshold", rules.algorithm_version)
    window = timedelta(hours=rules.window_hours)
    if article_time < start - window or article_time > end + window:
        return MembershipDecision(False, similarity, "outside_temporal_window", rules.algorithm_version)
    return MembershipDecision(True, similarity, "semantic_and_temporal_match", rules.algorithm_version)


def cluster_status(
    *, member_count: int, has_ambiguity: bool, last_event: datetime | None, now: datetime
) -> ClusterStatus:
    if member_count == 0:
        return ClusterStatus.EMPTY
    if has_ambiguity:
        return ClusterStatus.AMBIGUOUS
    if last_event is not None and _as_utc(last_event) < _as_utc(now) - timedelta(hours=72):
        return ClusterStatus.STALE
    return ClusterStatus.ACTIVE


def should_merge_clusters(left_similarity: float, right_similarity: float, rules: ClusterRules | None = None) -> bool:
    rules = rules or ClusterRules()
    return min(1.0, max(0.0, left_similarity), max(0.0, right_similarity)) >= rules.merge_threshold
