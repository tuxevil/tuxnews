import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import IdentityContext, get_owned_or_404, require_scope
from app.core.permissions import Scope
from app.db.models import Article, Feedback, Source
from app.db.session import get_session
from app.feedback.service import submit_feedback

router = APIRouter(prefix="/api/v1/feedback", tags=["feedback"])
TOPIC_PATTERN = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,199}$")


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: Literal["article", "source", "topic", "quality"] = "article"
    rating: Literal["like", "dislike", "neutral"]
    article_id: int | None = Field(default=None, ge=1)
    source_id: int | None = Field(default=None, ge=1)
    topic_name: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("topic_name")
    @classmethod
    def normalize_topic(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not TOPIC_PATTERN.fullmatch(normalized):
            raise ValueError("topic_name contains unsafe characters")
        return normalized

    @model_validator(mode="after")
    def validate_target(self) -> "FeedbackCreate":
        target_present = sum(value is not None for value in (self.article_id, self.source_id, self.topic_name))
        if target_present != 1:
            raise ValueError("exactly one feedback target is required")
        if self.action_type in {"article", "quality"} and self.article_id is None:
            raise ValueError("article and quality feedback require article_id")
        if self.action_type == "source" and self.source_id is None:
            raise ValueError("source feedback requires source_id")
        if self.action_type == "topic" and self.topic_name is None:
            raise ValueError("topic feedback requires topic_name")
        return self


class FeedbackPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    action_type: str
    rating: str
    article_id: int | None
    source_id: int | None
    topic_name: str | None
    reason: str | None
    supersedes_id: int | None
    is_current: bool


@router.get("", response_model=list[FeedbackPublic])
async def list_current_feedback(
    article_id: list[int] = Query(default=[]),
    identity: IdentityContext = Depends(require_scope(Scope.FEEDBACK_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> list[FeedbackPublic]:
    query = select(Feedback).where(
        Feedback.user_id == identity.user.id,
        Feedback.is_current.is_(True),
    )
    if article_id:
        query = query.where(Feedback.article_id.in_(article_id))
    events = await session.scalars(query.order_by(Feedback.id))
    return [FeedbackPublic.model_validate(event) for event in events]


async def _validate_target(payload: FeedbackCreate, identity: IdentityContext, session: AsyncSession) -> None:
    if payload.article_id is not None:
        await get_owned_or_404(session, Article, payload.article_id, identity)
    if payload.source_id is not None:
        await get_owned_or_404(session, Source, payload.source_id, identity)


@router.post("", response_model=FeedbackPublic, status_code=status.HTTP_201_CREATED)
async def create_feedback(
    payload: FeedbackCreate,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.FEEDBACK_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> FeedbackPublic:
    await _validate_target(payload, identity, session)
    try:
        event = await submit_feedback(
            session,
            tenant=identity.tenant,
            action_type=payload.action_type,
            rating=payload.rating,
            article_id=payload.article_id,
            source_id=payload.source_id,
            topic_name=payload.topic_name,
            reason=payload.reason,
            correlation_id=getattr(request.state, "correlation_id", None),
            actor=identity.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feedback target not found") from exc
    return FeedbackPublic.model_validate(event)


@router.post("/{feedback_id}/undo", response_model=FeedbackPublic)
async def undo_feedback(
    feedback_id: int,
    request: Request,
    identity: IdentityContext = Depends(require_scope(Scope.FEEDBACK_WRITE.value)),
    session: AsyncSession = Depends(get_session),
) -> FeedbackPublic:
    event = await session.scalar(
        select(Feedback).where(Feedback.id == feedback_id, Feedback.user_id == identity.user.id)
    )
    if event is None or not event.is_current:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feedback not found")
    undone = await submit_feedback(
        session,
        tenant=identity.tenant,
        action_type=event.action_type,  # type: ignore[arg-type]
        rating="neutral",
        article_id=event.article_id,
        source_id=event.source_id,
        topic_name=event.topic_name,
        reason="undo",
        correlation_id=getattr(request.state, "correlation_id", None),
        actor=identity.actor,
    )
    return FeedbackPublic.model_validate(undone)
