from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import record_audit
from app.core.context import ActorContext
from app.db.models import Source


class SourceDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    url: HttpUrl
    source_type: Literal["rss", "atom"] = "rss"
    tags: list[str] = Field(default_factory=list, max_length=32)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be empty")
        return normalized

    @field_validator("url")
    @classmethod
    def reject_url_credentials(cls, value: HttpUrl) -> HttpUrl:
        parsed = urlsplit(str(value))
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("source URL credentials are not allowed")
        return value

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        normalized = [tag.strip() for tag in value]
        if any(not tag for tag in normalized):
            raise ValueError("tags cannot be empty")
        return list(dict.fromkeys(normalized))


@dataclass(frozen=True)
class SourceLoadResult:
    sources: list[SourceDefinition]
    errors: list[str]


def load_sources(path: Path) -> SourceLoadResult:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return SourceLoadResult([], [f"unable to read sources file: {exc}"])
    except yaml.YAMLError:
        return SourceLoadResult([], ["sources file contains invalid YAML"])

    if document is None:
        return SourceLoadResult([], [])
    if isinstance(document, dict):
        unknown_keys = set(document) - {"sources"}
        if unknown_keys:
            return SourceLoadResult([], ["sources file contains unknown top-level keys"])
        records = document.get("sources")
    else:
        records = document
    if not isinstance(records, list):
        return SourceLoadResult([], ["sources must be a list"])

    sources: list[SourceDefinition] = []
    errors: list[str] = []
    seen_urls: set[str] = set()
    for index, record in enumerate(records):
        try:
            source = SourceDefinition.model_validate(record)
        except ValidationError:
            errors.append(f"sources[{index}] is invalid")
            continue
        normalized_url = str(source.url)
        if normalized_url in seen_urls:
            errors.append(f"sources[{index}] duplicates an earlier URL")
            continue
        seen_urls.add(normalized_url)
        sources.append(source)
    return SourceLoadResult(sources, errors)


async def sync_static_sources(
    session: AsyncSession,
    user_id: int,
    path: Path,
    *,
    actor: ActorContext | None = None,
    correlation_id: str | None = None,
) -> SourceLoadResult:
    result = load_sources(path)
    existing = list(
        await session.scalars(select(Source).where(Source.user_id == user_id))
    )
    by_url = {source.url: source for source in existing}
    seen_static_urls: set[str] = set()
    errors = list(result.errors)
    created = 0
    deactivated = 0

    for definition in result.sources:
        url = str(definition.url)
        current = by_url.get(url)
        if current is not None and current.origin == "dynamic":
            errors.append(f"dynamic source already owns URL {url}")
            continue
        if current is None:
            current = Source(user_id=user_id, url=url, origin="static")
            session.add(current)
            created += 1
        current.name = definition.name
        current.source_type = definition.source_type
        current.tags = definition.tags
        current.is_active = definition.is_active
        current.origin = "static"
        seen_static_urls.add(url)

    for source in existing:
        if source.origin == "static" and source.url not in seen_static_urls:
            source.is_active = False
            deactivated += 1

    await session.flush()
    record_audit(
        session,
        user_id=user_id,
        action="source.static_sync",
        resource_type="source_catalog",
        resource_id=str(user_id),
        outcome="partial" if errors else "success",
        correlation_id=correlation_id,
        actor=actor,
        details={"created": created, "deactivated": deactivated, "errors": len(errors)},
    )
    await session.commit()
    return SourceLoadResult(result.sources, errors)
