from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import record_audit
from app.core.config import get_settings
from app.core.permissions import AgentScope
from app.core.security import hash_agent_token, new_agent_token
from app.db.models import AgentToken

TOKEN_NAME_MAX_LENGTH = 120
ALLOWED_SCOPES = frozenset(scope.value for scope in AgentScope)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def validate_agent_token_name(name: str) -> str:
    normalized = name.strip()
    if not normalized or len(normalized) > TOKEN_NAME_MAX_LENGTH:
        raise ValueError("token name must contain between 1 and 120 characters")
    return normalized


def validate_agent_scopes(scopes: Sequence[str]) -> list[str]:
    normalized = list(dict.fromkeys(scopes))
    if not normalized:
        raise ValueError("at least one agent scope is required")
    unknown = set(normalized).difference(ALLOWED_SCOPES)
    if unknown:
        raise ValueError("unknown agent scope")
    return sorted(normalized)


def default_agent_expiration() -> datetime:
    return datetime.now(UTC) + timedelta(days=get_settings().agent_token_default_days)


def validate_agent_expiration(expires_at: datetime | None) -> datetime:
    normalized = _as_utc(expires_at) if expires_at is not None else default_agent_expiration()
    if normalized <= datetime.now(UTC):
        raise ValueError("agent token expiration must be in the future")
    return normalized


async def create_agent_token(
    session: AsyncSession,
    *,
    user_id: int,
    name: str,
    scopes: Sequence[str],
    expires_at: datetime | None = None,
    correlation_id: str | None = None,
) -> tuple[AgentToken, str]:
    secret = new_agent_token()
    token = AgentToken(
        user_id=user_id,
        name=validate_agent_token_name(name),
        token_hash=hash_agent_token(secret),
        scopes=validate_agent_scopes(scopes),
        expires_at=validate_agent_expiration(expires_at),
    )
    session.add(token)
    await session.flush()
    expires_at_value = token.expires_at
    if expires_at_value is None:
        raise RuntimeError("agent token expiration was not assigned")
    record_audit(
        session,
        user_id=user_id,
        action="agent_token.created",
        resource_type="agent_token",
        resource_id=str(token.id),
        outcome="success",
        correlation_id=correlation_id,
        details={"scopes": token.scopes, "expires_at": expires_at_value.isoformat()},
    )
    await session.commit()
    await session.refresh(token)
    return token, secret


async def list_agent_tokens(session: AsyncSession, *, user_id: int) -> list[AgentToken]:
    result = await session.scalars(
        select(AgentToken)
        .where(AgentToken.user_id == user_id)
        .order_by(AgentToken.created_at.desc(), AgentToken.id.desc())
    )
    return list(result)


async def revoke_agent_token(
    session: AsyncSession,
    *,
    user_id: int,
    token_id: int,
    correlation_id: str | None = None,
) -> AgentToken | None:
    token = await session.scalar(
        select(AgentToken).where(AgentToken.id == token_id, AgentToken.user_id == user_id).with_for_update()
    )
    if token is None:
        return None
    if token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)
        record_audit(
            session,
            user_id=user_id,
            action="agent_token.revoked",
            resource_type="agent_token",
            resource_id=str(token.id),
            outcome="success",
            correlation_id=correlation_id,
        )
        await session.commit()
        await session.refresh(token)
    return token


async def rotate_agent_token(
    session: AsyncSession,
    *,
    user_id: int,
    token_id: int,
    correlation_id: str | None = None,
) -> tuple[AgentToken, str] | None:
    token = await session.scalar(
        select(AgentToken).where(AgentToken.id == token_id, AgentToken.user_id == user_id).with_for_update()
    )
    if token is None:
        return None
    if token.revoked_at is not None:
        raise ValueError("revoked agent tokens cannot be rotated")
    secret = new_agent_token()
    token.revoked_at = datetime.now(UTC)
    replacement = AgentToken(
        user_id=user_id,
        name=token.name,
        token_hash=hash_agent_token(secret),
        scopes=list(token.scopes),
        expires_at=token.expires_at,
    )
    session.add(replacement)
    await session.flush()
    record_audit(
        session,
        user_id=user_id,
        action="agent_token.rotated",
        resource_type="agent_token",
        resource_id=str(replacement.id),
        outcome="success",
        correlation_id=correlation_id,
        details={"replaced_token_id": token.id, "scopes": replacement.scopes},
    )
    await session.commit()
    await session.refresh(replacement)
    return replacement, secret
