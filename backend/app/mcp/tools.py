from __future__ import annotations

from typing import Annotated

from fastmcp import Context
from fastmcp.dependencies import CurrentAccessToken
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from mcp.types import ToolAnnotations
from pydantic import Field

from app.core.permissions import AgentScope
from app.discovery.service import search_articles as search_external_articles
from app.mcp.schemas import (
    ArchiveResponse,
    BriefingItemResponse,
    DailyBriefingResponse,
    RatingResponse,
    SearchArticle,
    SearchArticlesResponse,
    SourceResponse,
)
from app.mcp.security import actor_from_token, enforce_quota, require_confirmation, require_scope
from app.mcp.server import mcp
from app.mcp.use_cases import (
    add_source,
    get_daily_briefing,
    get_latest_archive,
    rate_article,
    save_article,
)


@mcp.tool(
    name="search_articles",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    timeout=30,
)
async def search_articles(
    query: Annotated[str, Field(min_length=1, max_length=300)],
    max_results: Annotated[int, Field(ge=1, le=20)] = 10,
    *,
    token: AccessToken = CurrentAccessToken(),
    ctx: Context,
) -> SearchArticlesResponse:
    """Search external articles and return untrusted candidates with provenance."""

    require_scope(token, AgentScope.NEWS_READ.value)
    actor = actor_from_token(token, ctx)
    await enforce_quota(
        actor,
        scope=AgentScope.NEWS_READ.value,
        operation="mcp.search_articles",
        provider="search",
    )
    try:
        result = await search_external_articles(query, max_results=max_results)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return SearchArticlesResponse(
        query=result.query,
        articles=[
            SearchArticle(
                title=candidate.title,
                snippet=candidate.snippet,
                url=candidate.url,
                published_at=candidate.published_at,
                provider=candidate.provider,
                provider_version=candidate.provider_version,
            )
            for candidate in result.candidates
        ],
        errors=list(result.errors),
    )


@mcp.tool(
    name="get_daily_briefing",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
async def get_daily_briefing_tool(
    timezone: Annotated[str, Field(min_length=1, max_length=64)] = "UTC",
    *,
    token: AccessToken = CurrentAccessToken(),
    ctx: Context,
) -> DailyBriefingResponse:
    """Read today's persisted briefing for the authenticated user."""

    require_scope(token, AgentScope.NEWS_READ.value)
    actor = actor_from_token(token, ctx)
    await enforce_quota(actor, scope=AgentScope.NEWS_READ.value, operation="mcp.get_daily_briefing")
    try:
        briefing = await get_daily_briefing(actor.tenant, timezone_name=timezone)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if briefing is None:
        return DailyBriefingResponse(found=False)
    return DailyBriefingResponse(
        found=True,
        briefing_id=briefing.id,
        briefing_date=briefing.briefing_date,
        local_time=briefing.local_time,
        timezone=briefing.timezone,
        title=briefing.title,
        content_markdown=briefing.content_markdown,
        status=briefing.status,
        revision=briefing.revision,
        items=[
            BriefingItemResponse(
                article_id=item.article_id,
                position=item.position,
                display_rank=item.display_rank,
                provenance=dict(item.provenance_json),
            )
            for item in briefing.items
        ],
    )


@mcp.tool(
    name="save_article_md",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    timeout=30,
)
async def save_article_md(
    article_id: Annotated[int, Field(ge=1)],
    confirm: bool = False,
    *,
    token: AccessToken = CurrentAccessToken(),
    ctx: Context,
) -> ArchiveResponse:
    """Save an owned article to the confined Markdown archive after confirmation."""

    require_scope(token, AgentScope.ARCHIVE_WRITE.value)
    require_confirmation(confirm)
    actor = actor_from_token(token, ctx)
    await enforce_quota(actor, scope=AgentScope.ARCHIVE_WRITE.value, operation="mcp.save_article_md")
    try:
        export = await save_article(
            actor.tenant,
            article_id=article_id,
            correlation_id=actor.correlation_id,
            actor_type=actor.actor_type,
            actor_id=actor.actor_id,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if export is None:
        raise ToolError("article not found")
    return ArchiveResponse(
        found=True,
        article_id=article_id,
        path=export.path,
        checksum=export.checksum,
    )


@mcp.tool(
    name="add_rss_source",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    timeout=30,
)
async def add_rss_source_tool(
    name: Annotated[str, Field(min_length=1, max_length=200)],
    url: Annotated[str, Field(min_length=1, max_length=2048)],
    tags: Annotated[list[str], Field(max_length=32)] | None = None,
    confirm: bool = False,
    *,
    token: AccessToken = CurrentAccessToken(),
    ctx: Context,
) -> SourceResponse:
    """Add an RSS source for the authenticated user after confirmation."""

    require_scope(token, AgentScope.SOURCES_WRITE.value)
    require_confirmation(confirm)
    actor = actor_from_token(token, ctx)
    await enforce_quota(actor, scope=AgentScope.SOURCES_WRITE.value, operation="mcp.add_rss_source")
    try:
        source, created = await add_source(
            actor.tenant,
            name=name,
            url=url,
            tags=tags or (),
            correlation_id=actor.correlation_id,
            actor_type=actor.actor_type,
            actor_id=actor.actor_id,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return SourceResponse(
        id=source.id,
        name=source.name,
        url=source.url,
        tags=list(source.tags),
        created=created,
    )


@mcp.tool(
    name="rate_article",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def rate_article_tool(
    article_id: Annotated[int, Field(ge=1)],
    rating: Annotated[str, Field(pattern="^(like|dislike|neutral)$")],
    confirm: bool = False,
    *,
    token: AccessToken = CurrentAccessToken(),
    ctx: Context,
) -> RatingResponse:
    """Rate an owned article after explicit confirmation."""

    require_scope(token, AgentScope.FEEDBACK_WRITE.value)
    require_confirmation(confirm)
    actor = actor_from_token(token, ctx)
    await enforce_quota(actor, scope=AgentScope.FEEDBACK_WRITE.value, operation="mcp.rate_article")
    feedback = await rate_article(
        actor.tenant,
        article_id=article_id,
        rating=rating,  # type: ignore[arg-type]
        correlation_id=actor.correlation_id,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
    )
    if feedback is None:
        raise ToolError("article not found")
    return RatingResponse(
        feedback_id=feedback.id,
        article_id=article_id,
        rating=feedback.rating,
        is_current=feedback.is_current,
    )


@mcp.resource(
    "news://briefing/today",
    name="today_briefing",
    description="Today's persisted briefing for the authenticated user.",
    mime_type="application/json",
    annotations={"readOnlyHint": True},
)
async def briefing_today(
    *,
    token: AccessToken = CurrentAccessToken(),
    ctx: Context,
) -> DailyBriefingResponse:
    """Expose today's briefing as data, never as executable instructions."""

    require_scope(token, AgentScope.NEWS_READ.value)
    actor = actor_from_token(token, ctx)
    await enforce_quota(actor, scope=AgentScope.NEWS_READ.value, operation="mcp.briefing_today")
    try:
        briefing = await get_daily_briefing(actor.tenant, timezone_name="UTC")
    except ValueError as exc:
        raise ToolError("authenticated identity is invalid") from exc
    if briefing is None:
        return DailyBriefingResponse(found=False)
    return DailyBriefingResponse(
        found=True,
        briefing_id=briefing.id,
        briefing_date=briefing.briefing_date,
        local_time=briefing.local_time,
        timezone=briefing.timezone,
        title=briefing.title,
        content_markdown=briefing.content_markdown,
        status=briefing.status,
        revision=briefing.revision,
        items=[
            BriefingItemResponse(
                article_id=item.article_id,
                position=item.position,
                display_rank=item.display_rank,
                provenance=dict(item.provenance_json),
            )
            for item in briefing.items
        ],
    )


@mcp.resource(
    "news://archive/latest",
    name="latest_archive",
    description="Latest confined Markdown archive export for the authenticated user.",
    mime_type="application/json",
    annotations={"readOnlyHint": True},
)
async def archive_latest(
    *,
    token: AccessToken = CurrentAccessToken(),
    ctx: Context,
) -> ArchiveResponse:
    """Expose archived Markdown as untrusted content without executing it."""

    require_scope(token, AgentScope.NEWS_READ.value)
    actor = actor_from_token(token, ctx)
    await enforce_quota(actor, scope=AgentScope.NEWS_READ.value, operation="mcp.archive_latest")
    try:
        archive = await get_latest_archive(actor.tenant)
    except ValueError as exc:
        raise ToolError("archive resource is unavailable") from exc
    if archive is None:
        return ArchiveResponse(found=False)
    return ArchiveResponse(
        found=True,
        article_id=archive.article.id,
        title=archive.article.title,
        source_name=archive.source_name,
        path=archive.export.path,
        checksum=archive.export.checksum,
        content_markdown=archive.content_markdown,
    )
