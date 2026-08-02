from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from app.observability.context import current_context, pseudonymize

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|password|secret|token|credential|api[_-]?key|prompt|content|body|email)",
    re.IGNORECASE,
)
_MAX_STRING_LENGTH = 256


def _sanitize_url(value: str) -> str:
    if "://" in value and "?" in value:
        return value.split("?", 1)[0] + "?[REDACTED]"
    return value


def _safe_key(key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", key)[:80] or "field"


def _sanitize(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if key in {"tenant_id", "actor_id", "user_id"} and value is not None:
        return pseudonymize(value)
    if isinstance(value, Mapping):
        return {
            _safe_key(str(child_key)): _sanitize(str(child_key), child_value)
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(key, item) for item in value[:20]]
    if isinstance(value, str):
        sanitized = _sanitize_url(value)
        return sanitized[:_MAX_STRING_LENGTH]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_STRING_LENGTH]


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    context = current_context()
    payload: dict[str, Any] = {"event": event}
    if context.correlation_id is not None:
        payload["correlation_id"] = context.correlation_id
    if context.tenant_key is not None:
        payload["tenant"] = context.tenant_key
    if context.actor_key is not None:
        payload["actor"] = context.actor_key
    if context.actor_type is not None:
        payload["actor_type"] = context.actor_type
    payload.update({_safe_key(key): _sanitize(key, value) for key, value in fields.items()})
    logger.log(level, json.dumps(payload, sort_keys=True, separators=(",", ":")))
