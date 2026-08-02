"""Rebuild the Qdrant index from PostgreSQL when a snapshot restore is not possible.

Articles persist embedding metadata but not vectors; Qdrant is the disposable
cache. Reconstruction re-embeds every published article through the configured
provider seam and upserts into the versioned collection.

Usage:
    python scripts/rebuild_qdrant.py [--model MODEL] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.context import TenantContext
from app.db.models import Article
from app.db.session import SessionFactory
from app.embeddings.provider import SentenceTransformerProvider
from app.embeddings.qdrant_index import EmbeddingIndex

EmbeddingFunction = Callable[[str], Awaitable[list[float]]]


async def rebuild(
    provider: EmbeddingFunction | None = None,
    *,
    limit: int | None = None,
    settings=None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    local_provider = None if provider is not None else SentenceTransformerProvider(settings)
    if local_provider is not None:
        await local_provider.ensure_available()
    active_provider = provider or local_provider.embed
    index = EmbeddingIndex(settings)
    scanned = 0
    embedded = 0
    skipped = 0
    try:
        async with SessionFactory() as session:
            query = select(Article).where(Article.status == "published").order_by(Article.id)
            if limit is not None:
                query = query.limit(limit)
            articles = list(await session.scalars(query))
            for article in articles:
                scanned += 1
                try:
                    vector = await active_provider(article.content_clean or article.summary or "")
                except Exception:
                    skipped += 1
                    continue
                await index.upsert(
                    tenant=TenantContext(article.user_id),
                    article_id=article.id,
                    vector=vector,
                    canonical_url_hash=article.canonical_url_hash,
                )
                article.embedding_model = settings.embedding_model
                article.embedding_version = settings.embedding_version
                embedded += 1
            await session.commit()
    finally:
        await index.aclose()
    return {
        "collection": index.collection,
        "articles_scanned": scanned,
        "vectors_upserted": embedded,
        "skipped_missing_embeddings": skipped,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    try:
        settings = get_settings()
        if args.model:
            settings = settings.model_copy(update={"embedding_model": args.model})
        result = await rebuild(limit=args.limit, settings=settings)
    except RuntimeError as exc:
        print(f"rebuild blocked: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
