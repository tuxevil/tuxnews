from __future__ import annotations

import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Final

_OPERATION_PATTERN: Final = re.compile(r"^[a-zA-Z0-9_.:-]{1,80}$")
_SAMPLE_LIMIT: Final = 2_048
_THROUGHPUT_WINDOW_SECONDS: Final = 60.0


def normalize_operation(operation: str) -> str:
    value = operation.strip().replace("/", ".")
    value = re.sub(r"[^a-zA-Z0-9_.:-]", "_", value).strip("._")
    return value[:80] if _OPERATION_PATTERN.fullmatch(value[:80]) else "unknown"


def _quantile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 2)


@dataclass(frozen=True)
class OperationSnapshot:
    operation: str
    count: int
    error_count: int
    failure_rate: float
    throughput_per_minute: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    last_duration_ms: float


@dataclass(frozen=True)
class MetricsSnapshot:
    generated_at: float
    operations: tuple[OperationSnapshot, ...]
    gauges: dict[str, float]


@dataclass
class _OperationStats:
    count: int = 0
    error_count: int = 0
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=_SAMPLE_LIMIT))
    recent: deque[tuple[float, bool]] = field(default_factory=deque)
    last_duration_ms: float = 0.0

    def observe(self, duration_ms: float, success: bool, now: float) -> None:
        self.count += 1
        self.error_count += 0 if success else 1
        self.samples.append(duration_ms)
        self.recent.append((now, success))
        self.last_duration_ms = duration_ms
        self._trim(now)

    def _trim(self, now: float) -> None:
        cutoff = now - _THROUGHPUT_WINDOW_SECONDS
        while self.recent and self.recent[0][0] < cutoff:
            self.recent.popleft()

    def snapshot(self, operation: str, now: float) -> OperationSnapshot:
        self._trim(now)
        recent_count = len(self.recent)
        return OperationSnapshot(
            operation=operation,
            count=self.count,
            error_count=self.error_count,
            failure_rate=round(self.error_count / self.count, 4) if self.count else 0.0,
            throughput_per_minute=round(recent_count * 60 / _THROUGHPUT_WINDOW_SECONDS, 2),
            p50_ms=_quantile(list(self.samples), 0.50),
            p95_ms=_quantile(list(self.samples), 0.95),
            p99_ms=_quantile(list(self.samples), 0.99),
            last_duration_ms=self.last_duration_ms,
        )


class MetricTimer:
    def __init__(self, registry: MetricsRegistry, operation: str) -> None:
        self.registry = registry
        self.operation = operation
        self.started = time.perf_counter()
        self.finished = False

    def finish(self, *, success: bool) -> None:
        if self.finished:
            return
        self.finished = True
        duration_ms = max((time.perf_counter() - self.started) * 1000, 0.0)
        self.registry.observe(self.operation, duration_ms, success=success)


class MetricsRegistry:
    """Bounded process-local metrics suitable for health and debugging views."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._operations: dict[str, _OperationStats] = {}
        self._gauges: dict[str, float] = {}

    def timer(self, operation: str) -> MetricTimer:
        return MetricTimer(self, operation)

    def observe(self, operation: str, duration_ms: float, *, success: bool) -> None:
        normalized = normalize_operation(operation)
        duration = max(float(duration_ms), 0.0)
        if not math.isfinite(duration):
            duration = 0.0
        now = time.monotonic()
        with self._lock:
            stats = self._operations.setdefault(normalized, _OperationStats())
            stats.observe(duration, success, now)

    def set_gauge(self, name: str, value: float) -> None:
        normalized = normalize_operation(name)
        numeric = float(value)
        if not math.isfinite(numeric):
            return
        with self._lock:
            self._gauges[normalized] = numeric

    def snapshot(self) -> MetricsSnapshot:
        now = time.monotonic()
        with self._lock:
            operations = tuple(stats.snapshot(operation, now) for operation, stats in sorted(self._operations.items()))
            gauges = dict(sorted(self._gauges.items()))
        return MetricsSnapshot(generated_at=time.time(), operations=operations, gauges=gauges)

    def reset(self) -> None:
        with self._lock:
            self._operations.clear()
            self._gauges.clear()


metrics = MetricsRegistry()
