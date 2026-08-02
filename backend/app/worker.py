import logging
import time

from arq import cron
from arq.connections import RedisSettings

from app.briefings.service import generate_briefing
from app.core.config import get_settings
from app.discovery.jobs import discover_user
from app.ingestion.jobs import ingest_source, reconcile_cluster
from app.observability import log_event, metrics

logger = logging.getLogger(__name__)
WORKER_HEARTBEAT_KEY = "tuxnews:health:worker"


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


class WorkerSettings:
    functions = [heartbeat, ingest_source, reconcile_cluster, discover_user, generate_briefing]
    cron_jobs = [
        cron("app.usage.service.purge_usage_events", hour=3, minute=0),
        cron("app.audit.service.purge_audit_events", hour=3, minute=15),
        cron("app.worker.heartbeat", second={0, 30}, run_at_startup=True, keep_result=0),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 10
    max_tries = get_settings().ingestion_max_attempts
    job_timeout = 300
