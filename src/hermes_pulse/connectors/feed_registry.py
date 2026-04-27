import logging
from collections.abc import Callable, Iterator, Sequence
from html import unescape
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from hermes_pulse.models import CitationLink, CollectedItem, ItemTimestamps, Provenance, SourceRegistryEntry


logger = logging.getLogger(__name__)
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HermesPulse/0.1; +https://github.com/2001Y/HermesPulse)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
}
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_ARTICLE_BODY_MAX_LENGTH = 1200
MAX_ITEMS_PER_SOURCE = 20


class FeedRegistryConnector:
    id = "feed_registry"
    source_family = "feed_registry"

    def __init__(
        self,
        fetcher: Callable[[str], str] | None = None,
        page_fetcher: Callable[[str], str] | None = None,
        error_handler: Callable[[str, str], None] | None = None,
        success_handler: Callable[[str], None] | None = None,
    ) -> None:
        self._fetcher = fetcher or _fetch_url
        self._page_fetcher = page_fetcher
        self._error_handler = error_handler
        self._success_handler = success_handler

    def collect(self, entries: Sequence[SourceRegistryEntry]) -> list[CollectedItem]:
        items: list[CollectedItem] = []
        for entry in entries:
            if not entry.rss_url:
                continue
            try:
                payload = self._fetcher(entry.rss_url)
                items.extend(
                    self._parse_items(
                        entry,
                        payload,
                        source_url=entry.rss_url,
                        visited_urls={entry.rss_url},
                        remaining_budget=MAX_ITEMS_PER_SOURCE,
                    )
                )
                if self._success_handler is not None:
                    self._success_handler(entry.id)
            except Exception as exc:
                logger.warning("Skipping feed source %s after fetch/parse failure: %s", entry.id, exc)
                if self._error_handler is not None:
                    self._error_handler(entry.id, str(exc))
        return items

    def _parse_items(
        self,
        entry: SourceRegistryEntry,
        payload: str,
        *,
        source_url: str,
        visited_urls: set[str],
        remaining_budget: int,
    ) -> list[CollectedItem]:
        if remaining_budget <= 0:
            return []
        root = ElementTree.fromstring(payload)
        if _local_name(root.tag) in {"urlset", "sitemapindex"}:
            return self._parse_sitemap_items(
                entry,
                root,
                source_url=source_url,
                visited_urls=visited_urls,
                remaining_budget=remaining_budget,
            )
        parsed_items: list[CollectedItem] = []
        for raw_item in _iter_feed_items(root):
            if len(parsed_items) >= remaining_budget:
                break
            title = _text(raw_item, "title")
            url = _item_link(raw_item)
            guid = _text(raw_item, "guid") or _text(raw_item, "id") or url or title or entry.id
            published_at = _text(raw_item, "pubDate") or _text(raw_item, "updated") or _text(raw_item, "published")
            excerpt = _text(raw_item, "description") or _text(raw_item, "summary")
            relation = "primary" if entry.authority_tier == "primary" else "secondary"
            parsed_items.append(
                CollectedItem(
                    id=f"{entry.id}:{guid}",
                    source=entry.id,
                    source_kind="feed_item",
                    title=title,
                    excerpt=excerpt,
                    body=self._fetch_article_body(url),
                    url=url,
                    timestamps=ItemTimestamps(created_at=published_at),
                    provenance=Provenance(
                        provider=entry.domain,
                        acquisition_mode=entry.acquisition_mode,
                        authority_tier=entry.authority_tier,
                        primary_source_url=url,
                        raw_record_id=guid,
                    ),
                    citation_chain=[CitationLink(label=title or entry.title, url=url or entry.rss_url, relation=relation)],
                )
            )
        return parsed_items

    def _parse_sitemap_items(
        self,
        entry: SourceRegistryEntry,
        root: ElementTree.Element,
        *,
        source_url: str,
        visited_urls: set[str],
        remaining_budget: int,
    ) -> list[CollectedItem]:
        if remaining_budget <= 0:
            return []
        root_name = _local_name(root.tag)
        if root_name == "sitemapindex":
            nested_items: list[CollectedItem] = []
            for sitemap in _children(root, "sitemap"):
                if len(nested_items) >= remaining_budget:
                    break
                nested_url = _text(sitemap, "loc")
                if not nested_url or nested_url in visited_urls:
                    continue
                visited_urls.add(nested_url)
                payload = self._fetcher(nested_url)
                nested_items.extend(
                    self._parse_items(
                        entry,
                        payload,
                        source_url=nested_url,
                        visited_urls=visited_urls,
                        remaining_budget=remaining_budget - len(nested_items),
                    )
                )
            return nested_items

        relation = "primary" if entry.authority_tier == "primary" else "secondary"
        parsed_items: list[CollectedItem] = []
        for index, url_node in enumerate(_children(root, "url"), start=1):
            if len(parsed_items) >= remaining_budget:
                break
            url = _text(url_node, "loc")
            if not url or not _url_matches_domain(url, entry.domain):
                continue
            title = _title_from_url(url)
            parsed_items.append(
                CollectedItem(
                    id=f"{entry.id}:{url}",
                    source=entry.id,
                    source_kind="document",
                    title=title,
                    url=url,
                    provenance=Provenance(
                        provider=entry.domain,
                        acquisition_mode=entry.acquisition_mode,
                        authority_tier=entry.authority_tier,
                        primary_source_url=url,
                        raw_record_id=url,
                    ),
                    citation_chain=[CitationLink(label=title or entry.title, url=url, relation=relation)],
                    metadata={"sitemap_source_url": source_url, "search_rank": index},
                )
            )
        return parsed_items

    def _fetch_article_body(self, url: str | None) -> str | None:
        if not url or self._page_fetcher is None:
            return None
        try:
            payload = self._page_fetcher(url)
        except Exception as exc:
            logger.warning("Skipping article body fetch %s after fetch failure: %s", url, exc)
            return None
        return _extract_article_text(payload)


class _ArticleBodyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if tag in {"script", "style", "noscript", "head", "title"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag in {"script", "style", "noscript", "head", "title"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in {"p", "div", "article", "section", "main", "h1", "h2", "h3", "li", "br"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text)

    def text(self) -> str:
        plain_text = unescape(" ".join(self._parts))
        normalized = " ".join(plain_text.split())
        if len(normalized) <= DEFAULT_ARTICLE_BODY_MAX_LENGTH:
            return normalized
        return f"{normalized[: DEFAULT_ARTICLE_BODY_MAX_LENGTH - 1].rstrip()}…"


def _extract_article_text(payload: str) -> str | None:
    parser = _ArticleBodyParser()
    parser.feed(payload)
    parser.close()
    text = parser.text()
    return text or None


def _fetch_url(url: str) -> str:
    request = Request(url, headers=DEFAULT_HEADERS)
    with urlopen(request, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8")


def _iter_feed_items(root: ElementTree.Element) -> Iterator[ElementTree.Element]:
    channel = _child(root, "channel")
    if channel is not None:
        yield from _children(channel, "item")
    yield from _children(root, "item")
    yield from _children(root, "entry")


def _child(element: ElementTree.Element, tag: str) -> ElementTree.Element | None:
    for child in element:
        if _local_name(child.tag) == tag:
            return child
    return None


def _children(element: ElementTree.Element, tag: str) -> Iterator[ElementTree.Element]:
    for child in element:
        if _local_name(child.tag) == tag:
            yield child


def _text(element: ElementTree.Element, tag: str) -> str | None:
    node = _child(element, tag)
    if node is None or node.text is None:
        return None
    return node.text.strip()


def _item_link(element: ElementTree.Element) -> str | None:
    node = _child(element, "link")
    if node is None:
        return None
    href = node.attrib.get('href')
    if href:
        return href.strip()
    if node.text is None:
        return None
    return node.text.strip()


def _url_matches_domain(url: str, domain: str) -> bool:
    from urllib.parse import urlparse

    host = urlparse(url).hostname
    if host is None:
        return False
    normalized_host = host.lower()
    normalized_domain = domain.lower()
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def _title_from_url(url: str) -> str:
    slug = url.rstrip('/').split('/')[-1].replace('-', ' ').replace('_', ' ')
    if not slug:
        return url
    return slug[:1].upper() + slug[1:]


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    if ":" in tag:
        return tag.rsplit(":", 1)[-1]
    return tag
