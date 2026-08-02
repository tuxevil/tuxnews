from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from app.archive.markdown import ArchiveMetadata, build_markdown, export_article
from app.archive.paths import AtomicArchiveWriter
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_markdown_export_is_stable_and_tracks_checksum(
    db_session: AsyncSession,
    user_factory,
    source_factory,
    article_factory,
    tmp_path: Path,
) -> None:
    user = user_factory()
    db_session.add(user)
    await db_session.flush()
    source = source_factory(user.id)
    db_session.add(source)
    await db_session.flush()
    article = article_factory(user.id, source.id)
    article.discovered_at = datetime(2026, 8, 1, tzinfo=UTC)
    article.content_clean = "<p>Readable <strong>article</strong>.</p><script>bad()</script>"
    db_session.add(article)
    await db_session.commit()
    writer = AtomicArchiveWriter(tmp_path / "archive")
    metadata = ArchiveMetadata("Fixture source", "https://example.com/feed", rating="like")

    first = await export_article(db_session, article, metadata, writer)
    content = (tmp_path / "archive" / first.path).read_text(encoding="utf-8")
    first_attempts = first.attempts
    first_checksum = first.checksum
    second = await export_article(db_session, article, metadata, writer)

    assert first.path == "tenants/1/standalone/2026-08-01_fixture-article-1.md"
    assert first_checksum == second.checksum
    assert first_attempts == 1
    assert second.attempts == 2
    assert "script" not in content
    document = yaml.safe_load(content.split("---\n", 2)[1])
    assert document["source_url"] == "https://example.com/feed"
    assert document["security_context"] == "UNTRUSTED_EXTERNAL_DATA"


def test_markdown_frontmatter_quotes_ambiguous_values(article_factory) -> None:
    article = article_factory(1, 1)
    article.title = "yes"
    markdown = build_markdown(article, ArchiveMetadata("source", "https://example.com/feed"))
    assert "title: 'yes'" in markdown or "title: yes" in markdown
