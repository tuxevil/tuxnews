from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import nh3
from sqlalchemy import select

from app.audit.service import record_audit
from app.core.config import get_settings
from app.core.context import job_context_from_payload, serialize_job_context
from app.core.quota import quota_guard
from app.db.models import Article, DiscoveryRun, Source, User, UserTopic
from app.db.session import SessionFactory
from app.discovery.search import DuckDuckGoSearchProvider, SearchCandidate, SearchProvider, SearchResult
from app.ingestion.feed_parser import canonicalize_url
from app.ingestion.http_client import SafeHttpClient
from app.preferences.settings import resolve_user_settings
from app.ranking.display import validate_serendipity
from app.ranking.scoring import ScoreWeights, apply_score, calculate_score

DISCOVERY_SOURCE_URL = "https://html.duckduckgo.com/html/"
DEFAULT_DISCOVERY_QUERY = "latest technology news"
SAFE_TOPIC = re.compile(r"[^a-z0-9 _-]+")
SAFE_HOST = re.compile(r"^[a-z0-9.-]+$")


@dataclass(frozen=True)
class DiscoveryQuery:
    query: str
    topic_name: str | None
    source_host: str | None


def _clean_topic(value: str) -> str:
    normalized = " ".join(value.lower().strip().split())
    return SAFE_TOPIC.sub("", normalized)[:80].strip()


def _source_host(source: Source) -> str | None:
    host = urlsplit(source.url).hostname
    if host is None:
        return None
    normalized = host.lower().strip(".")
    return normalized if SAFE_HOST.fullmatch(normalized) else None


def build_discovery_queries(
    topics: Sequence[UserTopic],
    sources: Sequence[Source],
    *,
    serendipity: float,
    max_queries: int = 8,
) -> tuple[DiscoveryQuery, ...]:
    serendipity = validate_serendipity(serendipity)
    if max_queries < 1:
        return ()
    positive_topics = sorted(
        {
            cleaned: topic.weight_score
            for topic in topics
            if topic.weight_score > 0 and (cleaned := _clean_topic(topic.topic_name))
        }.items(),
        key=lambda item: (-item[1], item[0]),
    )
    hosts = list(dict.fromkeys(host for source in sources if source.is_active and (host := _source_host(source))))
    queries: list[DiscoveryQuery] = []

    def add(topic_name: str | None, source_host: str | None, suffix: str = "") -> None:
        if len(queries) >= max_queries:
            return
        terms = [topic_name or DEFAULT_DISCOVERY_QUERY, "news"]
        if suffix:
            terms.append(suffix)
        if source_host:
            terms.append(f"site:{source_host}")
        queries.append(DiscoveryQuery(" ".join(terms)[:300], topic_name, source_host))

    if not positive_topics:
        add(None, None)
    for topic_name, _ in positive_topics:
        add(topic_name, None)
        if serendipity >= 0.5:
            add(topic_name, None, "emerging perspectives")
        for host in hosts[:1]:
            add(topic_name, host)
    return tuple(queries[:max_queries])


def _clean_external_text(value: str, max_length: int) -> str:
    return nh3.clean(value, tags=set()).strip()[:max_length].rstrip()


async def _persist_candidate(
    session: Any,
    *,
    user_id: int,
    source: Source,
    query: DiscoveryQuery,
    candidate: SearchCandidate,
    weights: ScoreWeights,
) -> int | None:
    try:
        canonical_url = canonicalize_url(candidate.url)
    except ValueError:
        return None
    canonical_hash = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    existing = await session.scalar(
        select(Article).where(Article.user_id == user_id, Article.canonical_url_hash == canonical_hash)
    )
    if existing is not None:
        return None
    title = _clean_external_text(candidate.title, 500)
    snippet = _clean_external_text(candidate.snippet, 4_000) or None
    if not title:
        return None
    article = Article(
        user_id=user_id,
        source_id=source.id,
        title=title,
        original_title=title,
        url=canonical_url,
        canonical_url_hash=canonical_hash,
        content_clean=snippet,
        summary=snippet,
        tags=[query.topic_name] if query.topic_name else [],
        published_at=candidate.published_at,
        status="discovered",
    )
    apply_score(
        article,
        calculate_score(
            semantic_similarity=None,
            source_reputation=source.reputation_score,
            feedback_penalty=None,
            text=snippet,
            weights=weights,
        ),
    )
    session.add(article)
    await session.flush()
    return article.id


async def _run_queries(
    session: Any,
    *,
    user: User,
    source: Source,
    queries: Sequence[DiscoveryQuery],
    provider: SearchProvider,
    validate_url: Callable[[str], Awaitable[None]],
    max_results: int,
    max_candidates: int,
    weights: ScoreWeights,
) -> tuple[int, list[str], list[int]]:
    created = 0
    errors: list[str] = []
    article_ids: list[int] = []
    seen_urls: set[str] = set()
    for query in queries:
        try:
            result: SearchResult = await provider.search(query.query, limit=max_results)
        except Exception as exc:
            errors.append(f"{query.query}: {type(exc).__name__}")
            continue
        errors.extend(f"{query.query}: {error}" for error in result.errors)
        for candidate in result.candidates:
            if created >= max_candidates:
                break
            try:
                await validate_url(candidate.url)
                canonical_url = canonicalize_url(candidate.url)
            except Exception:
                continue
            if canonical_url in seen_urls:
                continue
            seen_urls.add(canonical_url)
            normalized_candidate = SearchCandidate(
                title=candidate.title,
                snippet=candidate.snippet,
                url=canonical_url,
                published_at=candidate.published_at,
                provider=candidate.provider,
                provider_version=candidate.provider_version,
            )
            article_id = await _persist_candidate(
                session,
                user_id=user.id,
                source=source,
                query=query,
                candidate=normalized_candidate,
                weights=weights,
            )
            if article_id is not None:
                created += 1
                article_ids.append(article_id)
    return created, errors, article_ids


@quota_guard(scope="news:read", operation="worker.discover_user", provider="search", payload_position=2)
async def discover_user(
    ctx: dict[str, Any],
    user_id: int,
    slot_key: str | None = None,
    job_payload: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    settings = get_settings()
    payload = job_payload if job_payload is not None else ctx
    job = job_context_from_payload(payload)
    if job is None or job.tenant.tenant_id != user_id:
        return {"status": "rejected", "user_id": user_id, "reason": "tenant context required"}
    tenant = job.tenant
    actor = job.actor
    redis = ctx.get("redis")
    slot_key = (slot_key or "").strip()[:64] or datetime.now(UTC).strftime("%Y-%m-%dT%H")
    async with SessionFactory() as session:
        user = await session.scalar(
            select(User).where(User.id == tenant.tenant_id, User.is_active.is_(True))
        )
        if user is None:
            return {"status": "missing", "user_id": user_id, "slot_key": slot_key}
        user_settings = resolve_user_settings(user.settings_json, settings)
        existing_run = await session.scalar(
            select(DiscoveryRun).where(DiscoveryRun.user_id == user_id, DiscoveryRun.slot_key == slot_key)
        )
        if existing_run is not None and existing_run.status in {"succeeded", "partial", "skipped"}:
            return {"status": existing_run.status, "run_id": existing_run.id, "slot_key": slot_key}
        topics = list(await session.scalars(select(UserTopic).where(UserTopic.user_id == user_id)))
        sources = list(
            await session.scalars(
                select(Source).where(Source.user_id == user_id, Source.is_active.is_(True)).order_by(Source.id)
            )
        )
        queries = build_discovery_queries(
            topics,
            sources,
            serendipity=user.serendipity_score,
            max_queries=user_settings.discovery_max_queries,
        )
        provider = ctx.get("search_provider") or DuckDuckGoSearchProvider(settings)
        if existing_run is None:
            run = DiscoveryRun(
                user_id=user_id,
                slot_key=slot_key,
                provider=provider.provider,
                provider_version=provider.version,
                serendipity_score=user.serendipity_score,
            )
            session.add(run)
            await session.flush()
        else:
            run = existing_run
            run.status = "running"
            run.provider = provider.provider
            run.provider_version = provider.version
            run.serendipity_score = user.serendipity_score
        run.query_count = len(queries)
        if not queries:
            run.status = "skipped"
            record_audit(
                session,
                user_id=user_id,
                action="discovery.skipped",
                resource_type="discovery_run",
                resource_id=str(run.id),
                outcome="skipped",
                actor_type=actor.actor_type,
                actor_id=actor.actor_id,
                correlation_id=actor.correlation_id,
                details={"reason": "no_queries"},
            )
            await session.commit()
            return {"status": run.status, "run_id": run.id, "queries": 0, "created": 0}
        source = await session.scalar(
            select(Source).where(
                Source.user_id == user_id,
                Source.url == DISCOVERY_SOURCE_URL,
                Source.origin == "discovery",
            )
        )
        if source is None:
            source = Source(
                user_id=user_id,
                name=f"Discovery / {provider.provider}",
                url=DISCOVERY_SOURCE_URL,
                source_type="rss",
                tags=["discovery", provider.provider],
                origin="discovery",
            )
            session.add(source)
            await session.flush()
        validator = ctx.get("url_validator")
        if validator is None:
            async with SafeHttpClient(settings) as client:
                    created, errors, article_ids = await _run_queries(
                    session,
                    user=user,
                    source=source,
                    queries=queries,
                    provider=provider,
                    validate_url=client.validate_destination,
                    max_results=settings.discovery_max_results,
                    max_candidates=user_settings.discovery_max_candidates,
                    weights=ScoreWeights.from_user_settings(user_settings),
                )
        else:
                created, errors, article_ids = await _run_queries(
                session,
                user=user,
                source=source,
                queries=queries,
                provider=provider,
                validate_url=validator,
                max_results=settings.discovery_max_results,
                max_candidates=user_settings.discovery_max_candidates,
                weights=ScoreWeights.from_user_settings(user_settings),
            )
        run.candidate_count = created
        run.status = "partial" if errors else "succeeded"
        run.error_message = "; ".join(errors)[:2000] or None
        record_audit(
            session,
            user_id=user_id,
            action="discovery.completed",
            resource_type="discovery_run",
            resource_id=str(run.id),
            outcome="partial" if errors else "success",
            actor_type=actor.actor_type,
            actor_id=actor.actor_id,
            correlation_id=actor.correlation_id,
            details={"queries": len(queries), "created": created, "errors": len(errors)},
        )
        await session.commit()
        queued = 0
        if redis is not None:
            for article_id in article_ids:
                await redis.enqueue_job(
                    "ingest_discovered_article",
                    article_id,
                    serialize_job_context(actor),
                    _job_id=f"discovery-ingest:{article_id}",
                )
                queued += 1
        return {
            "status": run.status,
            "run_id": run.id,
            "slot_key": slot_key,
            "queries": len(queries),
            "created": created,
            "queued": queued,
            "errors": errors,
        }
