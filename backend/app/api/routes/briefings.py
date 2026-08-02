from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import IdentityContext, require_scope
from app.briefings.service import (
    BriefingView,
    generate_briefing,
    get_briefing_view,
    get_or_create_schedule,
    list_briefing_views,
    update_schedule,
)
from app.core.permissions import Scope
from app.db.session import get_session

router = APIRouter(prefix="/api/v1/briefings", tags=["briefings"])


class BriefingItemPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    article_id: int
    position: int
    display_rank: float
    provenance_json: dict[str, Any]


class BriefingPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    briefing_date: str
    local_time: str
    timezone: str
    title: str
    content_markdown: str
    status: str
    security_context: str
    generation_version: str
    revision: int
    checksum: str | None
    error_message: str | None
    items: list[BriefingItemPublic]


class BriefingGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    briefing_date: date
    local_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    timezone: str = Field(min_length=1, max_length=64)
    regenerate: bool = False

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        return value


class BriefingSchedulePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    local_time: str
    timezone: str
    is_active: bool


class BriefingScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    timezone: str = Field(min_length=1, max_length=64)
    is_active: bool = True

    @field_validator("timezone")
    @classmethod
    def validate_schedule_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        return value


def _response(view: BriefingView) -> BriefingPublic:
    return BriefingPublic.model_validate(view)


def _correlation_id(request: Request) -> str | None:
    value = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, str) else None


@router.get("", response_model=list[BriefingPublic])
async def list_briefings(
    limit: int = Query(default=20, ge=1, le=100),
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> list[BriefingPublic]:
    return [_response(view) for view in await list_briefing_views(session, identity.user.id, limit)]


@router.get("/schedule", response_model=BriefingSchedulePublic)
async def get_briefing_schedule(
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> BriefingSchedulePublic:
    schedule = await get_or_create_schedule(
        session,
        identity.user.id,
        actor=identity.actor,
        correlation_id=_correlation_id(request),
    )
    return BriefingSchedulePublic.model_validate(schedule)


@router.put("/schedule", response_model=BriefingSchedulePublic)
async def put_briefing_schedule(
    payload: BriefingScheduleUpdate,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.PREFERENCES_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> BriefingSchedulePublic:
    try:
        schedule = await update_schedule(
            session,
            user_id=identity.user.id,
            local_time=payload.local_time,
            timezone_name=payload.timezone,
            is_active=payload.is_active,
            actor=identity.actor,
            correlation_id=_correlation_id(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return BriefingSchedulePublic.model_validate(schedule)


@router.get("/today", response_model=BriefingPublic)
async def get_today_briefing(
    timezone: str = Query(default="UTC", min_length=1, max_length=64),
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> BriefingPublic:
    try:
        today = datetime.now(ZoneInfo(timezone)).date().isoformat()
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="unknown timezone") from exc
    views = await list_briefing_views(session, identity.user.id, 100)
    for view in views:
        if view.briefing_date == today:
            return _response(view)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="today briefing not found")


@router.post("/generate", response_model=BriefingPublic, status_code=status.HTTP_201_CREATED)
async def generate_briefing_route(
    payload: BriefingGenerateRequest,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> BriefingPublic:
    result = await generate_briefing(
        {
            "tenant_id": identity.tenant.tenant_id,
            "actor_type": identity.actor.actor_type,
            "actor_id": identity.actor.actor_id,
            "correlation_id": _correlation_id(request),
            "_quota_checked": True,
        },
        identity.user.id,
        payload.briefing_date.isoformat(),
        payload.local_time,
        payload.timezone,
        payload.regenerate,
    )
    briefing_id = result.get("briefing_id")
    if not isinstance(briefing_id, int):
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="briefing was not persisted")
    view = await get_briefing_view(session, user_id=identity.user.id, briefing_id=briefing_id)
    if view is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="briefing was not persisted")
    return _response(view)


@router.get("/{briefing_id}", response_model=BriefingPublic)
async def get_briefing(
    briefing_id: int,
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> BriefingPublic:
    view = await get_briefing_view(session, user_id=identity.user.id, briefing_id=briefing_id)
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="briefing not found")
    return _response(view)


@router.post("/{briefing_id}/regenerate", response_model=BriefingPublic)
async def regenerate_briefing(
    briefing_id: int,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> BriefingPublic:
    current = await get_briefing_view(session, user_id=identity.user.id, briefing_id=briefing_id)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="briefing not found")
    await generate_briefing(
        {
            "tenant_id": identity.tenant.tenant_id,
            "actor_type": identity.actor.actor_type,
            "actor_id": identity.actor.actor_id,
            "correlation_id": _correlation_id(request),
            "_quota_checked": True,
        },
        identity.user.id,
        current.briefing_date,
        current.local_time,
        current.timezone,
        True,
    )
    refreshed = await get_briefing_view(session, user_id=identity.user.id, briefing_id=briefing_id)
    if refreshed is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="briefing was not persisted")
    return _response(refreshed)
