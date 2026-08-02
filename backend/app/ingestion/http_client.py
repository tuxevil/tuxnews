from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import Settings, get_settings
from app.observability import metrics

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[Sequence[IPAddress]]]


class HttpFetchError(RuntimeError):
    """Safe, non-sensitive error raised for rejected or failed fetches."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()


async def _resolve_host(hostname: str, port: int) -> Sequence[IPAddress]:
    try:
        records = await asyncio.to_thread(socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise HttpFetchError("destination could not be resolved") from exc

    addresses: list[IPAddress] = []
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0])
        except ValueError:
            continue
        if address not in addresses:
            addresses.append(address)
    return addresses


def _mime_is_allowed(content_type: str, allowed_mime_types: Collection[str]) -> bool:
    mime = content_type.split(";", 1)[0].strip().lower()
    for allowed in allowed_mime_types:
        normalized = allowed.strip().lower()
        if normalized.endswith("/*") and mime.startswith(normalized[:-1]):
            return True
        if mime == normalized:
            return True
    return False


class SafeHttpClient:
    """HTTP adapter that treats every external response and redirect as untrusted."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._resolver = resolver or _resolve_host
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            headers={"User-Agent": "Tuxnews/0.1"},
            timeout=self.settings.http_timeout_seconds,
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self) -> SafeHttpClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def validate_destination(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise HttpFetchError("invalid destination URL") from exc

        if parsed.scheme not in {"http", "https"} or not hostname:
            raise HttpFetchError("destination URL must use HTTP or HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise HttpFetchError("destination credentials are not allowed")
        normalized_hostname = hostname.rstrip(".").lower()
        if normalized_hostname == "localhost" or normalized_hostname.endswith(
            (".localhost", ".local", ".localdomain", ".internal", ".home.arpa")
        ):
            raise HttpFetchError("destination is not publicly routable")

        destination_port = port or (443 if parsed.scheme == "https" else 80)
        if destination_port not in self.settings.http_allowed_ports:
            raise HttpFetchError("destination port is not allowed")

        try:
            literal_address = ipaddress.ip_address(hostname)
        except ValueError:
            addresses = await self._resolver(hostname, destination_port)
        else:
            addresses = [literal_address]

        if not addresses or any(not address.is_global for address in addresses):
            raise HttpFetchError("destination is not publicly routable")

    async def fetch(
        self,
        url: str,
        *,
        allowed_mime_types: Collection[str] | None = None,
        max_bytes: int | None = None,
        operation: str = "external.fetch",
    ) -> FetchResult:
        timer = metrics.timer(operation)
        try:
            result = await self._fetch(url, allowed_mime_types=allowed_mime_types, max_bytes=max_bytes)
        except Exception:
            timer.finish(success=False)
            raise
        timer.finish(success=True)
        return result

    async def _fetch(
        self,
        url: str,
        *,
        allowed_mime_types: Collection[str] | None = None,
        max_bytes: int | None = None,
    ) -> FetchResult:
        allowed_types = tuple(
            self.settings.http_allowed_mime_types if allowed_mime_types is None else allowed_mime_types
        )
        byte_limit = max_bytes if max_bytes is not None else self.settings.http_max_bytes
        if byte_limit < 1:
            raise ValueError("max_bytes must be positive")

        current_url = url
        for redirect_count in range(self.settings.http_max_redirects + 1):
            await self.validate_destination(current_url)
            try:
                async with self._client.stream("GET", current_url) as response:
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("location")
                        if not location or redirect_count >= self.settings.http_max_redirects:
                            raise HttpFetchError("redirect limit exceeded")
                        current_url = urljoin(current_url, location)
                        continue
                    if not 200 <= response.status_code < 300:
                        raise HttpFetchError(f"upstream returned status {response.status_code}")

                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared_length = int(content_length)
                        except ValueError as exc:
                            raise HttpFetchError("upstream returned an invalid content length") from exc
                        if declared_length < 0 or declared_length > byte_limit:
                            raise HttpFetchError("response exceeds byte limit")
                    if not _mime_is_allowed(response.headers.get("content-type", ""), allowed_types):
                        raise HttpFetchError("response MIME type is not allowed")

                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        if len(content) + len(chunk) > byte_limit:
                            raise HttpFetchError("response exceeds byte limit")
                        content.extend(chunk)
                    return FetchResult(
                        url=current_url,
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        content=bytes(content),
                    )
            except HttpFetchError:
                raise
            except httpx.TimeoutException as exc:
                raise HttpFetchError("upstream request timed out") from exc
            except httpx.HTTPError as exc:
                raise HttpFetchError("upstream request failed") from exc

        raise HttpFetchError("redirect limit exceeded")
