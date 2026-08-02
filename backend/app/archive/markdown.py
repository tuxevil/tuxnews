from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import nh3
import yaml
from markdownify import markdownify  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.archive.paths import (
    ArchivePathError,
    ArchiveWriteResult,
    AtomicArchiveWriter,
    safe_slug,
    tenant_relative_path,
)
from app.db.models import ArchiveExport, Article


@dataclass(frozen=True)
class ArchiveMetadata:
    source_name: str
    source_url: str
    rating: str | None = None
    cluster: str | None = None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _plain_text(value: str | None) -> str | None:
    if value is None:
        return None
    return nh3.clean(value, tags=set()).strip()


def _safe_source_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return value


def build_frontmatter(article: Article, metadata: ArchiveMetadata) -> dict[str, Any]:
    return {
        "title": _plain_text(article.title),
        "original_title": _plain_text(article.original_title),
        "source_name": _plain_text(metadata.source_name),
        "source_url": _safe_source_url(metadata.source_url),
        "author": _plain_text(article.author),
        "published_at": _iso(article.published_at),
        "discovered_at": _iso(article.discovered_at),
        "tags": [_plain_text(tag) or "" for tag in (article.tags or [])],
        "read_time_minutes": article.read_time_minutes,
        "score": article.relevance_score,
        "score_breakdown": article.score_breakdown,
        "rating": _plain_text(metadata.rating),
        "cluster": _plain_text(metadata.cluster),
        "security_context": "UNTRUSTED_EXTERNAL_DATA",
    }


def build_markdown(article: Article, metadata: ArchiveMetadata) -> str:
    frontmatter = yaml.safe_dump(
        build_frontmatter(article, metadata),
        allow_unicode=False,
        default_flow_style=False,
        sort_keys=False,
    )
    raw_content = article.content_clean or article.summary or ""
    clean_content = markdownify(nh3.clean(raw_content)).strip()
    return f"---\n{frontmatter}---\n\n{clean_content}\n"


def _default_path(article: Article) -> Path:
    date = (article.published_at or article.discovered_at or article.created_at).date()
    return Path("tenants") / str(article.user_id) / "standalone" / f"{date:%Y-%m-%d}_{safe_slug(article.title)}.md"


async def export_article(
    session: AsyncSession,
    article: Article,
    metadata: ArchiveMetadata,
    writer: AtomicArchiveWriter,
) -> ArchiveExport:
    export = await session.scalar(
        select(ArchiveExport).where(
            ArchiveExport.user_id == article.user_id,
            ArchiveExport.article_id == article.id,
        )
    )
    if export is None:
        export = ArchiveExport(
            user_id=article.user_id,
            article_id=article.id,
            path=str(_default_path(article)),
            status="writing",
            attempts=0,
        )
        session.add(export)
        await session.flush()
    elif export.status != "writing":
        export.status = "writing"

    export.attempts += 1
    try:
        path = tenant_relative_path(article.user_id, Path(export.path))
        collision = await session.scalar(
            select(ArchiveExport).where(
                ArchiveExport.path == export.path,
                ArchiveExport.user_id == article.user_id,
                ArchiveExport.article_id != article.id,
            )
        )
        if collision is not None:
            path = path.with_name(f"{path.stem}-{article.id}{path.suffix}")
            export.path = str(path)
        result: ArchiveWriteResult = writer.write_text(path, build_markdown(article, metadata))
    except ArchivePathError:
        export.status = "failed"
        export.error_message = "archive export failed"
        await session.commit()
        raise
    export.path = result.relative_path
    export.checksum = result.checksum
    export.status = "ready"
    export.error_message = None
    await session.commit()
    return export
