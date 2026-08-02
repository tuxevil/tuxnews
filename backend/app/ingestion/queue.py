from __future__ import annotations

from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import ActorContext
from app.db.models import IngestionRun, Source


async def enqueue_source_ingestion(
    session: AsyncSession,
    *,
    source: Source,
    actor: ActorContext,
) -> int:
    """Create an ingestion run for a source and enqueue the ARQ job."""

    run = IngestionRun(
        user_id=source.user_id,
        source_id=source.id,
        status="running",
    )
    session.add(run)
    await session.flush()
    settings = get_settings()
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job(
            "ingest_source",
            run.id,
            {
                "tenant_id": source.user_id,
                "actor_type": actor.actor_type,
                "actor_id": actor.actor_id,
                "correlation_id": actor.correlation_id,
            },
        )
    finally:
        await pool.aclose()
    return run.id
