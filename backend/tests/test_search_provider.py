import ipaddress

import httpx
import pytest
from app.core.config import Settings
from app.discovery.search import DuckDuckGoSearchProvider, SearchCandidate, SearchProvider
from app.ingestion.http_client import FetchResult

HTML = b"""
<html><div class="result">
  <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fstory%23frag">  Safe <b>story</b> </a>
  <a class="result__snippet">A <script>bad()</script> useful snippet.</a>
</div><div class="result">
  <a class="result__a" href="file:///etc/passwd">Private</a>
</div></html>
"""


class FakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.validated: list[str] = []

    async def fetch(self, _: str, **__: object) -> FetchResult:
        if self.fail:
            from app.ingestion.http_client import HttpFetchError

            raise HttpFetchError("upstream failed")
        return FetchResult(
            url="https://html.duckduckgo.com/html/",
            status_code=200,
            headers={"content-type": "text/html"},
            content=HTML,
        )

    async def validate_destination(self, url: str) -> None:
        self.validated.append(url)

    async def aclose(self) -> None:
        return None


def test_provider_protocol_is_replaceable() -> None:
    fake: SearchProvider = DuckDuckGoSearchProvider(client=FakeClient())
    assert fake.provider == "duckduckgo"


@pytest.mark.asyncio
async def test_duckduckgo_normalizes_results_and_records_provenance() -> None:
    client = FakeClient()
    result = await DuckDuckGoSearchProvider(client=client).search("  linux news  ", limit=5)

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert isinstance(candidate, SearchCandidate)
    assert candidate.title == "Safe story"
    assert candidate.snippet == "A useful snippet."
    assert candidate.url == "https://example.com/story"
    assert candidate.provider == "duckduckgo"
    assert candidate.provider_version == "html-v1"
    assert client.validated == [candidate.url]


@pytest.mark.asyncio
async def test_provider_errors_return_empty_result_after_bounded_retries() -> None:
    client = FakeClient(fail=True)
    settings = Settings(discovery_max_retries=1, discovery_retry_backoff_seconds=0)
    result = await DuckDuckGoSearchProvider(settings, client=client).search("linux")

    assert result.candidates == ()
    assert result.errors == ("HttpFetchError", "HttpFetchError")


@pytest.mark.asyncio
async def test_provider_rejects_empty_or_oversized_query() -> None:
    provider = DuckDuckGoSearchProvider(client=FakeClient())
    with pytest.raises(ValueError):
        await provider.search(" ")
    with pytest.raises(ValueError):
        await provider.search("x" * 301)


@pytest.mark.asyncio
async def test_provider_uses_safe_http_destination_checks() -> None:
    async def resolver(_: str, __: int) -> list[ipaddress.IPv4Address]:
        return [ipaddress.ip_address("93.184.216.34")]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=HTML, request=request)

    from app.ingestion.http_client import SafeHttpClient

    async with SafeHttpClient(
        Settings(discovery_max_results=1),
        transport=httpx.MockTransport(handler),
        resolver=resolver,
    ) as client:
        result = await DuckDuckGoSearchProvider(client=client).search("linux")
    assert len(result.candidates) == 1
