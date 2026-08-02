from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.observability.health import HealthSnapshot
from app.observability.metrics import MetricsSnapshot

ALERT_COOLDOWN_SECONDS = 300


@dataclass(frozen=True)
class AlertRule:
    name: str
    severity: str
    description: str
    cause: str
    owner: str
    recovery: str


@dataclass(frozen=True)
class AlertState:
    rule: str
    severity: str
    status: str
    message: str
    value: float
    threshold: float


@dataclass(frozen=True)
class AlertEvaluation:
    fired: tuple[AlertState, ...]
    resolved: tuple[AlertState, ...]
    rules: tuple[AlertRule, ...]


class AlertCooldown:
    """Suppress repeated firing of the same rule to avoid alert noise."""

    def __init__(self, seconds: float = ALERT_COOLDOWN_SECONDS) -> None:
        self.seconds = seconds
        self._last_fired: dict[str, float] = {}

    def allow(self, rule: str) -> bool:
        last = self._last_fired.get(rule)
        return last is None or time.monotonic() - last >= self.seconds

    def mark_fired(self, rule: str) -> None:
        self._last_fired[rule] = time.monotonic()


alert_cooldown = AlertCooldown()


RULES: tuple[AlertRule, ...] = (
    AlertRule(
        name="core_dependency_down",
        severity="critical",
        description="PostgreSQL or Redis is unreachable",
        cause="Database or rate-limit store outage; networking or credentials misconfiguration",
        owner="platform",
        recovery="Restart the failed container, verify DNS/credentials, confirm readiness probe recovers",
    ),
    AlertRule(
        name="reconstructible_dependency_down",
        severity="warning",
        description="Qdrant or the worker is unreachable",
        cause="Vector index or worker process outage; Qdrant is reconstructible from PostgreSQL",
        owner="platform",
        recovery="Restart Qdrant/worker; reindex vectors from PostgreSQL if the collection was lost",
    ),
    AlertRule(
        name="http_error_rate",
        severity="warning",
        description="HTTP 5xx failure rate above threshold",
        cause="Application errors, provider failures, or misconfigurations in API routes",
        owner="backend",
        recovery="Inspect http.error logs by request_id, check provider/LLM status, rollback recent deploys",
    ),
    AlertRule(
        name="http_p95_latency",
        severity="warning",
        description="HTTP p95 latency above threshold",
        cause="Slow queries, queue saturation, or provider latency",
        owner="backend",
        recovery="Review usage report latencies, index missing columns, scale workers",
    ),
    AlertRule(
        name="worker_queue_backlog",
        severity="warning",
        description="ARQ queue depth above threshold",
        cause="Workers saturated or queue stuck",
        owner="backend",
        recovery="Scale workers, restart ARQ, inspect job timeouts",
    ),
    AlertRule(
        name="llm_fallback_rate",
        severity="warning",
        description="LLM fallback rate above threshold",
        cause="Provider degraded or unavailable",
        owner="ai",
        recovery="Check provider status, switch LLM profile, verify cost/latency report",
    ),
    AlertRule(
        name="llm_cost_anomaly",
        severity="warning",
        description="Daily estimated LLM cost above threshold",
        cause="Runaway job loop or expensive model selected",
        owner="ai",
        recovery="Review usage report by tenant, pause jobs, adjust quotas",
    ),
)


def _operation(metrics: MetricsSnapshot, name: str) -> dict[str, float] | None:
    for operation in metrics.operations:
        if operation.operation == name:
            return {
                "failure_rate": operation.failure_rate,
                "p95_ms": operation.p95_ms,
                "count": float(operation.count),
            }
    return None


def evaluate_alerts(
    metrics_snapshot: MetricsSnapshot,
    health_snapshot: HealthSnapshot,
    *,
    http_error_rate_threshold: float = 0.05,
    http_p95_ms_threshold: float = 2_000.0,
    worker_queue_backlog_threshold: float = 100.0,
    llm_fallback_rate_threshold: float = 0.30,
    daily_cost_cents_threshold: float = 100_000.0,
) -> AlertEvaluation:
    fired: list[AlertState] = []
    resolved: list[AlertState] = []
    last_checked_at = 0.0

    def decide(rule_name: str, value: float, threshold: float, message: str) -> None:
        nonlocal last_checked_at
        rule = next((candidate for candidate in RULES if candidate.name == rule_name), None)
        if rule is None:
            return
        severity = rule.severity
        state = AlertState(
            rule=rule.name,
            severity=severity,
            status="firing" if value > threshold else "ok",
            message=message,
            value=value,
            threshold=threshold,
        )
        (fired if value > threshold else resolved).append(state)

    core_checks = ("database", "redis")
    reconstructible_checks = ("qdrant", "worker")
    core_down = [name for name in core_checks if health_snapshot.checks.get(name, _down()).status == "down"]
    if core_down:
        fired.append(
            AlertState(
                rule="core_dependency_down",
                severity="critical",
                status="firing",
                message=", ".join(core_down),
                value=1.0,
                threshold=0.0,
            )
        )
    else:
        resolved.append(
            AlertState(
                rule="core_dependency_down",
                severity="critical",
                status="ok",
                message="all core dependencies reachable",
                value=0.0,
                threshold=0.0,
            )
        )

    degraded = [
        name
        for name in reconstructible_checks
        if health_snapshot.checks.get(name, _down()).status == "down"
    ]
    if degraded:
        fired.append(
            AlertState(
                rule="reconstructible_dependency_down",
                severity="warning",
                status="firing",
                message=", ".join(degraded),
                value=1.0,
                threshold=0.0,
            )
        )
    else:
        resolved.append(
            AlertState(
                rule="reconstructible_dependency_down",
                severity="warning",
                status="ok",
                message="reconstructible dependencies reachable",
                value=0.0,
                threshold=0.0,
            )
        )

    http = _operation(metrics_snapshot, "http.get.api.v1.feed")
    if http is None:
        http = _operation(metrics_snapshot, "http.get.health.status")
    if http is not None:
        decide("http_error_rate", http["failure_rate"], http_error_rate_threshold, "http 5xx rate")
        decide("http_p95_latency", http["p95_ms"], http_p95_ms_threshold, "http p95 latency")

    queue_depth = metrics_snapshot.gauges.get("worker.queue_depth", 0.0)
    decide("worker_queue_backlog", queue_depth, worker_queue_backlog_threshold, "arq queue depth")

    llm = _operation(metrics_snapshot, "llm.briefing.generate")
    if llm is not None:
        decide("llm_fallback_rate", llm["failure_rate"], llm_fallback_rate_threshold, "llm fallback rate")

    daily_cost_cents = metrics_snapshot.gauges.get("quota.cost_today_cents", 0.0)
    decide("llm_cost_anomaly", daily_cost_cents, daily_cost_cents_threshold, "daily llm cost")

    return AlertEvaluation(fired=tuple(fired), resolved=tuple(resolved), rules=RULES)


def _down() -> Any:
    from app.observability.health import DependencyStatus

    return DependencyStatus("down", 0.0, "missing")
