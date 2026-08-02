from __future__ import annotations

import nh3
import trafilatura  # type: ignore[import-untyped]


def extract_article_text(content: bytes, *, url: str) -> str | None:
    """Extract plain article text while treating the page as untrusted input."""

    html = content.decode("utf-8", errors="replace")
    try:
        extracted = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            output_format="txt",
        )
    except Exception:
        return None
    if not isinstance(extracted, str):
        return None
    cleaned = nh3.clean(extracted, tags=set()).strip()
    return cleaned or None
