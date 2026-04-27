from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import hermes_pulse.connectors.known_source_search as known_source_search_module
from hermes_pulse.connectors.known_source_search import KnownSourceSearchConnector
from hermes_pulse.models import SourceRegistryEntry


FIXTURE_HTML = Path("fixtures/search_samples/known_source_results.html").read_text()
ANTHROPIC_SITEMAP_XML = """<?xml version='1.0' encoding='UTF-8'?>
<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
  <url><loc>https://www.anthropic.com/news/introducing-claude-opus-4-7</loc></url>
  <url><loc>https://www.anthropic.com/news/claude-for-financial-services</loc></url>
</urlset>
"""
XAI_NEWS_HTML = """<!doctype html><html><body>
<a href='/news/grok-4'>Grok 4</a>
<a href='/news/grok-business'>Grok Business</a>
<a href='/about'>About</a>
</body></html>
"""
BING_RSS_XML = """<?xml version='1.0' encoding='utf-8'?>
<rss version='2.0'>
  <channel>
    <item>
      <title>Zeiss cine update</title>
      <link>https://www.zeiss.com/cine-lenses/us/news-events/news/supreme-prime-radiance.html</link>
      <description>ZEISS announces a cine lens update.</description>
    </item>
    <item>
      <title>Ignored off-domain result</title>
      <link>https://example.com/not-zeiss</link>
      <description>Should be filtered out.</description>
    </item>
  </channel>
</rss>
"""


def test_known_source_search_connector_collects_domain_constrained_results_with_provenance() -> None:
    entry = SourceRegistryEntry(
        id="discovery-only-source",
        source_family="discovery_blog",
        domain="discover.example.net",
        title="Discovery Source",
        acquisition_mode="known_source_search",
        authority_tier="discovery_only",
        search_hints=["rumors", "supply chain"],
        topical_scopes=["discovery"],
        language="en",
        requires_primary_confirmation=True,
    )

    connector = KnownSourceSearchConnector(fetcher=lambda url: FIXTURE_HTML)

    items = connector.collect([entry])

    assert len(items) == 1
    item = items[0]
    assert item.id == "discovery-only-source:https://discover.example.net/2026/04/discovery-scoop"
    assert item.source == "discovery-only-source"
    assert item.source_kind == "document"
    assert item.title == "Discovery scoop"
    assert item.excerpt == "A focused rumor roundup from a curated source."
    assert item.url == "https://discover.example.net/2026/04/discovery-scoop"
    assert item.provenance is not None
    assert item.provenance.provider == "discover.example.net"
    assert item.provenance.acquisition_mode == "known_source_search"
    assert item.provenance.authority_tier == "discovery_only"
    assert item.provenance.primary_source_url == "https://discover.example.net/2026/04/discovery-scoop"
    assert item.provenance.raw_record_id == "https://discover.example.net/2026/04/discovery-scoop"
    assert item.citation_chain[0].label == "Discovery scoop"
    assert item.citation_chain[0].url == "https://discover.example.net/2026/04/discovery-scoop"
    assert item.citation_chain[0].relation == "secondary"
    assert item.metadata["search_rank"] == 1
    assert item.metadata["search_query"] == "site:discover.example.net rumors supply chain"


def test_known_source_search_connector_builds_site_scoped_query_and_skips_non_search_entries() -> None:
    requested_urls: list[str] = []

    def fetcher(url: str) -> str:
        requested_urls.append(url)
        return FIXTURE_HTML

    entries = [
        SourceRegistryEntry(
            id="discovery-only-source",
            source_family="discovery_blog",
            domain="discover.example.net",
            title="Discovery Source",
            acquisition_mode="known_source_search",
            authority_tier="discovery_only",
            search_hints=["rumors", "supply chain"],
        ),
        SourceRegistryEntry(
            id="official-blog",
            source_family="company_updates",
            domain="example.com",
            title="Example Official Blog",
            acquisition_mode="rss_poll",
            authority_tier="primary",
            rss_url="https://example.com/feed.xml",
            search_hints=["official updates"],
        ),
    ]

    items = KnownSourceSearchConnector(fetcher=fetcher).collect(entries)

    assert len(items) == 1
    assert len(requested_urls) == 1
    parsed = urlparse(requested_urls[0])
    assert parsed.netloc == "html.duckduckgo.com"
    assert parsed.path == "/html/"
    assert parse_qs(parsed.query)["q"] == ["site:discover.example.net rumors supply chain"]


def test_known_source_search_connector_does_not_duplicate_site_scope_when_hint_already_contains_site() -> None:
    requested_urls: list[str] = []

    def fetcher(url: str) -> str:
        requested_urls.append(url)
        return ANTHROPIC_SITEMAP_XML if url == "https://www.anthropic.com/sitemap.xml" else FIXTURE_HTML

    entry = SourceRegistryEntry(
        id="anthropic-newsroom",
        source_family="official_lab_news",
        domain="anthropic.com",
        title="Anthropic Newsroom",
        acquisition_mode="known_source_search",
        authority_tier="primary",
        search_hints=["site:anthropic.com/news Anthropic announcement"],
    )

    items = KnownSourceSearchConnector(fetcher=fetcher).collect([entry])

    assert requested_urls == ["https://www.anthropic.com/sitemap.xml"]
    assert [item.url for item in items] == [
        "https://www.anthropic.com/news/introducing-claude-opus-4-7",
        "https://www.anthropic.com/news/claude-for-financial-services",
    ]


def test_known_source_search_connector_uses_anthropic_sitemap_when_supported() -> None:
    requested_urls: list[str] = []

    def fetcher(url: str) -> str:
        requested_urls.append(url)
        if url == "https://www.anthropic.com/sitemap.xml":
            return ANTHROPIC_SITEMAP_XML
        raise AssertionError(url)

    entry = SourceRegistryEntry(
        id="anthropic-newsroom",
        source_family="official_lab_news",
        domain="anthropic.com",
        title="Anthropic Newsroom",
        acquisition_mode="known_source_search",
        authority_tier="primary",
        search_hints=["site:anthropic.com/news Anthropic announcement"],
    )

    items = KnownSourceSearchConnector(fetcher=fetcher).collect([entry])

    assert requested_urls == ["https://www.anthropic.com/sitemap.xml"]
    assert [item.url for item in items] == [
        "https://www.anthropic.com/news/introducing-claude-opus-4-7",
        "https://www.anthropic.com/news/claude-for-financial-services",
    ]


def test_known_source_search_connector_uses_xai_news_page_when_supported() -> None:
    requested_urls: list[str] = []

    def fetcher(url: str) -> str:
        requested_urls.append(url)
        if url == "https://x.ai/news":
            return XAI_NEWS_HTML
        raise AssertionError(url)

    entry = SourceRegistryEntry(
        id="xai-news",
        source_family="official_lab_news",
        domain="x.ai",
        title="xAI News",
        acquisition_mode="known_source_search",
        authority_tier="primary",
        search_hints=["site:x.ai/news xAI announcement"],
    )

    items = KnownSourceSearchConnector(fetcher=fetcher).collect([entry])

    assert requested_urls == ["https://x.ai/news"]
    assert [item.url for item in items] == [
        "https://x.ai/news/grok-4",
        "https://x.ai/news/grok-business",
    ]


def test_known_source_search_connector_keeps_path_scoped_anthropic_entries_on_search_fallback() -> None:
    requested_urls: list[str] = []

    def fetcher(url: str) -> str:
        requested_urls.append(url)
        return FIXTURE_HTML

    entry = SourceRegistryEntry(
        id="anthropic-engineering",
        source_family="official_engineering_blog",
        domain="anthropic.com",
        title="Anthropic Engineering",
        acquisition_mode="known_source_search",
        authority_tier="primary",
        search_hints=["site:anthropic.com/engineering Anthropic engineering"],
    )

    items = KnownSourceSearchConnector(fetcher=fetcher).collect([entry])

    assert len(items) == 0
    parsed = urlparse(requested_urls[0])
    assert parsed.netloc == "html.duckduckgo.com"
    assert parse_qs(parsed.query)["q"] == ["site:anthropic.com/engineering Anthropic engineering"]


def test_known_source_search_connector_falls_back_to_search_when_direct_source_yields_no_items() -> None:
    requested_urls: list[str] = []

    def fetcher(url: str) -> str:
        requested_urls.append(url)
        if url == "https://x.ai/news":
            return "<html><body><a href='/about'>About</a></body></html>"
        if url.startswith("https://html.duckduckgo.com/html/?q="):
            return FIXTURE_HTML
        raise AssertionError(url)

    entry = SourceRegistryEntry(
        id="xai-news",
        source_family="official_lab_news",
        domain="x.ai",
        title="xAI News",
        acquisition_mode="known_source_search",
        authority_tier="primary",
        search_hints=["site:x.ai/news xAI announcement"],
    )

    items = KnownSourceSearchConnector(fetcher=fetcher).collect([entry])

    assert requested_urls == [
        "https://x.ai/news",
        "https://html.duckduckgo.com/html/?q=site%3Ax.ai%2Fnews+xAI+announcement",
    ]
    assert items == []


def test_known_source_search_connector_reports_per_source_errors_to_callback() -> None:
    entry = SourceRegistryEntry(
        id="discovery-only-source",
        source_family="discovery_blog",
        domain="discover.example.net",
        title="Discovery Source",
        acquisition_mode="known_source_search",
        authority_tier="discovery_only",
        search_hints=["rumors", "supply chain"],
    )
    reported_errors: list[tuple[str, str]] = []

    connector = KnownSourceSearchConnector(
        fetcher=lambda url: (_ for _ in ()).throw(TimeoutError("search timed out")),
        error_handler=lambda entry_id, message: reported_errors.append((entry_id, message)),
    )

    items = connector.collect([entry])

    assert items == []
    assert reported_errors == [("discovery-only-source", "search timed out")]


def test_known_source_search_connector_falls_back_to_bing_rss_when_duckduckgo_returns_403() -> None:
    requested_urls: list[str] = []

    def fetcher(url: str) -> str:
        requested_urls.append(url)
        if url.startswith("https://html.duckduckgo.com/html/?q="):
            raise HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)
        if url.startswith("https://www.bing.com/search?format=rss&q="):
            return BING_RSS_XML
        raise AssertionError(url)

    entry = SourceRegistryEntry(
        id="zeiss-cine",
        source_family="official_cine_news",
        domain="zeiss.com",
        title="ZEISS Cine",
        acquisition_mode="known_source_search",
        authority_tier="primary",
        search_hints=["site:zeiss.com cine lens supreme radiance nano announcement"],
    )

    items = KnownSourceSearchConnector(fetcher=fetcher).collect([entry])

    assert requested_urls == [
        "https://html.duckduckgo.com/html/?q=site%3Azeiss.com+cine+lens+supreme+radiance+nano+announcement",
        "https://www.bing.com/search?format=rss&q=site%3Azeiss.com+cine+lens+supreme+radiance+nano+announcement",
    ]
    assert [item.url for item in items] == [
        "https://www.zeiss.com/cine-lenses/us/news-events/news/supreme-prime-radiance.html"
    ]
    assert items[0].title == "Zeiss cine update"


def test_known_source_search_fetches_live_payloads_with_browser_headers_and_timeout_when_no_fetcher_is_provided(
    monkeypatch,
) -> None:
    requests: list[Request] = []
    timeouts: list[object] = []

    class DummyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return FIXTURE_HTML.encode("utf-8")

    def fake_urlopen(request: Request, *args, **kwargs) -> DummyResponse:
        requests.append(request)
        timeouts.append(kwargs.get("timeout"))
        return DummyResponse()

    monkeypatch.setattr(known_source_search_module, "urlopen", fake_urlopen)
    entry = SourceRegistryEntry(
        id="discovery-only-source",
        source_family="discovery_blog",
        domain="discover.example.net",
        title="Discovery Source",
        acquisition_mode="known_source_search",
        authority_tier="discovery_only",
        search_hints=["rumors", "supply chain"],
    )

    items = KnownSourceSearchConnector().collect([entry])

    assert len(requests) == 1
    request = requests[0]
    assert isinstance(request, Request)
    assert request.full_url == "https://html.duckduckgo.com/html/?q=site%3Adiscover.example.net+rumors+supply+chain"
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["user-agent"]
    assert headers["accept"]
    assert timeouts == [5]
    assert len(items) == 1
