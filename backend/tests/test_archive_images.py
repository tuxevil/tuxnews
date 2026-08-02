from pathlib import Path

import pytest
from app.archive.images import ImageDownloader
from app.archive.paths import AtomicArchiveWriter
from app.db.models import Article
from app.ingestion.http_client import FetchResult


class FakeImageClient:
    async def fetch(self, url: str, **_: object) -> FetchResult:
        if "private" in url:
            raise ValueError("private image")
        return FetchResult(
            url=url,
            status_code=200,
            headers={"content-type": "image/png"},
            content=b"png-bytes",
        )


@pytest.mark.asyncio
async def test_images_are_downloaded_confined_and_rewritten(tmp_path: Path) -> None:
    article = Article(
        id=7,
        user_id=3,
        source_id=1,
        title="Image article",
        original_title="Image article",
        url="https://example.com/article",
        canonical_url_hash="a" * 64,
        content_clean=(
            '<p>Text</p><img src="/hero.png" srcset="/hero-2x.png 2x">'
            '<img src="http://private.invalid/private"><script>bad()</script>'
        ),
    )
    writer = AtomicArchiveWriter(tmp_path / "archive")
    target_dir = Path("tenants/3/standalone/2026-08-01_image-article")

    downloaded = await ImageDownloader(FakeImageClient()).download_and_rewrite(
        article,
        writer,
        base_url="https://example.com/feed.xml",
        target_dir=target_dir,
    )

    assert downloaded == 1
    assert article.image_local_path is not None
    assert article.image_local_path.startswith("tenants/3/standalone/2026-08-01_image-article/assets/")
    assert "https://" not in (article.content_clean or "")
    assert "private" not in (article.content_clean or "")
    assert "script" not in (article.content_clean or "")
    assert "src=\"assets/" in (article.content_clean or "")
    assert (tmp_path / "archive" / article.image_local_path).read_bytes() == b"png-bytes"
