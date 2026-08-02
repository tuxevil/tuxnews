import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select

from app.audit.service import record_audit
from app.briefings.service import generate_briefing
from app.core.config import get_settings
from app.core.context import ActorContext, TenantContext, serialize_job_context
from app.db.models import Briefing, BriefingSchedule, DiscoveryRun, IngestionRun, Source, User
from app.db.session import SessionFactory
from app.discovery.jobs import discover_user
from app.ingestion.jobs import ingest_discovered_article, ingest_source, reconcile_cluster
from app.ingestion.queue import enqueue_source_ingestion
from app.observability import log_event, metrics

logger = logging.getLogger(__name__)
WORKER_HEARTBEAT_KEY = "tuxnews:health:worker"
SCHEDULER_ACTOR_TYPE = "scheduler"
SCHEDULER_ACTOR_ID = "scheduler"


async def _write_heartbeat(ctx: object) -> None:
    if not isinstance(ctx, dict):
        return
    redis = ctx.get("redis")
    if redis is None:
        return
    try:
        await redis.set(WORKER_HEARTBEAT_KEY, str(time.time()), ex=60)
    except Exception as exc:
        log_event(logger, "worker.heartbeat_failed", level=logging.WARNING, error_type=type(exc).__name__)


async def startup(ctx: object) -> None:
    """Worker lifecycle hook reserved for dependency initialization."""
    await _write_heartbeat(ctx)


async def shutdown(ctx: object) -> None:
    """Worker lifecycle hook reserved for dependency cleanup."""
    if isinstance(ctx, dict):
        redis = ctx.get("redis")
        if redis is not None:
            try:
                await redis.delete(WORKER_HEARTBEAT_KEY)
            except Exception as exc:
                log_event(
                    logger,
                    "worker.shutdown_cleanup_failed",
                    level=logging.WARNING,
                    error_type=type(exc).__name__,
                )


async def heartbeat(ctx: dict) -> None:
    """Keep the worker executable before the ingestion jobs are registered."""
    timer = metrics.timer("worker.heartbeat")
    try:
        await _write_heartbeat(ctx)
        metrics.set_gauge("worker.last_heartbeat_unix", time.time())
    except Exception:
        timer.finish(success=False)
        raise
    timer.finish(success=True)


def _scheduler_actor(user_id: int) -> ActorContext:
    return ActorContext(
        tenant=TenantContext(user_id),
        actor_type=SCHEDULER_ACTOR_TYPE,
        actor_id=SCHEDULER_ACTOR_ID,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _schedule_sources(
    session: Any,
    redis: Any,
    *,
    now: datetime,
) -> int:
    settings = get_settings()
    cutoff = now - timedelta(seconds=settings.ingestion_poll_interval_seconds)
    sources = list(
        await session.scalars(
            select(Source)
            .join(User, User.id == Source.user_id)
            .where(
                Source.is_active.is_(True),
                Source.origin != "discovery",
                User.is_active.is_(True),
            )
            .order_by(Source.id)
        )
    )
    scheduled = 0
    for source in sources:
        if source.last_fetched_at is not None and _utc(source.last_fetched_at) >= cutoff:
            continue
        recent_run = await session.scalar(
            select(IngestionRun.id)
            .where(
                IngestionRun.source_id == source.id,
                IngestionRun.user_id == source.user_id,
                IngestionRun.created_at >= cutoff,
                IngestionRun.status.in_(["running", "retrying"]),
            )
            .limit(1)
        )
        if recent_run is not None:
            continue
        actor = _scheduler_actor(source.user_id)
        run_id = await enqueue_source_ingestion(session, source=source, actor=actor, pool=redis)
        record_audit(
            session,
            user_id=source.user_id,
            action="ingestion.scheduled",
            resource_type="ingestion_run",
            resource_id=str(run_id),
            outcome="accepted",
            actor=actor,
            details={"source_id": source.id, "trigger": "scheduler"},
        )
        await session.commit()
        scheduled += 1
    return scheduled


async def _schedule_discovery(session: Any, redis: Any, *, now: datetime) -> int:
    slot_key = now.strftime("%Y-%m-%dT%H")
    users = list(await session.scalars(select(User).where(User.is_active.is_(True)).order_by(User.id)))
    scheduled = 0
    for user in users:
        existing = await session.scalar(
            select(DiscoveryRun.id).where(
                DiscoveryRun.user_id == user.id,
                DiscoveryRun.slot_key == slot_key,
            )
        )
        if existing is not None:
            continue
        actor = _scheduler_actor(user.id)
        await redis.enqueue_job(
            "discover_user",
            user.id,
            slot_key,
            serialize_job_context(actor),
            _job_id=f"discovery:{user.id}:{slot_key}",
        )
        record_audit(
            session,
            user_id=user.id,
            action="discovery.scheduled",
            resource_type="discovery_run",
            resource_id=slot_key,
            outcome="accepted",
            actor=actor,
            details={"slot_key": slot_key, "trigger": "scheduler"},
        )
        await session.commit()
        scheduled += 1
    return scheduled


async def _schedule_briefings(session: Any, redis: Any, *, now: datetime) -> int:
    schedules = list(
        await session.scalars(
            select(BriefingSchedule)
            .join(User, User.id == BriefingSchedule.user_id)
            .where(BriefingSchedule.is_active.is_(True), User.is_active.is_(True))
            .order_by(BriefingSchedule.id)
        )
    )
    scheduled = 0
    for schedule in schedules:
        try:
            local_now = now.astimezone(ZoneInfo(schedule.timezone))
        except (ZoneInfoNotFoundError, ValueError):
            log_event(
                logger,
                "scheduler.invalid_timezone",
                level=logging.WARNING,
                user_id=schedule.user_id,
                timezone=schedule.timezone,
            )
            continue
        if local_now.strftime("%H:%M") != schedule.local_time:
            continue
        briefing_date = local_now.date().isoformat()
        existing = await session.scalar(
            select(Briefing.id).where(
                Briefing.user_id == schedule.user_id,
                Briefing.briefing_date == briefing_date,
                Briefing.local_time == schedule.local_time,
            )
        )
        if existing is not None:
            continue
        actor = _scheduler_actor(schedule.user_id)
        await redis.enqueue_job(
            "generate_briefing",
            schedule.user_id,
            briefing_date,
            schedule.local_time,
            schedule.timezone,
            False,
            serialize_job_context(actor),
            _job_id=f"briefing:{schedule.user_id}:{briefing_date}:{schedule.local_time}",
        )
        record_audit(
            session,
            user_id=schedule.user_id,
            action="briefing.scheduled",
            resource_type="briefing",
            resource_id=f"{briefing_date}:{schedule.local_time}",
            outcome="accepted",
            actor=actor,
            details={"timezone": schedule.timezone, "trigger": "scheduler"},
        )
        await session.commit()
        scheduled += 1
    return scheduled


async def schedule_due_work(ctx: dict[str, Any]) -> dict[str, int]:
    """Dispatch due tenant work without doing the work inside the cron tick."""

    redis = ctx.get("redis")
    if redis is None:
        return {"sources": 0, "discovery": 0, "briefings": 0}
    now = datetime.now(UTC)
    async with SessionFactory() as session:
        sources = await _schedule_sources(session, redis, now=now)
        discovery = await _schedule_discovery(session, redis, now=now)
        briefings = await _schedule_briefings(session, redis, now=now)
    return {"sources": sources, "discovery": discovery, "briefings": briefings}


class WorkerSettings:
    functions = [
        heartbeat,
        ingest_source,
        ingest_discovered_article,
        reconcile_cluster,
        discover_user,
        generate_briefing,
    ]
    cron_jobs = [
        cron("app.usage.service.purge_usage_events", hour=3, minute=0),
        cron("app.audit.service.purge_audit_events", hour=3, minute=15),
        cron("app.worker.heartbeat", second={0, 30}, run_at_startup=True, keep_result=0),
        cron("app.worker.schedule_due_work", second=0, run_at_startup=True, keep_result=0),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 10
    max_tries = get_settings().ingestion_max_attempts
    job_timeout = 300
