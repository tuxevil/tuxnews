"""Guard the committed load baseline and the release gate contract."""

import json
from pathlib import Path

from app.core.config import get_settings

BASELINE = Path(__file__).parents[2] / "benchmarks" / "feed-load-baseline.json"
FEED_P95_BUDGET_MS = 200.0


def test_committed_baseline_meets_feed_budget() -> None:
    report = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert report["generated_at"]
    feed = next(target for target in report["targets"] if target["target"] == "feed")
    assert feed["p95_ms"] <= FEED_P95_BUDGET_MS
    assert feed["error_rate"] == 0.0


def test_baseline_covers_all_measured_targets() -> None:
    report = json.loads(BASELINE.read_text(encoding="utf-8"))
    names = {target["target"] for target in report["targets"]}

    assert names == {"feed", "clusters", "sources", "briefings", "feedback"}
    for target in report["targets"]:
        for key in ("p50_ms", "p95_ms", "p99_ms", "errors", "throughput_per_minute"):
            assert key in target


def test_load_gate_configuration_is_sane() -> None:
    settings = get_settings()
    assert settings.quota_requests_per_window >= 1
    assert settings.quota_window_seconds >= 1
