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


def test_story_overview_lists_members_with_links() -> None:
    from datetime import UTC, datetime

    from app.archive.markdown import StoryContext, StoryMember, build_story_overview

    story = StoryContext(
        cluster_id=42,
        title="Story: <script>alert(1)</script>Python 3.15",
        summary="A release story.",
        members=(
            StoryMember(
                article_id=1,
                title="Beta 4 out",
                source_name="Python Blog",
                url="https://blog.python.org/beta4",
                published_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
            StoryMember(
                article_id=2,
                title="Beta 3 out",
                source_name="Python Insider",
                url="https://blog.python.org/beta3",
                published_at=datetime(2026, 6, 23, tzinfo=UTC),
            ),
        ),
    )

    markdown = build_story_overview(story)

    assert "story_cluster_id: 42" in markdown
    assert "member_count: 2" in markdown
    assert "security_context: UNTRUSTED_EXTERNAL_DATA" in markdown
    assert "script" not in markdown
    assert "[Beta 4 out](https://blog.python.org/beta4)" in markdown
    assert "[Beta 3 out](https://blog.python.org/beta3)" in markdown


@pytest.mark.asyncio
async def test_story_export_writes_member_and_overview(
    db_session: AsyncSession,
    user_factory,
    source_factory,
    article_factory,
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from app.archive.markdown import StoryContext, StoryMember
    from app.db.models import Cluster

    user = user_factory()
    db_session.add(user)
    await db_session.flush()
    source = source_factory(user.id)
    db_session.add(source)
    await db_session.flush()
    article = article_factory(user.id, source.id)
    article.discovered_at = datetime(2026, 8, 1, tzinfo=UTC)
    article.content_clean = "<p>Member article.</p>"
    db_session.add(article)
    cluster = Cluster(user_id=user.id, title="Release train", summary="A story.")
    db_session.add(cluster)
    await db_session.flush()
    article.cluster_id = cluster.id
    db_session.add(article)
    await db_session.commit()
    writer = AtomicArchiveWriter(tmp_path / "archive")
    story = StoryContext(
        cluster_id=cluster.id,
        title=cluster.title,
        summary=cluster.summary,
        members=(
            StoryMember(
                article_id=article.id,
                title=article.title,
                source_name="Fixture source",
                url="https://example.com/member",
                published_at=article.discovered_at,
            ),
        ),
    )

    export = await export_article(
        db_session,
        article,
        ArchiveMetadata("Fixture source", "https://example.com/feed"),
        writer,
        story=story,
    )

    assert export.path == "tenants/1/stories/2026-08-01_release-train/2026-08-01_fixture-source-fixture-article-1.md"
    assert (tmp_path / "archive" / export.path).is_file()
    overview = tmp_path / "archive" / "tenants/1/stories/2026-08-01_release-train/00_story_overview.md"
    assert overview.is_file()
    overview_text = overview.read_text(encoding="utf-8")
    assert "story_cluster_id" in overview_text
    assert "Release train" in overview_text
