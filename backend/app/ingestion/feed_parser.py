from __future__ import annotations

import hashlib
import posixpath
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser  # type: ignore[import-untyped]
import nh3

TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_"}


@dataclass(frozen=True)
class NormalizedEntry:
    title: str
    url: str
    canonical_url: str
    canonical_url_hash: str
    guid: str
    author: str | None
    summary: str | None
    content: str | None
    tags: tuple[str, ...]
    published_at: datetime | None
    image_url: str | None


@dataclass(frozen=True)
class FeedParseResult:
    entries: tuple[NormalizedEntry, ...]
    errors: tuple[str, ...]


def canonicalize_url(raw_url: str, *, base_url: str | None = None) -> str:
    candidate = urljoin(base_url, raw_url) if base_url else raw_url
    try:
        parsed = urlsplit(candidate.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid URL") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")

    normalized_hostname = hostname.encode("idna").decode("ascii").lower()
    default_port = 443 if parsed.scheme == "https" else 80
    netloc = f"[{normalized_hostname}]" if ":" in normalized_hostname else normalized_hostname
    if port is not None and port != default_port:
        netloc = f"{netloc}:{port}"

    path = posixpath.normpath(parsed.path or "/")
    if not path.startswith("/"):
        path = f"/{path}"
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    query.sort()
    return urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(query), ""))


def _published_at(entry: object) -> datetime | None:
    if not isinstance(entry, dict):
        return None
    for field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(field)
        if parsed is not None:
            try:
                parts = tuple(int(part) for part in parsed[:6])
                if len(parts) != 6:
                    return None
                return datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], tzinfo=UTC)
            except (TypeError, ValueError):
                return None
    return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = nh3.clean(value).strip()
    return cleaned or None


def _entry_link(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    link = entry.get("link")
    if isinstance(link, str) and link.strip():
        return link
    links = entry.get("links", [])
    if isinstance(links, list):
        for candidate in links:
            if isinstance(candidate, dict) and candidate.get("rel", "alternate") == "alternate":
                href = candidate.get("href")
                if isinstance(href, str) and href.strip():
                    return href
    return None


def _entry_image(entry: object, *, base_url: str) -> str | None:
    if not isinstance(entry, dict):
        return None
    candidates: list[object] = []
    for field in ("media_content", "media_thumbnail", "enclosures", "links"):
        value = entry.get(field, [])
        if isinstance(value, list):
            candidates.extend(value)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        image_url = candidate.get("url") or candidate.get("href")
        media_type = str(candidate.get("type", ""))
        if not isinstance(image_url, str) or (media_type and not media_type.startswith("image/")):
            continue
        try:
            return canonicalize_url(image_url, base_url=base_url)
        except ValueError:
            return None
    return None


def _entry_tags(entry: object) -> tuple[str, ...]:
    if not isinstance(entry, dict):
        return ()
    tags: list[str] = []
    raw_tags = entry.get("tags", [])
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            value = tag.get("term") if isinstance(tag, dict) else tag
            if isinstance(value, str) and value.strip():
                normalized = value.strip()
                if normalized not in tags:
                    tags.append(normalized)
    return tuple(tags)


def _normalize_entry(entry: object, *, index: int, base_url: str) -> NormalizedEntry:
    if not isinstance(entry, dict):
        raise ValueError(f"entry[{index}] is not an object")
    title = str(entry.get("title", "")).strip()
    if not title:
        raise ValueError(f"entry[{index}] has no title")
    raw_url = _entry_link(entry) or entry.get("id") or entry.get("guid")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError(f"entry[{index}] has no link or GUID")
    canonical_url = canonicalize_url(raw_url, base_url=base_url)
    guid = str(entry.get("id") or entry.get("guid") or canonical_url).strip()
    author = entry.get("author")
    if not isinstance(author, str):
        author_detail = entry.get("author_detail")
        author = author_detail.get("name") if isinstance(author_detail, dict) else None
    content = None
    raw_content = entry.get("content", [])
    if isinstance(raw_content, list):
        content_values = [item.get("value") for item in raw_content if isinstance(item, dict)]
        content = _text("\n\n".join(value for value in content_values if isinstance(value, str)))
    summary = _text(entry.get("summary"))
    if content is None:
        content = summary
    return NormalizedEntry(
        title=title,
        url=canonical_url,
        canonical_url=canonical_url,
        canonical_url_hash=hashlib.sha256(canonical_url.encode("utf-8")).hexdigest(),
        guid=guid,
        author=author.strip() if isinstance(author, str) and author.strip() else None,
        summary=summary,
        content=content,
        tags=_entry_tags(entry),
        published_at=_published_at(entry),
        image_url=_entry_image(entry, base_url=base_url),
    )


def parse_feed(content: bytes, *, source_url: str) -> FeedParseResult:
    try:
        base_url = canonicalize_url(source_url)
    except ValueError:
        return FeedParseResult((), ("source URL is invalid",))
    try:
        parsed = feedparser.parse(content)
    except Exception:
        return FeedParseResult((), ("feed could not be parsed",))

    errors: list[str] = []
    if getattr(parsed, "bozo", False):
        errors.append("feed contains malformed XML")
    entries: list[NormalizedEntry] = []
    for index, entry in enumerate(getattr(parsed, "entries", [])):
        try:
            entries.append(_normalize_entry(entry, index=index, base_url=base_url))
        except ValueError as exc:
            errors.append(str(exc))
    return FeedParseResult(tuple(deduplicate_entries(entries)), tuple(errors))


def deduplicate_entries(entries: Iterable[NormalizedEntry]) -> tuple[NormalizedEntry, ...]:
    unique: dict[str, NormalizedEntry] = {}
    for entry in entries:
        previous = unique.get(entry.canonical_url_hash)
        if previous is None or (
            entry.published_at is not None
            and (previous.published_at is None or entry.published_at > previous.published_at)
        ):
            unique[entry.canonical_url_hash] = entry
    return tuple(unique.values())
