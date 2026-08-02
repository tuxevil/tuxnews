from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TenantContext:
    """The tenant identity derived from authenticated state, never request input."""

    tenant_id: int


@dataclass(frozen=True)
class ActorContext:
    """The authenticated actor and the tenant in which it is acting."""

    tenant: TenantContext
    actor_type: str
    actor_id: str
    correlation_id: str | None = None

    @property
    def tenant_id(self) -> int:
        return self.tenant.tenant_id

    @property
    def user_id(self) -> int:
        return self.tenant_id


@dataclass(frozen=True)
class JobContext:
    """Serializable tenant and actor boundary for a worker invocation."""

    actor: ActorContext

    @property
    def tenant(self) -> TenantContext:
        return self.actor.tenant

    def to_payload(self) -> dict[str, str | int | None]:
        return {
            "tenant_id": self.actor.tenant_id,
            "actor_type": self.actor.actor_type,
            "actor_id": self.actor.actor_id,
            "correlation_id": self.actor.correlation_id,
        }


def job_context_from_payload(payload: Mapping[str, Any]) -> JobContext | None:
    value = payload.get("tenant_id")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    actor_type = payload.get("actor_type", "job")
    actor_id = payload.get("actor_id", "worker")
    correlation_id = payload.get("correlation_id")
    if not isinstance(actor_type, str) or not actor_type or len(actor_type) > 32:
        return None
    if not isinstance(actor_id, str) or not actor_id or len(actor_id) > 120:
        return None
    if correlation_id is not None and (not isinstance(correlation_id, str) or len(correlation_id) > 64):
        return None
    return JobContext(
        actor=ActorContext(
            tenant=TenantContext(value),
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )
    )


def serialize_job_context(actor: ActorContext) -> dict[str, str | int | None]:
    return JobContext(actor=actor).to_payload()


def tenant_from_job(payload: Mapping[str, Any]) -> TenantContext | None:
    context = job_context_from_payload(payload)
    return context.tenant if context is not None else None
