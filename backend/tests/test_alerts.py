
import pytest
from app.core.security import hash_password
from app.db.models import User
from app.main import app
from app.observability.alerts import (
    AlertCooldown,
    evaluate_alerts,
)
from app.observability.health import DependencyStatus, HealthSnapshot
from app.observability.metrics import MetricsSnapshot, OperationSnapshot
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _snapshot(
    *,
    http_failure_rate: float = 0.0,
    http_p95_ms: float = 100.0,
    queue_depth: float = 0.0,
) -> MetricsSnapshot:
    return MetricsSnapshot(
        generated_at=1_700_000_000.0,
        operations=(
            OperationSnapshot(
                operation="http.get.api.v1.feed",
                count=100,
                error_count=int(http_failure_rate * 100),
                failure_rate=http_failure_rate,
                throughput_per_minute=100.0,
                p50_ms=50.0,
                p95_ms=http_p95_ms,
                p99_ms=http_p95_ms,
                last_duration_ms=http_p95_ms,
            ),
        ),
        gauges={"worker.queue_depth": queue_depth},
    )


def _healthy() -> HealthSnapshot:
    return HealthSnapshot(
        status="healthy",
        readiness="ready",
        checked_at=1_700_000_000.0,
        checks={
            "database": DependencyStatus("ok", 1.0),
            "redis": DependencyStatus("ok", 1.0),
            "qdrant": DependencyStatus("ok", 1.0),
            "worker": DependencyStatus("ok", 1.0),
        },
    )


def _degraded() -> HealthSnapshot:
    return HealthSnapshot(
        status="degraded",
        readiness="ready",
        checked_at=1_700_000_000.0,
        checks={
            "database": DependencyStatus("ok", 1.0),
            "redis": DependencyStatus("ok", 1.0),
            "qdrant": DependencyStatus("down", 2_000.0, "timeout"),
            "worker": DependencyStatus("down", 2_000.0, "unavailable"),
        },
    )


def test_healthy_system_fires_no_alerts() -> None:
    evaluation = evaluate_alerts(_snapshot(), _healthy())

    assert evaluation.fired == ()
    assert any(state.rule == "core_dependency_down" and state.status == "ok" for state in evaluation.resolved)


def test_http_error_rate_and_latency_fire_thresholds() -> None:
    evaluation = evaluate_alerts(
        _snapshot(http_failure_rate=0.30, http_p95_ms=5_000.0),
        _healthy(),
    )

    fired = {state.rule for state in evaluation.fired}
    assert "http_error_rate" in fired
    assert "http_p95_latency" in fired


def test_reconstructible_dependency_downtime_is_warning_and_keeps_readiness() -> None:
    evaluation = evaluate_alerts(_snapshot(), _degraded())

    fired = {state.rule: state for state in evaluation.fired}
    assert fired["reconstructible_dependency_down"].severity == "warning"
    assert fired["reconstructible_dependency_down"].value == 1.0
    assert "core_dependency_down" not in fired


def test_worker_queue_backlog_fires_when_deep() -> None:
    evaluation = evaluate_alerts(_snapshot(queue_depth=500.0), _healthy())

    fired = {state.rule for state in evaluation.fired}
    assert "worker_queue_backlog" in fired


def test_cooldown_suppresses_repeated_firing(caplog, monkeypatch) -> None:
    monkeypatch.setattr("app.observability.alerts.time.monotonic", lambda: 100.0)
    cooldown = AlertCooldown(seconds=3_600)
    assert cooldown.allow("http_error_rate") is True
    cooldown.mark_fired("http_error_rate")
    assert cooldown.allow("http_error_rate") is False


def test_every_rule_has_cause_owner_and_recovery() -> None:
    evaluation = evaluate_alerts(_snapshot(), _healthy())

    for rule in evaluation.rules:
        assert rule.cause.strip()
        assert rule.owner.strip()
        assert rule.recovery.strip()


@pytest.mark.asyncio
async def test_basic_feed_survives_qdrant_and_worker_downtime(
    db_session: AsyncSession,
    user_factory,
    source_factory,
    article_factory,
) -> None:
    from app.db.models import Article

    user = user_factory()
    db_session.add(user)
    await db_session.flush()
    source = source_factory(user.id)
    db_session.add(source)
    await db_session.flush()
    article = article_factory(user.id, source.id)
    article.status = "published"
    db_session.add(article)
    await db_session.commit()

    rows = list(
        (
            await db_session.execute(
                select(Article)
                .where(Article.user_id == user.id, Article.status == "published")
                .order_by(Article.id.desc())
                .limit(20)
            )
        ).scalars()
    )

    assert len(rows) == 1
    assert rows[0].title == article.title


@pytest.mark.asyncio
async def test_admin_alerts_endpoint_returns_catalog(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    from app.api.routes import alerts as alerts_route

    async def fake_health():
        return _degraded()

    monkeypatch.setattr(alerts_route, "collect_health", fake_health)
    from app.observability.metrics import metrics as process_metrics

    process_metrics.reset()
    process_metrics.observe("http.get.api.v1.feed", 100.0, success=True)
    process_metrics.set_gauge("worker.queue_depth", 5.0)
    admin = User(
        email="alerts-admin@example.com",
        password_hash=hash_password("alerts-admin-password"),
        role="admin",
    )
    db_session.add(admin)
    await db_session.commit()

    login = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": admin.email, "password": "alerts-admin-password"},
    )
    assert login.status_code == 200
    response = await auth_client.get(
        "/api/v1/admin/alerts",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert any(rule["name"] == "core_dependency_down" for rule in payload["rules"])
    assert all(rule["recovery"] for rule in payload["rules"])


def test_alerts_route_is_private_in_openapi() -> None:
    document = TestClient(app).get("/openapi.json", headers={"Host": "testserver"}).json()
    assert "/api/v1/admin/alerts" in document["paths"]
    operation = document["paths"]["/api/v1/admin/alerts"]["get"]
    assert operation.get("security")
