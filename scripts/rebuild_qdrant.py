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
from app.embeddings.qdrant_index import EmbeddingIndex

EmbeddingProvider = Callable[[str], Awaitable[list[float]]]


async def _default_provider(model: str) -> list[float]:
    raise RuntimeError(
        f"no embedding provider configured for model {model!r}; "
        "set one via the rebuild entrypoint or restore the Qdrant snapshot"
    )


async def rebuild(
    provider: EmbeddingProvider | None = None,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    active_provider = provider or _default_provider
    index = EmbeddingIndex(settings)
    scanned = 0
    embedded = 0
    skipped = 0
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
            embedded += 1
    return {
        "collection": index.collection,
        "articles_scanned": scanned,
        "vectors_upserted": embedded,
        "skipped_missing_embeddings": skipped,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    try:
        result = await rebuild(limit=args.limit)
    except RuntimeError as exc:
        print(f"rebuild blocked: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
