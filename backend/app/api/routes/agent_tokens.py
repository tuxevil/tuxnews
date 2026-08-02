from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_tokens.service import (
    create_agent_token,
    list_agent_tokens,
    revoke_agent_token,
    rotate_agent_token,
)
from app.api.deps import IdentityContext, require_scope
from app.core.permissions import AgentScope, Scope
from app.db.models import AgentToken
from app.db.session import get_session

router = APIRouter(prefix="/api/v1/agent-tokens", tags=["agent-tokens"])


class AgentTokenCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    scopes: list[AgentScope] = Field(min_length=1, max_length=4)
    expires_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be empty")
        return normalized

    @field_validator("scopes")
    @classmethod
    def normalize_scopes(cls, value: list[AgentScope]) -> list[AgentScope]:
        return list(dict.fromkeys(value))

    @field_validator("expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return value.astimezone(UTC) if value is not None else None


class AgentTokenPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    scopes: list[AgentScope]
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class AgentTokenCreated(AgentTokenPublic):
    token: str


def _correlation_id(request: Request) -> str | None:
    value = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, str) else None


def _public(token: AgentToken) -> AgentTokenPublic:
    return AgentTokenPublic.model_validate(token)


@router.post("", response_model=AgentTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(
    payload: AgentTokenCreateRequest,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.AGENT_TOKENS_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> AgentTokenCreated:
    try:
        token, secret = await create_agent_token(
            session,
            user_id=identity.user.id,
            name=payload.name,
            scopes=[scope.value for scope in payload.scopes],
            expires_at=payload.expires_at,
            correlation_id=_correlation_id(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return AgentTokenCreated.model_validate({**_public(token).model_dump(), "token": secret})


@router.get("", response_model=list[AgentTokenPublic])
async def get_tokens(
    identity: IdentityContext = Depends(require_scope(Scope.AGENT_TOKENS_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> list[AgentTokenPublic]:
    return [_public(token) for token in await list_agent_tokens(session, user_id=identity.user.id)]


@router.post("/{token_id}/rotate", response_model=AgentTokenCreated)
async def rotate_token(
    token_id: int,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.AGENT_TOKENS_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> AgentTokenCreated:
    try:
        result = await rotate_agent_token(
            session,
            user_id=identity.user.id,
            token_id=token_id,
            correlation_id=_correlation_id(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent token not found")
    token, secret = result
    return AgentTokenCreated.model_validate({**_public(token).model_dump(), "token": secret})


@router.delete("/{token_id}", response_model=AgentTokenPublic)
async def revoke_token(
    token_id: int,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.AGENT_TOKENS_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> AgentTokenPublic:
    token = await revoke_agent_token(
        session,
        user_id=identity.user.id,
        token_id=token_id,
        correlation_id=_correlation_id(request),
    )
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent token not found")
    return _public(token)
