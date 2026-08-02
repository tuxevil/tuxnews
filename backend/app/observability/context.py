from __future__ import annotations

import hashlib
import hmac
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Any

from app.core.config import get_settings


@dataclass(frozen=True)
class ObservabilityContext:
    correlation_id: str | None = None
    tenant_key: str | None = None
    actor_key: str | None = None
    actor_type: str | None = None


ContextToken = Token[ObservabilityContext | None]
_context: ContextVar[ObservabilityContext | None] = ContextVar(
    "tuxnews_observability_context",
    default=None,
)


def pseudonymize(value: Any) -> str:
    """Return a stable, non-reversible identifier for telemetry labels."""

    raw = str(value).encode("utf-8")
    salt = get_settings().observability_hash_salt.encode("utf-8")
    digest = hmac.new(salt, raw, hashlib.sha256).hexdigest()
    return f"p_{digest[:16]}"


def current_context() -> ObservabilityContext:
    return _context.get() or ObservabilityContext()


def bind_context(
    *,
    correlation_id: str | None = None,
    tenant_id: int | str | None = None,
    actor_type: str | None = None,
    actor_id: int | str | None = None,
) -> ContextToken:
    previous = current_context()
    return _context.set(
        ObservabilityContext(
            correlation_id=correlation_id,
            tenant_key=pseudonymize(tenant_id) if tenant_id is not None else previous.tenant_key,
            actor_key=pseudonymize(actor_id) if actor_id is not None else previous.actor_key,
            actor_type=actor_type or previous.actor_type,
        )
    )


def set_actor_context(*, tenant_id: int | str, actor_type: str, actor_id: int | str) -> None:
    _context.set(
        replace(
            current_context(),
            tenant_key=pseudonymize(tenant_id),
            actor_key=pseudonymize(actor_id),
            actor_type=actor_type,
        )
    )


def reset_context(token: ContextToken) -> None:
    _context.reset(token)
