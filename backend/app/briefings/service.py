from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import nh3
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.gateway import LLMGateway
from app.audit.service import record_audit
from app.briefings.schemas import BriefingDraft, validate_briefing_output
from app.core.config import Settings, get_settings
from app.core.context import ActorContext, job_context_from_payload
from app.core.quota import quota_guard
from app.db.models import Article, Briefing, BriefingItem, BriefingSchedule, Source, User
from app.db.session import SessionFactory
from app.preferences.settings import UserSettings, resolve_user_settings
from app.ranking.display import load_ranking_context, rank_articles_for_display
from app.usage.service import record_usage_event
from app.usage.types import UsageContext

GENERATION_VERSION = "briefing-v1"
SECURITY_CONTEXT = "UNTRUSTED_EXTERNAL_DATA"


@dataclass(frozen=True)
class BriefingItemView:
    article_id: int
    position: int
    display_rank: float
    provenance_json: dict[str, Any]


@dataclass(frozen=True)
class BriefingView:
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
    items: list[BriefingItemView]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _briefing_window(briefing_date: str, timezone_name: str) -> tuple[datetime, datetime]:
    try:
        local_date = date.fromisoformat(briefing_date)
        timezone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("invalid briefing date or timezone") from exc
    start = datetime.combine(local_date, time.min, tzinfo=timezone)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=timezone)
    return start.astimezone(UTC) - timedelta(hours=24), end.astimezone(UTC)


def _validate_local_time(local_time: str) -> str:
    try:
        parsed = time.fromisoformat(local_time)
    except ValueError as exc:
        raise ValueError("invalid briefing local time") from exc
    if parsed.second or parsed.microsecond:
        raise ValueError("briefing local time must use HH:MM")
    return parsed.strftime("%H:%M")


async def _published_articles(
    session: AsyncSession,
    *,
    user_id: int,
    briefing_date: str,
    timezone_name: str,
    settings: Settings,
    user_settings: UserSettings,
) -> list[Any]:
    window_start, window_end = _briefing_window(briefing_date, timezone_name)
    result = await session.execute(
        select(Article)
        .add_columns(Source.name)
        .join(Source, Source.id == Article.source_id)
        .where(
            Article.user_id == user_id,
            Article.status == "published",
            Article.published_at >= window_start,
            Article.published_at < window_end,
            Source.user_id == user_id,
            Source.is_muted.is_(False),
        )
    )
    rows = list(result)
    context = await load_ranking_context(session, user_id)
    user = await session.get(User, user_id)
    if user is None:
        return []
    return _select_diverse(
        rank_articles_for_display(
            [(row[0], row[1]) for row in rows],
            context=context,
            serendipity=user.serendipity_score,
        ),
        limit=user_settings.briefing_max_items,
    )


def _select_diverse(ranked: Sequence[Any], *, limit: int) -> list[Any]:
    if limit < 1:
        return []
    selected: list[Any] = []
    used_sources: set[int] = set()
    for item in ranked:
        if item.article.source_id in used_sources:
            continue
        selected.append(item)
        used_sources.add(item.article.source_id)
        if len(selected) >= limit:
            return selected
    for item in ranked:
        if item in selected:
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def _clean_text(value: str, max_length: int) -> str:
    return nh3.clean(value, tags=set()).strip()[:max_length].rstrip()


def _fallback_draft(articles: Sequence[Any], briefing_date: str) -> BriefingDraft:
    if not articles:
        return BriefingDraft(
            title=f"Daily briefing / {briefing_date}",
            executive_summary="No published stories matched this briefing window.",
            key_points=["The edition is ready for a later retry when new stories arrive."],
            caveat="This edition contains no external story claims.",
        )
    summaries = [_clean_text(item.article.summary or item.article.title, 500) for item in articles]
    return BriefingDraft(
        title=f"Daily briefing / {briefing_date}",
        executive_summary=_clean_text(" ".join(summaries), 2_000),
        key_points=[_clean_text(item.article.title, 300) for item in articles[:8]],
        caveat="Summaries use the stored article text; verify the linked sources before acting.",
    )


def _external_data(articles: Sequence[Any]) -> str:
    return "\n".join(
        f"[story {index}] source={item.source_name}; title={item.article.title}; "
        f"summary={item.article.summary or ''}; url={item.article.url}"
        for index, item in enumerate(articles, start=1)
    )


async def _generate_draft(
    articles: Sequence[Any],
    *,
    briefing_date: str,
    gateway: LLMGateway,
    settings: Settings,
    profile: str,
) -> tuple[BriefingDraft, str | None]:
    fallback = _fallback_draft(articles, briefing_date)
    if not articles:
        return fallback, None
    try:
        response = await gateway.complete(
            instruction=(
                "Create a neutral executive briefing from the external stories. "
                "Return only the requested strict JSON schema; preserve uncertainty and do not invent facts."
            ),
            external_data=_external_data(articles),
            profile=profile,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "daily_briefing",
                    "strict": True,
                    "schema": BriefingDraft.model_json_schema(),
                },
            },
        )
        if response.used_fallback:
            return fallback, response.error
        return validate_briefing_output(response.content), None
    except Exception as exc:
        return fallback, type(exc).__name__


def _render_markdown(draft: BriefingDraft, articles: Sequence[Any]) -> str:
    lines = [f"# {draft.title}", "", draft.executive_summary, "", "## Key points"]
    lines.extend(f"- {point}" for point in draft.key_points)
    lines.extend(["", f"> {draft.caveat}", "", "## Sources"])
    for item in articles:
        lines.append(f"- {_clean_text(item.article.title, 300)} | {item.source_name} | {item.article.url}")
    return "\n".join(lines)


async def _view(session: AsyncSession, briefing: Briefing) -> BriefingView:
    items = list(
        await session.scalars(
            select(BriefingItem)
            .where(
                BriefingItem.briefing_id == briefing.id,
                BriefingItem.user_id == briefing.user_id,
            )
            .order_by(BriefingItem.position)
        )
    )
    return BriefingView(
        id=briefing.id,
        briefing_date=briefing.briefing_date,
        local_time=briefing.local_time,
        timezone=briefing.timezone,
        title=briefing.title,
        content_markdown=briefing.content_markdown,
        status=briefing.status,
        security_context=briefing.security_context,
        generation_version=briefing.generation_version,
        revision=briefing.revision,
        checksum=briefing.checksum,
        error_message=briefing.error_message,
        items=[
            BriefingItemView(
                article_id=item.article_id,
                position=item.position,
                display_rank=item.display_rank,
                provenance_json=dict(item.provenance_json),
            )
            for item in items
        ],
    )


async def list_briefing_views(session: AsyncSession, user_id: int, limit: int = 20) -> list[BriefingView]:
    briefings = list(
        await session.scalars(
            select(Briefing)
            .where(Briefing.user_id == user_id)
            .order_by(Briefing.briefing_date.desc(), Briefing.local_time.desc(), Briefing.id.desc())
            .limit(limit)
        )
    )
    return [await _view(session, briefing) for briefing in briefings]


async def get_briefing_view(session: AsyncSession, *, user_id: int, briefing_id: int) -> BriefingView | None:
    briefing = await session.scalar(select(Briefing).where(Briefing.id == briefing_id, Briefing.user_id == user_id))
    return await _view(session, briefing) if briefing is not None else None


async def get_today_briefing_view(
    session: AsyncSession,
    *,
    user_id: int,
    timezone_name: str = "UTC",
) -> BriefingView | None:
    try:
        today = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc
    briefing = await session.scalar(
        select(Briefing)
        .where(Briefing.user_id == user_id, Briefing.briefing_date == today)
        .order_by(Briefing.local_time.desc(), Briefing.id.desc())
        .limit(1)
    )
    return await _view(session, briefing) if briefing is not None else None


async def get_or_create_schedule(
    session: AsyncSession,
    user_id: int,
    *,
    actor: ActorContext | None = None,
    correlation_id: str | None = None,
) -> BriefingSchedule:
    schedule = await session.scalar(select(BriefingSchedule).where(BriefingSchedule.user_id == user_id))
    if schedule is None:
        schedule = BriefingSchedule(user_id=user_id, local_time="08:00", timezone="UTC", is_active=True)
        session.add(schedule)
        record_audit(
            session,
            user_id=user_id,
            action="briefing.schedule.created",
            resource_type="briefing_schedule",
            resource_id=str(user_id),
            outcome="success",
            correlation_id=correlation_id,
            actor=actor,
        )
        await session.commit()
        await session.refresh(schedule)
    return schedule


async def update_schedule(
    session: AsyncSession,
    *,
    user_id: int,
    local_time: str,
    timezone_name: str,
    is_active: bool,
    actor: ActorContext | None = None,
    correlation_id: str | None = None,
) -> BriefingSchedule:
    normalized_time = _validate_local_time(local_time)
    _briefing_window(date.today().isoformat(), timezone_name)
    schedule = await session.scalar(
        select(BriefingSchedule).where(BriefingSchedule.user_id == user_id).with_for_update()
    )
    if schedule is None:
        schedule = BriefingSchedule(user_id=user_id)
        session.add(schedule)
    schedule.local_time = normalized_time
    schedule.timezone = timezone_name
    schedule.is_active = is_active
    record_audit(
        session,
        user_id=user_id,
        action="briefing.schedule.updated",
        resource_type="briefing_schedule",
        resource_id=str(user_id),
        outcome="success",
        correlation_id=correlation_id,
        actor=actor,
        details={"local_time": normalized_time, "timezone": timezone_name, "is_active": is_active},
    )
    await session.commit()
    await session.refresh(schedule)
    return schedule


@quota_guard(
    scope="content:write",
    operation="worker.generate_briefing",
    provider="llm",
    payload_position=5,
)
async def generate_briefing(
    ctx: dict[str, Any],
    user_id: int,
    briefing_date: str,
    local_time: str,
    timezone_name: str,
    regenerate: bool = False,
    job_payload: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    settings = get_settings()
    payload = job_payload if job_payload is not None else ctx
    job = job_context_from_payload(payload)
    if job is None or job.tenant.tenant_id != user_id:
        return {"status": "rejected", "user_id": user_id, "reason": "tenant context required"}
    tenant = job.tenant
    normalized_time = _validate_local_time(local_time)
    _briefing_window(briefing_date, timezone_name)
    async with SessionFactory() as session:
        user = await session.scalar(select(User).where(User.id == tenant.tenant_id, User.is_active.is_(True)))
        if user is None:
            return {"status": "missing", "user_id": user_id}
        user_settings = resolve_user_settings(user.settings_json, settings)
        existing = await session.scalar(
            select(Briefing).where(
                Briefing.user_id == user_id,
                Briefing.briefing_date == briefing_date,
                Briefing.local_time == normalized_time,
            )
        )
        if existing is not None and not regenerate:
            return {"status": existing.status, "briefing_id": existing.id, "revision": existing.revision}
        articles = await _published_articles(
            session,
            user_id=user_id,
            briefing_date=briefing_date,
            timezone_name=timezone_name,
            settings=settings,
            user_settings=user_settings,
        )
        gateway = ctx.get("gateway")
        if gateway is None:
            gateway = LLMGateway(
                settings,
                usage_context=UsageContext(
                    tenant_id=job.tenant.tenant_id,
                    actor_type=job.actor.actor_type,
                    actor_id=job.actor.actor_id,
                    operation="briefing.generate",
                    correlation_id=job.actor.correlation_id,
                ),
                usage_recorder=record_usage_event,
            )
        draft, generation_error = await _generate_draft(
            articles,
            briefing_date=briefing_date,
            gateway=gateway,
            settings=settings,
            profile=user_settings.llm_profile,
        )
        content = _render_markdown(draft, articles)
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if existing is None:
            briefing = Briefing(
                user_id=user_id,
                briefing_date=briefing_date,
                local_time=normalized_time,
                timezone=timezone_name,
                title=draft.title,
                content_markdown=content,
                status="ready",
                security_context=SECURITY_CONTEXT,
                generation_version=settings.briefing_generation_version or GENERATION_VERSION,
                revision=1,
                checksum=checksum,
                error_message=generation_error,
            )
            session.add(briefing)
            await session.flush()
        else:
            briefing = existing
            briefing.title = draft.title
            briefing.content_markdown = content
            briefing.status = "ready"
            briefing.security_context = SECURITY_CONTEXT
            briefing.generation_version = settings.briefing_generation_version or GENERATION_VERSION
            briefing.revision = (briefing.revision or 0) + 1
            briefing.checksum = checksum
            briefing.error_message = generation_error
            await session.execute(delete(BriefingItem).where(BriefingItem.briefing_id == briefing.id))
            await session.flush()
        for position, item in enumerate(articles, start=1):
            session.add(
                BriefingItem(
                    user_id=user_id,
                    briefing_id=briefing.id,
                    article_id=item.article.id,
                    position=position,
                    display_rank=item.result.display_rank,
                    provenance_json={
                        "article_id": item.article.id,
                        "title": item.article.title,
                        "url": str(item.article.url),
                        "source_id": item.article.source_id,
                        "source_name": item.source_name,
                        "published_at": item.article.published_at.isoformat() if item.article.published_at else None,
                        "security_context": SECURITY_CONTEXT,
                    },
                )
            )
        record_audit(
            session,
            user_id=user_id,
            action="briefing.regenerated" if existing is not None else "briefing.generated",
            resource_type="briefing",
            resource_id=str(briefing.id),
            outcome="fallback" if generation_error is not None else "success",
            correlation_id=job.actor.correlation_id,
            actor_type=job.actor.actor_type,
            actor_id=job.actor.actor_id,
            details={
                "items": len(articles),
                "revision": briefing.revision,
                "used_fallback": generation_error is not None,
                "llm_profile": user_settings.llm_profile,
                "settings_version": user_settings.version,
            },
        )
        await session.commit()
        return {
            "status": briefing.status,
            "briefing_id": briefing.id,
            "revision": briefing.revision,
            "items": len(articles),
            "used_fallback": generation_error is not None,
        }
