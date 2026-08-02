from datetime import UTC, datetime

from fastapi import APIRouter, Response
from pydantic import BaseModel

from app.observability.health import HealthSnapshot, collect_health
from app.observability.metrics import MetricsSnapshot, metrics

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    service: str = "tuxnews-api"


class DependencyHealthPublic(BaseModel):
    status: str
    latency_ms: float
    detail: str | None = None


class HealthStatusPublic(BaseModel):
    status: str
    readiness: str
    liveness: str = "ok"
    service: str = "tuxnews-api"
    checked_at: datetime
    checks: dict[str, DependencyHealthPublic]


class MetricSnapshotPublic(BaseModel):
    operation: str
    count: int
    error_count: int
    failure_rate: float
    throughput_per_minute: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    last_duration_ms: float


class MetricsPublic(BaseModel):
    generated_at: datetime
    operations: list[MetricSnapshotPublic]
    gauges: dict[str, float]


def _health_public(snapshot: HealthSnapshot) -> HealthStatusPublic:
    return HealthStatusPublic(
        status=snapshot.status,
        readiness=snapshot.readiness,
        checked_at=datetime.fromtimestamp(snapshot.checked_at, tz=UTC),
        checks={name: DependencyHealthPublic(**check.__dict__) for name, check in snapshot.checks.items()},
    )


def _metrics_public(snapshot: MetricsSnapshot) -> MetricsPublic:
    return MetricsPublic(
        generated_at=datetime.fromtimestamp(snapshot.generated_at, tz=UTC),
        operations=[MetricSnapshotPublic(**operation.__dict__) for operation in snapshot.operations],
        gauges=snapshot.gauges,
    )


@router.get("/health/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/health/ready", response_model=HealthStatusPublic)
@router.get("/api/v1/health/ready", response_model=HealthStatusPublic, include_in_schema=False)
async def readiness(response: Response) -> HealthStatusPublic:
    snapshot = await collect_health()
    response.status_code = 200 if snapshot.readiness == "ready" else 503
    return _health_public(snapshot)


@router.get("/health/status", response_model=HealthStatusPublic)
@router.get("/api/v1/health/status", response_model=HealthStatusPublic, include_in_schema=False)
async def operational_status() -> HealthStatusPublic:
    return _health_public(await collect_health())


@router.get("/health/metrics", response_model=MetricsPublic)
@router.get("/api/v1/health/metrics", response_model=MetricsPublic, include_in_schema=False)
async def operational_metrics() -> MetricsPublic:
    return _metrics_public(metrics.snapshot())
