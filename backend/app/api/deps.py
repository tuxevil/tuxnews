from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import ActorContext, TenantContext
from app.core.permissions import has_scope, scopes_for_role
from app.core.quota import (
    QuotaBackendUnavailable,
    QuotaExceeded,
    QuotaRequest,
    QuotaService,
    get_quota_service,
)
from app.core.security import decode_token, token_is_revoked
from app.db.models import User
from app.db.session import get_session
from app.observability import set_actor_context

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class IdentityContext:
    user: User
    token: dict[str, Any]
    scopes: frozenset[str]

    @property
    def tenant(self) -> TenantContext:
        return TenantContext(self.user.id)

    @property
    def actor(self) -> ActorContext:
        return ActorContext(
            tenant=self.tenant,
            actor_type="user",
            actor_id=str(self.user.id),
        )


async def get_identity(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> IdentityContext:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    try:
        payload = decode_token(credentials.credentials, "access")
        user_id = int(payload["sub"])
        raw_scopes = payload.get("scopes", [])
        if not isinstance(raw_scopes, list) or not all(isinstance(scope, str) for scope in raw_scopes):
            raise TypeError("invalid token scopes")
    except (ValueError, KeyError, TypeError, jwt.InvalidTokenError) as exc:
        # Do not reveal whether a token was malformed, expired, or signed with another key.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials") from exc
    user = await session.scalar(select(User).where(User.id == user_id, User.is_active.is_(True)))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    if token_is_revoked(payload, user.tokens_revoked_at):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    # A role downgrade takes effect immediately even if an older access token
    # still exists; callers only receive scopes allowed by both token and role.
    effective_scopes = frozenset(raw_scopes).intersection(scopes_for_role(user.role))
    set_actor_context(tenant_id=user.id, actor_type="user", actor_id=user.id)
    return IdentityContext(user=user, token=payload, scopes=effective_scopes)


def _request_operation(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


async def _enforce_request_quota(
    request: Request,
    identity: IdentityContext,
    *,
    scope: str,
) -> None:
    service = getattr(request.app.state, "quota_service", None)
    if not isinstance(service, QuotaService):
        service = get_quota_service()
    try:
        decision = await service.enforce(
            QuotaRequest(
                tenant_id=identity.tenant.tenant_id,
                actor_id=identity.actor.actor_id,
                scope=scope,
                operation=_request_operation(request),
            )
        )
        request.state.quota_decision = decision
    except QuotaExceeded as exc:
        request.state.quota_decision = exc.decision
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": exc.code,
                "message": "quota exceeded",
                "retry_after": exc.retry_after,
            },
            headers=service.headers(exc.decision),
        ) from exc
    except QuotaBackendUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": "quota service unavailable"},
            headers={"Retry-After": "5"},
        ) from exc


async def get_current_user(
    request: Request,
    identity: IdentityContext = Depends(get_identity),
) -> User:
    await _enforce_request_quota(request, identity, scope="authenticated")
    return identity.user


def require_role(*roles: str):
    if not roles:
        raise ValueError("at least one role is required")

    async def dependency(
        identity: IdentityContext = Depends(get_identity),
    ) -> User:
        if identity.user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient permissions")
        return identity.user

    return dependency


def require_scope(scope: str):
    async def dependency(
        request: Request,
        identity: IdentityContext = Depends(get_identity),
    ) -> IdentityContext:
        direct_identity = request if isinstance(request, IdentityContext) else None
        resolved_identity = direct_identity or identity
        if not has_scope(resolved_identity.scopes, scope):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient scope")
        if direct_identity is None:
            await _enforce_request_quota(request, resolved_identity, scope=scope)
        return resolved_identity

    return dependency


def ownership_filter(model: Any, identity: IdentityContext) -> Any:
    if not hasattr(model, "user_id"):
        raise TypeError("tenant ownership requires a user_id column")
    return model.user_id == identity.tenant.tenant_id


def admin_ownership_filter(model: Any) -> Any:
    """Opt into cross-tenant access only from an explicitly administrative query."""

    if not hasattr(model, "user_id"):
        raise TypeError("tenant ownership requires a user_id column")
    return model.user_id.is_not(None)


async def get_owned_or_404(
    session: AsyncSession,
    model: Any,
    resource_id: int,
    identity: IdentityContext,
) -> Any:
    resource = await session.scalar(
        select(model).where(model.id == resource_id, ownership_filter(model, identity))
    )
    if resource is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="resource not found")
    return resource
