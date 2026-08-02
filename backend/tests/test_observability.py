import json
import logging

from app.api.routes import health as health_route
from app.main import app
from app.observability import log_event, metrics
from app.observability.health import DependencyStatus, HealthSnapshot
from fastapi.testclient import TestClient


def test_metrics_expose_percentiles_throughput_and_failures() -> None:
    metrics.reset()
    for duration in range(1, 101):
        metrics.observe("ingestion.fetch", duration, success=duration < 96)

    snapshot = metrics.snapshot()
    operation = snapshot.operations[0]
    assert operation.operation == "ingestion.fetch"
    assert operation.count == 100
    assert operation.error_count == 5
    assert operation.failure_rate == 0.05
    assert operation.p95_ms == 95
    assert operation.p99_ms == 99
    assert operation.throughput_per_minute == 100


def test_log_event_redacts_sensitive_values_and_pseudonymizes_identity(caplog) -> None:
    caplog.set_level(logging.INFO)

    log_event(
        logging.getLogger("test.observability"),
        "test.event",
        tenant_id=42,
        actor_id="user-42",
        email="person@example.com",
        authorization="Bearer secret",
    )

    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "test.event"
    assert payload["tenant_id"] != 42
    assert payload["actor_id"] != "user-42"
    assert payload["email"] == "[REDACTED]"
    assert payload["authorization"] == "[REDACTED]"


def test_operational_status_exposes_partial_degradation(monkeypatch) -> None:
    snapshot = HealthSnapshot(
        status="degraded",
        readiness="ready",
        checked_at=1_700_000_000,
        checks={
            "database": DependencyStatus("ok", 1.2),
            "redis": DependencyStatus("ok", 1.4),
            "qdrant": DependencyStatus("down", 2_000, "timeout"),
        },
    )
    monkeypatch.setattr(health_route, "collect_health", lambda: _health_snapshot(snapshot))

    response = TestClient(app).get("/health/status", headers={"Host": "testserver"})

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["readiness"] == "ready"
    assert response.json()["checks"]["qdrant"]["detail"] == "timeout"


def test_readiness_returns_service_unavailable_when_core_dependency_is_down(monkeypatch) -> None:
    snapshot = HealthSnapshot(
        status="unavailable",
        readiness="not_ready",
        checked_at=1_700_000_000,
        checks={"database": DependencyStatus("down", 2_000, "unavailable")},
    )
    monkeypatch.setattr(health_route, "collect_health", lambda: _health_snapshot(snapshot))

    response = TestClient(app).get("/health/ready", headers={"Host": "testserver"})

    assert response.status_code == 503
    assert response.json()["readiness"] == "not_ready"


def _health_snapshot(snapshot: HealthSnapshot):
    async def load() -> HealthSnapshot:
        return snapshot

    return load()
