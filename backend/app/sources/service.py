from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import record_audit
from app.core.config import Settings, get_settings
from app.core.context import TenantContext
from app.db.models import Source
from app.ingestion.http_client import HttpFetchError, SafeHttpClient


async def add_rss_source(
    session: AsyncSession,
    *,
    tenant: TenantContext,
    name: str,
    url: str,
    tags: Sequence[str] = (),
    settings: Settings | None = None,
    correlation_id: str | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> tuple[Source, bool]:
    user_id = tenant.tenant_id
    normalized_name = name.strip()
    normalized_url = url.strip()
    if not normalized_name or len(normalized_name) > 200:
        raise ValueError("source name must contain between 1 and 200 characters")
    parsed = urlsplit(normalized_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source URL is not allowed")
    normalized_tags = list(dict.fromkeys(tag.strip() for tag in tags))
    if len(normalized_tags) > 32 or any(not tag for tag in normalized_tags):
        raise ValueError("source tags are invalid")

    existing = await session.scalar(
        select(Source).where(Source.user_id == user_id, Source.url == normalized_url)
    )
    if existing is not None:
        record_audit(
            session,
            user_id=user_id,
            action="source.added",
            resource_type="source",
            resource_id=str(existing.id),
            outcome="idempotent",
            correlation_id=correlation_id,
            actor_type=actor_type,
            actor_id=actor_id,
            details={"url": normalized_url},
        )
        await session.commit()
        return existing, False

    try:
        async with SafeHttpClient(settings or get_settings()) as client:
            await client.validate_destination(normalized_url)
    except HttpFetchError as exc:
        raise ValueError("source URL is not allowed") from exc

    source = Source(
        user_id=user_id,
        name=normalized_name,
        url=normalized_url,
        source_type="rss",
        tags=normalized_tags,
        is_active=True,
        origin="dynamic",
    )
    session.add(source)
    await session.flush()
    record_audit(
        session,
        user_id=user_id,
        action="source.added",
        resource_type="source",
        resource_id=str(source.id),
        outcome="success",
        correlation_id=correlation_id,
        actor_type=actor_type,
        actor_id=actor_id,
        details={"url": normalized_url, "tags": normalized_tags},
    )
    await session.commit()
    await session.refresh(source)
    return source, True
