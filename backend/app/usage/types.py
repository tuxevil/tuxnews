from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UsageContext:
    tenant_id: int
    actor_type: str
    actor_id: str
    operation: str
    correlation_id: str | None = None


@dataclass(frozen=True)
class UsageRecord:
    tenant_id: int
    actor_type: str
    actor_id: str
    operation: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cost_is_estimated: bool
    cost_currency: str
    latency_ms: int
    outcome: str
    used_fallback: bool
    attempt_count: int
    error_code: str | None = None
    provider_request_id: str | None = None
    correlation_id: str | None = None
