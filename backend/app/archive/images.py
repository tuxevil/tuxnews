from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urljoin

import nh3
from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from app.archive.paths import AtomicArchiveWriter
from app.core.config import get_settings
from app.db.models import Article
from app.ingestion.http_client import HttpFetchError, SafeHttpClient

IMAGE_EXTENSIONS = {
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/svg+xml": "svg",
    "image/webp": "webp",
}


class ImageDownloader:
    def __init__(self, client: SafeHttpClient | None = None) -> None:
        self.client = client

    async def download_and_rewrite(
        self,
        article: Article,
        writer: AtomicArchiveWriter,
        *,
        base_url: str | None = None,
        target_dir: Path | None = None,
    ) -> int:
        """Download article images into ``target_dir/assets`` and rewrite markdown sources.

        When ``target_dir`` is the folder that will hold the Markdown document, the
        rewritten ``src`` attributes are relative to it (``assets/<name>``) so the
        export remains readable offline as a folder.
        """
        if article.id is None or not article.content_clean:
            return 0
        soup = BeautifulSoup(nh3.clean(article.content_clean), "html.parser")
        own_client = self.client is None
        client = self.client or SafeHttpClient()
        downloaded = 0
        first_path: str | None = None
        try:
            for image in soup.find_all("img"):
                raw_url = image.get("src")
                if not isinstance(raw_url, str) or not raw_url.strip():
                    image.decompose()
                    continue
                image_url = urljoin(base_url, raw_url) if base_url else raw_url
                try:
                    result = await client.fetch(
                        image_url,
                        allowed_mime_types=("image/*",),
                        max_bytes=get_settings().image_max_bytes,
                    )
                    extension = IMAGE_EXTENSIONS.get(result.content_type)
                    if extension is None:
                        raise HttpFetchError("unsupported image type")
                    digest = hashlib.sha256(result.url.encode("utf-8")).hexdigest()[:16]
                    if target_dir is not None:
                        directory = target_dir / "assets"
                    else:
                        directory = Path("images") / str(article.user_id) / str(article.id)
                    stored = writer.write_bytes(directory / f"{digest}.{extension}", result.content)
                except (HttpFetchError, OSError, ValueError):
                    image.decompose()
                    continue
                if target_dir is not None:
                    image["src"] = Path(stored.relative_path).relative_to(target_dir).as_posix()
                else:
                    image["src"] = stored.relative_path
                image.attrs.pop("srcset", None)
                downloaded += 1
                first_path = first_path or stored.relative_path
        finally:
            if own_client:
                await client.aclose()
        article.content_clean = str(soup)
        article.image_local_path = first_path
        return downloaded
