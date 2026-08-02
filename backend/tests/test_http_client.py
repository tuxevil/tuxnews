import ipaddress

import httpx
import pytest
from app.core.config import Settings
from app.ingestion.http_client import HttpFetchError, SafeHttpClient


async def public_resolver(_: str, __: int) -> list[ipaddress.IPv4Address]:
    return [ipaddress.ip_address("93.184.216.34")]


def local_settings(**overrides: object) -> Settings:
    return Settings(
        http_max_bytes=64,
        http_max_redirects=2,
        **overrides,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.test/feed",
        "http://127.0.0.1/feed",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/feed",
        "http://224.0.0.1/feed",
    ],
)
async def test_rejects_non_http_and_non_public_destinations(url: str) -> None:
    async with SafeHttpClient(local_settings(), resolver=public_resolver) as client:
        with pytest.raises(HttpFetchError):
            await client.fetch(url)


@pytest.mark.asyncio
async def test_revalidates_redirect_destination() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"}, request=request)

    async with SafeHttpClient(
        local_settings(),
        transport=httpx.MockTransport(handler),
        resolver=public_resolver,
    ) as client:
        with pytest.raises(HttpFetchError, match="publicly routable"):
            await client.fetch("http://news.example.test/feed")


@pytest.mark.asyncio
async def test_enforces_mime_and_streaming_byte_limits() -> None:
    async def bad_mime(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream", "content-length": "3"},
            content=b"abc",
            request=request,
        )

    async with SafeHttpClient(
        local_settings(),
        transport=httpx.MockTransport(bad_mime),
        resolver=public_resolver,
    ) as client:
        with pytest.raises(HttpFetchError, match="MIME"):
            await client.fetch("http://news.example.test/feed")

    async def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/xml"},
            content=b"x" * 65,
            request=request,
        )

    async with SafeHttpClient(
        local_settings(),
        transport=httpx.MockTransport(oversized),
        resolver=public_resolver,
    ) as client:
        with pytest.raises(HttpFetchError, match="byte limit"):
            await client.fetch("http://news.example.test/feed")


@pytest.mark.asyncio
async def test_fetches_allowed_content_without_following_proxy_environment() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/rss+xml; charset=utf-8"},
            content=b"<rss />",
            request=request,
        )

    async with SafeHttpClient(
        local_settings(),
        transport=httpx.MockTransport(handler),
        resolver=public_resolver,
    ) as client:
        result = await client.fetch("http://news.example.test/feed")

    assert result.content == b"<rss />"
    assert result.content_type == "application/rss+xml"
