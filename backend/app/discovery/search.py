from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit, urlunsplit

import nh3
from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from app.core.config import Settings, get_settings
from app.ingestion.http_client import FetchResult, HttpFetchError, SafeHttpClient

PROVIDER_NAME = "duckduckgo"
PROVIDER_VERSION = "html-v1"
WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class SearchCandidate:
    title: str
    snippet: str
    url: str
    published_at: datetime | None
    provider: str
    provider_version: str


@dataclass(frozen=True)
class SearchResult:
    query: str
    provider: str
    provider_version: str
    candidates: tuple[SearchCandidate, ...]
    errors: tuple[str, ...] = ()


class SearchProvider(Protocol):
    provider: str
    version: str

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        timeout_seconds: float | None = None,
    ) -> SearchResult: ...


def _plain_text(value: str | None, *, max_length: int) -> str:
    if not value:
        return ""
    clean = WHITESPACE.sub(" ", nh3.clean(value, tags=set())).strip()
    return clean[:max_length].rstrip()


def _external_url(raw_url: str, *, base_url: str) -> str | None:
    candidate = raw_url.strip()
    if candidate.startswith("/l/"):
        encoded = parse_qs(urlsplit(candidate).query).get("uddg", [""])[0]
        candidate = unquote(encoded)
    if not candidate:
        return None
    parsed = urlsplit(candidate, scheme="https")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.netloc == "":
        candidate = base_url.rstrip("/") + "/" + candidate.lstrip("/")
        parsed = urlsplit(candidate)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _parse_candidates(result: FetchResult, *, limit: int) -> list[SearchCandidate]:
    soup = BeautifulSoup(result.content, "html.parser")
    candidates: list[SearchCandidate] = []
    seen_urls: set[str] = set()
    for link in soup.select("a.result__a"):
        raw_url = link.get("href")
        if not isinstance(raw_url, str):
            continue
        url = _external_url(raw_url, base_url="https://html.duckduckgo.com")
        title = _plain_text(link.get_text(" ", strip=True), max_length=500)
        if url is None or not title or url in seen_urls:
            continue
        snippet_node = link.find_parent(class_="result")
        snippet = ""
        if snippet_node is not None:
            snippet_element = snippet_node.select_one(".result__snippet")
            if snippet_element is not None:
                snippet = _plain_text(snippet_element.get_text(" ", strip=True), max_length=2_000)
        seen_urls.add(url)
        candidates.append(
            SearchCandidate(
                title=title,
                snippet=snippet,
                url=url,
                published_at=None,
                provider=PROVIDER_NAME,
                provider_version=PROVIDER_VERSION,
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


class DuckDuckGoSearchProvider:
    provider = PROVIDER_NAME
    version = PROVIDER_VERSION

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: SafeHttpClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        timeout_seconds: float | None = None,
    ) -> SearchResult:
        normalized_query = WHITESPACE.sub(" ", query).strip()
        if not normalized_query:
            raise ValueError("search query cannot be empty")
        if len(normalized_query) > 300:
            raise ValueError("search query is too long")
        bounded_limit = min(max(limit, 1), self.settings.discovery_max_results)
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(normalized_query)}&kl=wt-wt"
        own_client = self.client is None
        client = self.client or SafeHttpClient(self.settings)
        errors: list[str] = []
        try:
            for attempt in range(self.settings.discovery_max_retries + 1):
                try:
                    fetch = client.fetch(url, allowed_mime_types=("text/html",), operation="discovery.fetch")
                    if timeout_seconds is None:
                        response = await fetch
                    else:
                        response = await asyncio.wait_for(fetch, timeout=timeout_seconds)
                    candidates = _parse_candidates(response, limit=bounded_limit)
                    safe_candidates: list[SearchCandidate] = []
                    for candidate in candidates:
                        try:
                            await client.validate_destination(candidate.url)
                        except HttpFetchError:
                            continue
                        safe_candidates.append(candidate)
                    return SearchResult(
                        query=normalized_query,
                        provider=self.provider,
                        provider_version=self.version,
                        candidates=tuple(safe_candidates),
                        errors=tuple(errors),
                    )
                except (HttpFetchError, TimeoutError) as exc:
                    errors.append(type(exc).__name__)
                    if attempt < self.settings.discovery_max_retries:
                        await asyncio.sleep(self.settings.discovery_retry_backoff_seconds * (attempt + 1))
            return SearchResult(
                query=normalized_query,
                provider=self.provider,
                provider_version=self.version,
                candidates=(),
                errors=tuple(errors),
            )
        finally:
            if own_client:
                await client.aclose()
