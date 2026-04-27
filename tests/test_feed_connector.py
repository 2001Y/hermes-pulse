from pathlib import Path
from urllib.request import Request

import hermes_pulse.connectors.feed_registry as feed_registry_module
from hermes_pulse.connectors.feed_registry import FeedRegistryConnector
from hermes_pulse.models import SourceRegistryEntry
from hermes_pulse.source_registry import load_source_registry


FIXTURE_XML = Path("fixtures/feed_samples/official_feed.xml").read_text()
ARTICLE_PAGE_HTML = Path("fixtures/feed_samples/article_page.html").read_text()
RDF_FIXTURE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns="http://purl.org/rss/1.0/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <channel rdf:about="https://applech2.com/">
    <title>Applech2</title>
    <link>https://applech2.com/</link>
    <description>Apple rumors and news.</description>
  </channel>
  <item rdf:about="https://applech2.com/2026/04/index-update">
    <title>Index update</title>
    <link>https://applech2.com/2026/04/index-update</link>
    <description>Rumor roundup.</description>
  </item>
</rdf:RDF>
"""
APPLE_NEWSROOM_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Apple Newsroom</title>
  <entry>
    <title>Apple expands something</title>
    <id>https://www.apple.com/newsroom/2026/04/apple-expands-something/</id>
    <link href="https://www.apple.com/newsroom/2026/04/apple-expands-something/"/>
    <updated>2026-04-20T17:00:00Z</updated>
    <summary>Important newsroom update.</summary>
  </entry>
</feed>
"""
SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.zeiss.com/news/launch-update</loc></url>
  <url><loc>https://www.zeiss.com/news/camera-two</loc></url>
  <url><loc>https://other.example.net/ignored</loc></url>
</urlset>
"""


def test_feed_registry_connector_collects_feed_items_with_provenance() -> None:
    entries = load_source_registry(Path("fixtures/source_registry/sample_sources.yaml"))
    connector = FeedRegistryConnector(fetcher=lambda url: FIXTURE_XML)

    items = connector.collect(entries)

    assert len(items) == 2

    official_item = next(item for item in items if item.source == "official-blog")
    assert official_item.source_kind == "feed_item"
    assert official_item.title == "Launch update"
    assert official_item.url == "https://example.com/posts/launch-update"
    assert official_item.provenance is not None
    assert official_item.provenance.authority_tier == "primary"
    assert official_item.provenance.acquisition_mode == "rss_poll"
    assert official_item.citation_chain[0].relation == "primary"
    assert official_item.citation_chain[0].url == "https://example.com/posts/launch-update"
    assert official_item.body is None

    secondary_item = next(item for item in items if item.source == "trusted-secondary-blog")
    assert secondary_item.provenance is not None
    assert secondary_item.provenance.authority_tier == "trusted_secondary"
    assert secondary_item.provenance.acquisition_mode == "atom_poll"


def test_feed_registry_connector_collects_rdf_feed_items_with_provenance() -> None:
    entry = SourceRegistryEntry(
        id="applech2",
        source_family="news",
        domain="applech2.com",
        title="Applech2",
        acquisition_mode="rss_poll",
        authority_tier="trusted_secondary",
        rss_url="https://applech2.com/index.rdf",
    )
    connector = FeedRegistryConnector(fetcher=lambda url: RDF_FIXTURE_XML)

    items = connector.collect([entry])

    assert len(items) == 1
    item = items[0]
    assert item.id == "applech2:https://applech2.com/2026/04/index-update"
    assert item.source == "applech2"
    assert item.title == "Index update"
    assert item.excerpt == "Rumor roundup."
    assert item.url == "https://applech2.com/2026/04/index-update"
    assert item.provenance is not None
    assert item.provenance.primary_source_url == "https://applech2.com/2026/04/index-update"
    assert item.citation_chain[0].relation == "secondary"


def test_feed_registry_connector_collects_atom_entries_with_link_href_and_updated_timestamp() -> None:
    entry = SourceRegistryEntry(
        id="apple-newsroom",
        source_family="official_newsroom",
        domain="apple.com",
        title="Apple Newsroom",
        acquisition_mode="rss_poll",
        authority_tier="primary",
        rss_url="https://www.apple.com/newsroom/rss-feed.rss",
    )

    items = FeedRegistryConnector(fetcher=lambda url: APPLE_NEWSROOM_ATOM).collect([entry])

    assert len(items) == 1
    item = items[0]
    assert item.title == "Apple expands something"
    assert item.url == "https://www.apple.com/newsroom/2026/04/apple-expands-something/"
    assert item.excerpt == "Important newsroom update."
    assert item.timestamps is not None
    assert item.timestamps.created_at == "2026-04-20T17:00:00Z"


def test_feed_registry_connector_collects_sitemap_urls_as_document_items() -> None:
    entry = SourceRegistryEntry(
        id="zeiss-cine",
        source_family="official_cine_news",
        domain="zeiss.com",
        title="ZEISS Cine",
        acquisition_mode="rss_poll",
        authority_tier="primary",
        rss_url="https://www.zeiss.com/sitemap.xml",
    )

    items = FeedRegistryConnector(fetcher=lambda url: SITEMAP_XML).collect([entry])

    assert [item.url for item in items] == [
        "https://www.zeiss.com/news/launch-update",
        "https://www.zeiss.com/news/camera-two",
    ]
    assert [item.source_kind for item in items] == ["document", "document"]
    assert items[0].title == "Launch update"
    assert items[0].provenance is not None
    assert items[0].provenance.acquisition_mode == "rss_poll"


def test_feed_registry_connector_enriches_body_from_article_page_when_available() -> None:
    entries = load_source_registry(Path("fixtures/source_registry/sample_sources.yaml"))
    connector = FeedRegistryConnector(
        fetcher=lambda url: FIXTURE_XML,
        page_fetcher=lambda url: ARTICLE_PAGE_HTML,
    )

    items = connector.collect(entries)

    official_item = next(item for item in items if item.source == "official-blog")
    assert official_item.body == (
        "Launch update The launch is now available to all users. "
        "Review the migration steps before enabling it in production."
    )


def test_feed_registry_connector_continues_when_article_body_fetch_fails() -> None:
    entries = load_source_registry(Path("fixtures/source_registry/sample_sources.yaml"))
    connector = FeedRegistryConnector(
        fetcher=lambda url: FIXTURE_XML,
        page_fetcher=lambda url: (_ for _ in ()).throw(TimeoutError("timed out")),
    )

    items = connector.collect(entries)

    official_item = next(item for item in items if item.source == "official-blog")
    assert official_item.title == "Launch update"
    assert official_item.body is None


def test_feed_registry_connector_fetches_live_payloads_with_browser_headers_when_no_fetcher_is_provided(
    monkeypatch,
) -> None:
    requests: list[Request] = []

    class DummyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return FIXTURE_XML.encode("utf-8")

    def fake_urlopen(request: Request, *args, **kwargs) -> DummyResponse:
        requests.append(request)
        assert kwargs.get("timeout") == 20
        return DummyResponse()

    monkeypatch.setattr(feed_registry_module, "urlopen", fake_urlopen)
    entry = SourceRegistryEntry(
        id="official-blog",
        source_family="company_updates",
        domain="example.com",
        title="Example Official Blog",
        acquisition_mode="rss_poll",
        authority_tier="primary",
        rss_url="https://example.com/feed.xml",
    )

    items = FeedRegistryConnector().collect([entry])

    assert len(requests) == 1
    request = requests[0]
    assert isinstance(request, Request)
    assert request.full_url == "https://example.com/feed.xml"
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["user-agent"]
    assert headers["accept"]
    assert [item.title for item in items] == ["Launch update"]


def test_feed_registry_connector_continues_when_a_feed_fetch_fails() -> None:
    entries = [
        SourceRegistryEntry(
            id="broken-feed",
            source_family="company_updates",
            domain="broken.example.com",
            title="Broken Feed",
            acquisition_mode="rss_poll",
            authority_tier="primary",
            rss_url="https://broken.example.com/feed.xml",
        ),
        SourceRegistryEntry(
            id="official-blog",
            source_family="company_updates",
            domain="example.com",
            title="Example Official Blog",
            acquisition_mode="rss_poll",
            authority_tier="primary",
            rss_url="https://example.com/feed.xml",
        ),
    ]

    def fetcher(url: str) -> str:
        if url == "https://broken.example.com/feed.xml":
            raise TimeoutError("timed out")
        return FIXTURE_XML

    items = FeedRegistryConnector(fetcher=fetcher).collect(entries)

    assert [item.source for item in items] == ["official-blog"]
    assert [item.title for item in items] == ["Launch update"]


def test_feed_registry_connector_continues_when_a_feed_parse_fails() -> None:
    entries = [
        SourceRegistryEntry(
            id="malformed-feed",
            source_family="company_updates",
            domain="malformed.example.com",
            title="Malformed Feed",
            acquisition_mode="rss_poll",
            authority_tier="primary",
            rss_url="https://malformed.example.com/feed.xml",
        ),
        SourceRegistryEntry(
            id="official-blog",
            source_family="company_updates",
            domain="example.com",
            title="Example Official Blog",
            acquisition_mode="rss_poll",
            authority_tier="primary",
            rss_url="https://example.com/feed.xml",
        ),
    ]

    def fetcher(url: str) -> str:
        if url == "https://malformed.example.com/feed.xml":
            return "<rss><channel><item>"
        return FIXTURE_XML

    items = FeedRegistryConnector(fetcher=fetcher).collect(entries)

    assert [item.source for item in items] == ["official-blog"]
    assert [item.title for item in items] == ["Launch update"]


def test_feed_registry_connector_reports_per_source_errors_to_callback() -> None:
    entries = [
        SourceRegistryEntry(
            id="broken-feed",
            source_family="company_updates",
            domain="broken.example.com",
            title="Broken Feed",
            acquisition_mode="rss_poll",
            authority_tier="primary",
            rss_url="https://broken.example.com/feed.xml",
        ),
        SourceRegistryEntry(
            id="official-blog",
            source_family="company_updates",
            domain="example.com",
            title="Example Official Blog",
            acquisition_mode="rss_poll",
            authority_tier="primary",
            rss_url="https://example.com/feed.xml",
        ),
    ]
    reported_errors: list[tuple[str, str]] = []
    reported_successes: list[str] = []

    def fetcher(url: str) -> str:
        if url == "https://broken.example.com/feed.xml":
            raise TimeoutError("timed out")
        return FIXTURE_XML

    connector = FeedRegistryConnector(
        fetcher=fetcher,
        error_handler=lambda entry_id, message: reported_errors.append((entry_id, message)),
        success_handler=lambda entry_id: reported_successes.append(entry_id),
    )

    items = connector.collect(entries)

    assert [item.source for item in items] == ["official-blog"]
    assert reported_errors == [("broken-feed", "timed out")]
    assert reported_successes == ["official-blog"]
