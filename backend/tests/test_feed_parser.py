from datetime import UTC

import pytest
from app.ingestion.feed_parser import canonicalize_url, parse_feed

RSS_FEED = b"""
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>Example feed</title>
    <item>
      <title>First story</title>
      <link>https://Example.com/story/?utm_source=mail&amp;b=2&amp;a=1#comments</link>
      <guid>story-1</guid>
      <pubDate>Wed, 02 Oct 2002 08:00:00 GMT</pubDate>
      <description><![CDATA[<p>Safe summary</p><script>alert(1)</script>]]></description>
      <author>author@example.com</author>
      <category>python</category>
      <media:content url="/images/story.jpg" type="image/jpeg" />
    </item>
    <item>
      <title>First story updated</title>
      <link>https://example.com/story?a=1&amp;b=2</link>
      <guid>story-1-update</guid>
      <pubDate>Thu, 03 Oct 2002 08:00:00 GMT</pubDate>
      <description>Updated summary</description>
    </item>
    <item>
      <link>javascript:alert(1)</link>
    </item>
  </channel>
</rss>
"""

ATOM_FEED = b"""
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom example</title>
  <entry>
    <title>Atom story</title>
    <link rel="alternate" href="/atom-story" />
    <id>tag:example.com,2026:atom-story</id>
    <updated>2026-08-01T12:00:00Z</updated>
    <summary>Atom summary</summary>
    <category term="linux" />
  </entry>
</feed>
"""


def test_canonicalize_url_removes_tracking_and_normalizes_path() -> None:
    assert (
        canonicalize_url("HTTPS://Example.com:443/a/../story/?utm_campaign=x&b=2&a=1#fragment")
        == "https://example.com/story?a=1&b=2"
    )


def test_rss_entries_are_sanitized_canonicalized_and_deduplicated() -> None:
    result = parse_feed(RSS_FEED, source_url="https://example.com/feed")

    assert len(result.entries) == 1
    assert len(result.errors) == 1
    entry = result.entries[0]
    assert entry.title == "First story updated"
    assert entry.canonical_url == "https://example.com/story?a=1&b=2"
    assert entry.published_at is not None
    assert entry.published_at.tzinfo == UTC
    assert entry.content == "Updated summary"


def test_atom_entries_support_relative_links_and_categories() -> None:
    result = parse_feed(ATOM_FEED, source_url="https://example.com/feeds/atom.xml")

    assert not result.errors
    assert len(result.entries) == 1
    assert result.entries[0].url == "https://example.com/atom-story"
    assert result.entries[0].tags == ("linux",)


@pytest.mark.parametrize(
    "url",
    ["ftp://example.com/feed", "javascript:alert(1)", "https://user:pass@example.com/feed"],
)
def test_canonicalize_url_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_url(url)
