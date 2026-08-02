import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import IdentityContext, require_scope
from app.core.permissions import Scope
from app.db.models import User
from app.db.session import get_session
from app.preferences.service import (
    get_user_settings,
    list_sources,
    list_topics,
    profile_version,
    reset_source,
    reset_topic,
    update_serendipity,
    update_source_mute,
    update_topic,
    update_user_settings,
)
from app.preferences.settings import UserSettings, UserSettingsUpdate
from app.ranking.display import MAX_SERENDIPITY, MIN_SERENDIPITY

router = APIRouter(prefix="/api/v1/preferences", tags=["preferences"])
TOPIC_PATTERN = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,199}$")


class TopicPreferencePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    topic_name: str
    weight_score: float
    preference_version: int


class SourcePreferencePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: HttpUrl
    origin: str
    is_active: bool
    is_muted: bool
    reputation_score: float
    reputation_version: int
    preference_version: int


class TopicPreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weight_score: float = Field(ge=-1.0, le=1.0)


class SourcePreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_muted: bool


class RankingPreferencePublic(BaseModel):
    serendipity: float = Field(ge=MIN_SERENDIPITY, le=MAX_SERENDIPITY)
    preference_version: int


class RankingPreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serendipity: float = Field(ge=MIN_SERENDIPITY, le=MAX_SERENDIPITY)


class PreferenceProfilePublic(BaseModel):
    topics: list[TopicPreferencePublic]
    sources: list[SourcePreferencePublic]
    ranking: RankingPreferencePublic
    profile_version: int


class ResetConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]


def _normalize_topic(topic_name: str) -> str:
    normalized = topic_name.strip().lower()
    if not TOPIC_PATTERN.fullmatch(normalized):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid topic name")
    return normalized


def _confirm(payload: ResetConfirmation | None, confirmed: bool) -> None:
    if confirmed or (payload is not None and payload.confirm):
        return
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="explicit confirmation is required")


def _correlation_id(request: Request) -> str | None:
    value = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, str) else None


def _ranking(user: User) -> RankingPreferencePublic:
    return RankingPreferencePublic(
        serendipity=user.serendipity_score,
        preference_version=user.ranking_preference_version,
    )


async def _profile(session: AsyncSession, user_id: int) -> PreferenceProfilePublic:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    topics = await list_topics(session, user_id)
    sources = await list_sources(session, user_id)
    return PreferenceProfilePublic(
        topics=[TopicPreferencePublic.model_validate(topic) for topic in topics],
        sources=[SourcePreferencePublic.model_validate(source) for source in sources],
        ranking=_ranking(user),
        profile_version=profile_version(topics, sources, user.ranking_preference_version),
    )


@router.get("", response_model=PreferenceProfilePublic)
async def get_profile(
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> PreferenceProfilePublic:
    return await _profile(session, identity.user.id)


@router.get("/ranking", response_model=RankingPreferencePublic)
async def get_ranking_preference(
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_READ.value)),
) -> RankingPreferencePublic:
    return _ranking(identity.user)


@router.get("/settings", response_model=UserSettings)
async def get_settings_profile(
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> UserSettings:
    settings = await get_user_settings(session, user_id=identity.user.id)
    if settings is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return settings


@router.patch("/settings", response_model=UserSettings)
async def edit_settings_profile(
    payload: UserSettingsUpdate,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.PREFERENCES_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> UserSettings:
    try:
        settings = await update_user_settings(
            session,
            user_id=identity.user.id,
            update=payload,
            actor=identity.actor,
            correlation_id=_correlation_id(request),
        )
    except ValueError as exc:
        code = status.HTTP_409_CONFLICT if "version conflict" in str(exc) else status.HTTP_422_UNPROCESSABLE_CONTENT
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    if settings is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return settings


@router.patch("/ranking", response_model=RankingPreferencePublic)
async def edit_ranking_preference(
    payload: RankingPreferenceUpdate,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.PREFERENCES_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> RankingPreferencePublic:
    user = await update_serendipity(
        session,
        user_id=identity.user.id,
        serendipity=payload.serendipity,
        actor=identity.actor,
        correlation_id=_correlation_id(request),
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return _ranking(user)


@router.get("/topics", response_model=list[TopicPreferencePublic])
async def get_topics(
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> list[TopicPreferencePublic]:
    return [TopicPreferencePublic.model_validate(topic) for topic in await list_topics(session, identity.user.id)]


@router.patch("/topics/{topic_name}", response_model=TopicPreferencePublic)
async def edit_topic(
    topic_name: str,
    payload: TopicPreferenceUpdate,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.PREFERENCES_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> TopicPreferencePublic:
    topic = await update_topic(
        session,
        user_id=identity.user.id,
        topic_name=_normalize_topic(topic_name),
        weight_score=payload.weight_score,
        actor=identity.actor,
        correlation_id=_correlation_id(request),
    )
    return TopicPreferencePublic.model_validate(topic)


@router.post("/topics/{topic_name}/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_topic_preference(
    topic_name: str,
    request: Request,
    payload: ResetConfirmation | None = None,
    confirm: bool = Query(default=False),
    identity: IdentityContext = Depends(require_scope(Scope.PREFERENCES_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> None:
    _confirm(payload, confirm)
    reset = await reset_topic(
        session,
        user_id=identity.user.id,
        topic_name=_normalize_topic(topic_name),
        actor=identity.actor,
        correlation_id=_correlation_id(request),
    )
    if not reset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="topic preference not found")


@router.delete("/topics/{topic_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic_preference(
    topic_name: str,
    request: Request,
    payload: ResetConfirmation | None = None,
    confirm: bool = Query(default=False),
    identity: IdentityContext = Depends(require_scope(Scope.PREFERENCES_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> None:
    _confirm(payload, confirm)
    reset = await reset_topic(
        session,
        user_id=identity.user.id,
        topic_name=_normalize_topic(topic_name),
        actor=identity.actor,
        correlation_id=_correlation_id(request),
    )
    if not reset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="topic preference not found")


@router.get("/sources", response_model=list[SourcePreferencePublic])
async def get_source_preferences(
    identity: IdentityContext = Depends(require_scope(Scope.CONTENT_READ.value)),
    session: AsyncSession = Depends(get_session),
) -> list[SourcePreferencePublic]:
    return [
        SourcePreferencePublic.model_validate(source)
        for source in await list_sources(session, identity.user.id)
    ]


@router.patch("/sources/{source_id}", response_model=SourcePreferencePublic)
async def edit_source_preference(
    source_id: int,
    payload: SourcePreferenceUpdate,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.PREFERENCES_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> SourcePreferencePublic:
    source = await update_source_mute(
        session,
        user_id=identity.user.id,
        source_id=source_id,
        is_muted=payload.is_muted,
        actor=identity.actor,
        correlation_id=_correlation_id(request),
    )
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source preference not found")
    return SourcePreferencePublic.model_validate(source)


@router.post("/sources/{source_id}/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_source_preference(
    source_id: int,
    request: Request,
    payload: ResetConfirmation | None = None,
    confirm: bool = Query(default=False),
    identity: IdentityContext = Depends(require_scope(Scope.PREFERENCES_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> None:
    _confirm(payload, confirm)
    reset = await reset_source(
        session,
        user_id=identity.user.id,
        source_id=source_id,
        actor=identity.actor,
        correlation_id=_correlation_id(request),
    )
    if not reset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source preference not found")
