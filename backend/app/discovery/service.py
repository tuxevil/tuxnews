from __future__ import annotations

from app.core.config import Settings, get_settings
from app.discovery.search import DuckDuckGoSearchProvider, SearchProvider, SearchResult


async def search_articles(
    query: str,
    *,
    max_results: int,
    settings: Settings | None = None,
    provider: SearchProvider | None = None,
) -> SearchResult:
    """Search external articles through the configured discovery provider."""

    selected_provider = provider or DuckDuckGoSearchProvider(settings or get_settings())
    return await selected_provider.search(query, limit=max_results)
