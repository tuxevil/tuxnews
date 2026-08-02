from __future__ import annotations

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken

from app.core.context import ActorContext, TenantContext
from app.core.quota import QuotaBackendUnavailable, QuotaExceeded, QuotaRequest, get_quota_service
from app.observability import set_actor_context

MCPActor = ActorContext


def actor_from_token(token: AccessToken, ctx: Context) -> MCPActor:
    try:
        user_id = int(token.claims["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolError("authenticated identity is invalid") from exc
    actor_type = "agent" if token.claims.get("type") == "agent" else "user"
    actor = ActorContext(
        tenant=TenantContext(user_id),
        actor_type=actor_type,
        actor_id=token.client_id,
        correlation_id=str(ctx.request_id),
    )
    set_actor_context(tenant_id=actor.tenant_id, actor_type=actor.actor_type, actor_id=actor.actor_id)
    return actor


def require_scope(token: AccessToken, required_scope: str) -> None:
    scopes = set(token.scopes)
    if "*" in scopes:
        return
    aliases = {
        "news:read": {"news:read", "content:read"},
        "sources:write": {"sources:write"},
        "feedback:write": {"feedback:write"},
        "archive:write": {"archive:write"},
    }
    if not scopes.intersection(aliases.get(required_scope, {required_scope})):
        raise ToolError(f"scope required: {required_scope}")


def require_confirmation(confirmed: bool) -> None:
    if not confirmed:
        raise ToolError("explicit human confirmation is required for this mutation")


async def enforce_quota(
    actor: MCPActor,
    *,
    scope: str,
    operation: str,
    provider: str | None = None,
    cost_cents: int = 0,
) -> None:
    service = get_quota_service()
    try:
        await service.enforce(
            QuotaRequest(
                tenant_id=actor.tenant_id,
                actor_id=actor.actor_id,
                scope=scope,
                operation=operation,
                provider=provider,
                cost_cents=cost_cents,
            )
        )
    except QuotaExceeded as exc:
        raise ToolError(f"quota_exceeded; retry_after={exc.retry_after}") from exc
    except QuotaBackendUnavailable as exc:
        raise ToolError("quota service unavailable; retry_after=5") from exc
