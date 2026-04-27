import logging
import urllib.error
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from hermes_pulse.models import CitationLink, CollectedItem, Provenance, SourceRegistryEntry


logger = logging.getLogger(__name__)
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HermesPulse/0.1; +https://github.com/2001Y/HermesPulse)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5

SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
BING_RSS_ENDPOINT = "https://www.bing.com/search?format=rss"


class KnownSourceSearchConnector:
    id = "known_source_search"
    source_family = "known_source_search"

    def __init__(
        self,
        fetcher: Callable[[str], str] | None = None,
        error_handler: Callable[[str, str], None] | None = None,
        success_handler: Callable[[str], None] | None = None,
    ) -> None:
        self._fetcher = fetcher or _fetch_url
        self._error_handler = error_handler
        self._success_handler = success_handler

    def collect(self, entries: Sequence[SourceRegistryEntry]) -> list[CollectedItem]:
        items: list[CollectedItem] = []
        for entry in entries:
            if entry.acquisition_mode != "known_source_search":
                continue
            query = _build_search_query(entry)
            try:
                direct_items = _collect_direct_items(entry, fetcher=self._fetcher, query=query)
                if direct_items is not None:
                    items.extend(direct_items)
                else:
                    items.extend(_collect_search_items(entry, query=query, fetcher=self._fetcher))
                if self._success_handler is not None:
                    self._success_handler(entry.id)
            except Exception as exc:
                logger.warning("Skipping known source search %s after fetch/parse failure: %s", entry.id, exc)
                if self._error_handler is not None:
                    self._error_handler(entry.id, str(exc))
        return items

    def _parse_items(self, entry: SourceRegistryEntry, payload: str, query: str) -> list[CollectedItem]:
        parser = _DuckDuckGoHTMLParser()
        parser.feed(payload)
        parser.close()
        return _build_result_items(entry, parser.results, query=query)


@dataclass(slots=True)
class _SearchResult:
    title: str | None = None
    url: str | None = None
    snippet: str | None = None


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[_SearchResult] = []
        self._current = _SearchResult()
        self._capture_title = False
        self._capture_snippet = False
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())

        if "result__a" in classes:
            self._flush_current_if_complete()
            self._current.url = attributes.get("href")
            self._capture_title = True
            self._title_parts = []
            return

        if "result__snippet" in classes and self._current.url:
            self._capture_snippet = True
            self._snippet_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_title:
            self._capture_title = False
            self._current.title = _clean_text(self._title_parts)
            self._title_parts = []
            return

        if self._capture_snippet and tag in {"a", "div", "span"}:
            self._capture_snippet = False
            self._current.snippet = _clean_text(self._snippet_parts)
            self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._title_parts.append(data)
        if self._capture_snippet:
            self._snippet_parts.append(data)

    def close(self) -> None:
        super().close()
        self._flush_current_if_complete(force=True)

    def _flush_current_if_complete(self, force: bool = False) -> None:
        if self._current.url and (force or self._current.title or self._current.snippet):
            self.results.append(self._current)
            self._current = _SearchResult()
            self._capture_title = False
            self._capture_snippet = False
            self._title_parts = []
            self._snippet_parts = []


def _build_search_query(entry: SourceRegistryEntry) -> str:
    hints = [hint.strip() for hint in entry.search_hints if hint.strip()]
    if any(hint.startswith("site:") for hint in hints):
        return " ".join(hints)
    parts = [f"site:{entry.domain}"]
    parts.extend(hints)
    return " ".join(parts)


def _build_search_url(query: str) -> str:
    return f"{SEARCH_ENDPOINT}?q={quote_plus(query)}"


def _build_bing_rss_url(query: str) -> str:
    return f"{BING_RSS_ENDPOINT}&q={quote_plus(query)}"


def _fetch_url(url: str) -> str:
    request = Request(url, headers=DEFAULT_HEADERS)
    with urlopen(request, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8")


def _collect_search_items(
    entry: SourceRegistryEntry,
    *,
    query: str,
    fetcher: Callable[[str], str],
) -> list[CollectedItem]:
    try:
        payload = fetcher(_build_search_url(query))
        return _parse_duckduckgo_items(entry, payload, query)
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            raise
        logger.info("DuckDuckGo HTML search returned 403 for %s; retrying via Bing RSS", entry.id)
        payload = fetcher(_build_bing_rss_url(query))
        return _parse_bing_rss_items(entry, payload, query)


def _parse_duckduckgo_items(entry: SourceRegistryEntry, payload: str, query: str) -> list[CollectedItem]:
    parser = _DuckDuckGoHTMLParser()
    parser.feed(payload)
    parser.close()
    return _build_result_items(entry, parser.results, query=query)


def _parse_bing_rss_items(entry: SourceRegistryEntry, payload: str, query: str) -> list[CollectedItem]:
    root = ElementTree.fromstring(payload)
    results: list[_SearchResult] = []
    for item in root.findall('./channel/item'):
        results.append(
            _SearchResult(
                title=(item.findtext('title') or '').strip() or None,
                url=(item.findtext('link') or '').strip() or None,
                snippet=(item.findtext('description') or '').strip() or None,
            )
        )
    return _build_result_items(entry, results, query=query)


def _build_result_items(entry: SourceRegistryEntry, results: Sequence[_SearchResult], *, query: str) -> list[CollectedItem]:
    relation = "primary" if entry.authority_tier == "primary" else "secondary"
    parsed_items: list[CollectedItem] = []
    search_rank = 0
    for result in results:
        resolved_url = _resolve_result_url(result.url)
        if resolved_url is None or not _url_matches_domain(resolved_url, entry.domain):
            continue
        search_rank += 1
        title = result.title or resolved_url
        parsed_items.append(
            CollectedItem(
                id=f"{entry.id}:{resolved_url}",
                source=entry.id,
                source_kind="document",
                title=title,
                excerpt=result.snippet,
                url=resolved_url,
                provenance=Provenance(
                    provider=entry.domain,
                    acquisition_mode=entry.acquisition_mode,
                    authority_tier=entry.authority_tier,
                    primary_source_url=resolved_url,
                    raw_record_id=resolved_url,
                ),
                citation_chain=[CitationLink(label=title, url=resolved_url, relation=relation)],
                metadata={
                    "search_query": query,
                    "search_rank": search_rank,
                },
            )
        )
    return parsed_items


def _collect_direct_items(
    entry: SourceRegistryEntry,
    *,
    fetcher: Callable[[str], str],
    query: str,
) -> list[CollectedItem] | None:
    if _supports_anthropic_news_sitemap(entry):
        payload = fetcher('https://www.anthropic.com/sitemap.xml')
        urls = _extract_sitemap_urls(payload, prefix='https://www.anthropic.com/news/')
        return _build_direct_items(entry, urls, query=query) if urls else None
    if _supports_xai_news_page(entry):
        payload = fetcher('https://x.ai/news')
        urls = _extract_news_page_urls(payload, base_url='https://x.ai/news', path_prefix='/news/')
        return _build_direct_items(entry, urls, query=query) if urls else None
    return None


def _supports_anthropic_news_sitemap(entry: SourceRegistryEntry) -> bool:
    return any(hint.strip().startswith('site:anthropic.com/news') for hint in entry.search_hints)


def _supports_xai_news_page(entry: SourceRegistryEntry) -> bool:
    return any(hint.strip().startswith('site:x.ai/news') for hint in entry.search_hints)


def _extract_sitemap_urls(payload: str, *, prefix: str) -> list[str]:
    root = ElementTree.fromstring(payload)
    urls: list[str] = []
    for element in root.iter():
        if element.tag.rsplit('}', 1)[-1] != 'loc' or element.text is None:
            continue
        url = element.text.strip()
        if url.startswith(prefix):
            urls.append(url)
    return urls


class _NewsLinkParser(HTMLParser):
    def __init__(self, *, base_url: str, path_prefix: str) -> None:
        super().__init__()
        self._base_url = base_url
        self._path_prefix = path_prefix
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != 'a':
            return
        href = dict(attrs).get('href')
        if href is None:
            return
        resolved = urljoin(self._base_url, href)
        parsed = urlparse(resolved)
        if parsed.path.startswith(self._path_prefix):
            self.urls.append(resolved)


def _extract_news_page_urls(payload: str, *, base_url: str, path_prefix: str) -> list[str]:
    parser = _NewsLinkParser(base_url=base_url, path_prefix=path_prefix)
    parser.feed(payload)
    parser.close()
    deduped: list[str] = []
    seen: set[str] = set()
    for url in parser.urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


def _build_direct_items(entry: SourceRegistryEntry, urls: Sequence[str], *, query: str) -> list[CollectedItem]:
    relation = 'primary' if entry.authority_tier == 'primary' else 'secondary'
    items: list[CollectedItem] = []
    for rank, resolved_url in enumerate(urls, start=1):
        if not _url_matches_domain(resolved_url, entry.domain):
            continue
        slug = resolved_url.rstrip('/').split('/')[-1].replace('-', ' ')
        title = slug[:1].upper() + slug[1:] if slug else resolved_url
        items.append(
            CollectedItem(
                id=f"{entry.id}:{resolved_url}",
                source=entry.id,
                source_kind='document',
                title=title,
                url=resolved_url,
                provenance=Provenance(
                    provider=entry.domain,
                    acquisition_mode=entry.acquisition_mode,
                    authority_tier=entry.authority_tier,
                    primary_source_url=resolved_url,
                    raw_record_id=resolved_url,
                ),
                citation_chain=[CitationLink(label=title, url=resolved_url, relation=relation)],
                metadata={'search_query': query, 'search_rank': rank},
            )
        )
    return items


def _resolve_result_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        url = f"https:{url}"

    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        if target:
            return unquote(target)
    return url


def _url_matches_domain(url: str, domain: str) -> bool:
    host = urlparse(url).hostname
    if host is None:
        return False
    normalized_host = host.lower()
    normalized_domain = domain.lower()
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def _clean_text(parts: list[str]) -> str | None:
    text = " ".join(part.strip() for part in parts if part.strip())
    return text or None
