from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from arq import Retry
from sqlalchemy import select

from app.articles.lifecycle import ArticleStatus, transition_article
from app.audit.service import record_audit
from app.clustering.service import assign_article
from app.clustering.service import reconcile_cluster as reconcile_story_cluster
from app.core.config import get_settings
from app.core.context import ActorContext, TenantContext, job_context_from_payload
from app.core.quota import quota_guard
from app.curation.service import CurationService
from app.db.models import Article, IngestionRun, Source, User
from app.db.session import SessionFactory
from app.embeddings.provider import EmbeddingProvider, SentenceTransformerProvider
from app.embeddings.qdrant_index import EmbeddingIndex
from app.ingestion.content import extract_article_text
from app.ingestion.feed_parser import parse_feed
from app.ingestion.http_client import HttpFetchError, SafeHttpClient
from app.preferences.settings import resolve_user_settings
from app.ranking.scoring import ScoreWeights, apply_score, calculate_score


def _attempt_from_context(ctx: dict[str, Any]) -> int:
    value = ctx.get("job_try", 1)
    return value if isinstance(value, int) and value > 0 else 1


def _backoff_seconds(attempt: int) -> float:
    settings = get_settings()
    return min(
        settings.ingestion_max_backoff_seconds,
        settings.ingestion_base_backoff_seconds * (2 ** (attempt - 1)),
    )


async def _retry_or_fail(
    session: Any,
    run_id: int,
    tenant_id: int,
    attempt: int,
    error_message: str,
    actor: ActorContext | None = None,
) -> dict[str, object]:
    settings = get_settings()
    await session.rollback()
    run = await session.scalar(
        select(IngestionRun).where(IngestionRun.id == run_id, IngestionRun.user_id == tenant_id)
    )
    if run is None:
        return {"status": "missing", "run_id": run_id}
    run.attempt = attempt
    run.error_message = error_message[:1000]
    if attempt >= settings.ingestion_max_attempts:
        run.status = "failed"
        record_audit(
            session,
            user_id=tenant_id,
            action="ingestion.failed",
            resource_type="ingestion_run",
            resource_id=str(run_id),
            outcome="failure",
            actor_type=actor.actor_type if actor is not None else "job",
            actor_id=actor.actor_id if actor is not None else "ingest_source",
            correlation_id=actor.correlation_id if actor is not None else None,
            details={"attempt": attempt, "error_type": error_message[:120]},
        )
        await session.commit()
        return {"status": "failed", "run_id": run_id, "attempt": attempt}
    run.status = "retrying"
    record_audit(
        session,
        user_id=tenant_id,
        action="ingestion.retrying",
        resource_type="ingestion_run",
        resource_id=str(run_id),
        outcome="retrying",
        actor_type=actor.actor_type if actor is not None else "job",
        actor_id=actor.actor_id if actor is not None else "ingest_source",
        correlation_id=actor.correlation_id if actor is not None else None,
        details={"attempt": attempt, "error_type": error_message[:120]},
    )
    await session.commit()
    raise Retry(defer=_backoff_seconds(attempt))


async def _upsert_articles(
    session: Any,
    run: IngestionRun,
    source: Source,
    entries: tuple[Any, ...],
    *,
    client: SafeHttpClient,
    curator: CurationService,
    embedding_provider: EmbeddingProvider,
    tenant: TenantContext,
    actor: ActorContext,
    profile: str,
    weights: ScoreWeights,
) -> int:
    created = 0
    index = EmbeddingIndex()
    try:
        for entry in entries:
            vector: list[float] | None = None
            article = await session.scalar(
                select(Article).where(
                    Article.user_id == run.user_id,
                    Article.canonical_url_hash == entry.canonical_url_hash,
                )
            )
            if article is None:
                article = Article(
                    user_id=run.user_id,
                    source_id=source.id,
                    title=entry.title,
                    original_title=entry.title,
                    url=entry.url,
                    canonical_url_hash=entry.canonical_url_hash,
                    author=entry.author,
                    content_clean=entry.content,
                    summary=entry.summary,
                    tags=list(entry.tags),
                    published_at=entry.published_at,
                    status=ArticleStatus.DISCOVERED.value,
                )
                session.add(article)
                created += 1
            elif article.status == ArticleStatus.PUBLISHED.value:
                continue
            elif article.status in {ArticleStatus.CURATED.value, ArticleStatus.INDEXED.value}:
                pass
            else:
                article.source_id = source.id
                article.title = entry.title
                article.url = entry.url
                article.author = entry.author
                article.content_clean = entry.content
                article.summary = entry.summary
                article.tags = list(entry.tags)
                article.published_at = entry.published_at
            if article.status == ArticleStatus.DISCOVERED.value or article.status == ArticleStatus.FAILED.value:
                transition_article(article, ArticleStatus.FETCHING)
            if article.status == ArticleStatus.FETCHING.value:
                try:
                    fetched = await client.fetch(entry.url)
                    extracted = await asyncio.to_thread(
                        extract_article_text,
                        fetched.content,
                        url=entry.url,
                    )
                except HttpFetchError:
                    extracted = None
                article.content_clean = extracted or article.content_clean or article.summary
                transition_article(article, ArticleStatus.EXTRACTED)
            if article.status == ArticleStatus.EXTRACTED.value:
                outcome = await curator.curate(
                    title=article.title,
                    content=article.content_clean or article.summary or "",
                    profile=profile,
                )
                if outcome.result is not None:
                    article.title = outcome.result.title
                    article.summary = outcome.result.summary
                    article.tags = list(outcome.result.tags) or article.tags
                else:
                    article.summary = outcome.fallback_summary
                await session.flush()
                semantic_similarity = None
                try:
                    vector = await embedding_provider.embed(
                        f"{article.title}\n{article.summary or ''}\n{article.content_clean or ''}"
                    )
                    await index.upsert(
                        tenant=tenant,
                        article_id=article.id,
                        vector=vector,
                        canonical_url_hash=article.canonical_url_hash,
                    )
                    article.embedding_model = embedding_provider.spec.model
                    article.embedding_version = embedding_provider.spec.version
                    hits = await index.search(user_id=article.user_id, vector=vector, limit=20)
                    semantic_similarity = max(
                        (hit.score for hit in hits if hit.article_id != article.id),
                        default=None,
                    )
                except Exception:
                    vector = None
                apply_score(
                    article,
                    calculate_score(
                        semantic_similarity=semantic_similarity,
                        source_reputation=source.reputation_score,
                        feedback_penalty=None,
                        text=article.content_clean,
                        weights=weights,
                    ),
                )
                transition_article(article, ArticleStatus.CURATED)
            if article.status == ArticleStatus.CURATED.value:
                transition_article(article, ArticleStatus.INDEXED)
            if article.status == ArticleStatus.INDEXED.value:
                transition_article(article, ArticleStatus.PUBLISHED)
                await assign_article(
                    session,
                    article,
                    tenant=tenant,
                    vector=vector if article.embedding_model is not None else None,
                    index=index if article.embedding_model is not None else None,
                    correlation_id=actor.correlation_id,
                    actor_type=actor.actor_type,
                    actor_id=actor.actor_id,
                )
        return created
    finally:
        await index.aclose()


@quota_guard(scope="content:write", operation="worker.ingest_source", payload_position=1)
async def ingest_source(
    ctx: dict[str, Any],
    run_id: int,
    job_payload: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Fetch, normalize, and idempotently persist one source ingestion run."""
    payload = job_payload if job_payload is not None else ctx
    job = job_context_from_payload(payload)
    if job is None:
        return {"status": "rejected", "run_id": run_id, "reason": "tenant context required"}
    tenant = job.tenant
    actor = job.actor
    runtime_settings = get_settings()
    attempt = _attempt_from_context(ctx)
    curator = CurationService()
    embedding_provider = SentenceTransformerProvider(runtime_settings)
    async with SessionFactory() as session:
        run = await session.scalar(
            select(IngestionRun).where(IngestionRun.id == run_id, IngestionRun.user_id == tenant.tenant_id)
        )
        if run is None:
            return {"status": "rejected", "run_id": run_id}
        if run.status in {"succeeded", "partial", "skipped"}:
            return {"status": run.status, "run_id": run_id, "attempt": run.attempt}
        run.attempt = attempt
        user = await session.scalar(select(User).where(User.id == run.user_id, User.is_active.is_(True)))
        if user is None:
            run.status = "skipped"
            run.error_message = None
            record_audit(
                session,
                user_id=tenant.tenant_id,
                action="ingestion.skipped",
                resource_type="ingestion_run",
                resource_id=str(run_id),
                outcome="skipped",
                actor_type=actor.actor_type,
                actor_id=actor.actor_id,
                correlation_id=actor.correlation_id,
                details={"reason": "inactive_user"},
            )
            await session.commit()
            return {"status": "skipped", "run_id": run_id, "attempt": attempt}
        user_settings = resolve_user_settings(user.settings_json, runtime_settings)
        source = await session.scalar(
            select(Source).where(Source.id == run.source_id, Source.user_id == run.user_id)
        )
        if source is None or not source.is_active:
            run.status = "skipped"
            run.error_message = None
            record_audit(
                session,
                user_id=tenant.tenant_id,
                action="ingestion.skipped",
                resource_type="ingestion_run",
                resource_id=str(run_id),
                outcome="skipped",
                actor_type=actor.actor_type,
                actor_id=actor.actor_id,
                correlation_id=actor.correlation_id,
                details={"reason": "inactive_source"},
            )
            await session.commit()
            return {"status": "skipped", "run_id": run_id, "attempt": attempt}
        try:
            async with SafeHttpClient() as client:
                fetched = await client.fetch(source.url)
                parsed = parse_feed(fetched.content, source_url=source.url)
                if not parsed.entries and parsed.errors:
                    return await _retry_or_fail(
                        session,
                        run_id,
                        tenant.tenant_id,
                        attempt,
                        "; ".join(parsed.errors),
                        actor,
                    )
                created = await _upsert_articles(
                    session,
                    run,
                    source,
                    parsed.entries,
                    client=client,
                    curator=curator,
                    embedding_provider=embedding_provider,
                    tenant=tenant,
                    actor=actor,
                    profile=user_settings.llm_profile,
                    weights=ScoreWeights.from_user_settings(user_settings),
                )
            source.last_fetched_at = datetime.now(UTC)
            run.status = "partial" if parsed.errors else "succeeded"
            run.error_message = "; ".join(parsed.errors)[:1000] or None
            record_audit(
                session,
                user_id=tenant.tenant_id,
                action="ingestion.completed",
                resource_type="ingestion_run",
                resource_id=str(run_id),
                outcome="partial" if parsed.errors else "success",
                actor_type=actor.actor_type,
                actor_id=actor.actor_id,
                correlation_id=actor.correlation_id,
                details={"created": created, "entries": len(parsed.entries), "errors": len(parsed.errors)},
            )
            await session.commit()
            return {
                "status": run.status,
                "run_id": run_id,
                "attempt": attempt,
                "created": created,
                "entries": len(parsed.entries),
            }
        except HttpFetchError as exc:
            return await _retry_or_fail(session, run_id, tenant.tenant_id, attempt, str(exc), actor)
        except Exception:
            return await _retry_or_fail(
                session,
                run_id,
                tenant.tenant_id,
                attempt,
                "ingestion failed unexpectedly",
                actor,
            )


@quota_guard(scope="content:write", operation="worker.reconcile_cluster", payload_position=1)
async def reconcile_cluster(
    ctx: dict[str, Any],
    cluster_id: int,
    job_payload: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Recompute one story's temporal state without touching article ingestion."""
    payload = job_payload if job_payload is not None else ctx
    job = job_context_from_payload(payload)
    if job is None:
        return {"status": "rejected", "cluster_id": cluster_id, "reason": "tenant context required"}
    tenant = job.tenant
    async with SessionFactory() as session:
        status = await reconcile_story_cluster(
            session,
            cluster_id,
            tenant=tenant,
            correlation_id=job.actor.correlation_id,
            actor_type=job.actor.actor_type,
            actor_id=job.actor.actor_id,
        )
        return {"status": status.value if status is not None else "missing", "cluster_id": cluster_id}
