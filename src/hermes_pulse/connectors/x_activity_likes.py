import html
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from hermes_pulse.models import CitationLink, CollectedItem, IntentSignals, ItemTimestamps, Provenance

OEmbedFetcher = Callable[[str], dict[str, Any]]
ErrorHandler = Callable[[str], None]


class XActivityLikesConnector:
    id = "x_activity_likes"
    source_family = "x"

    def __init__(
        self,
        *,
        event_log: str | Path,
        expected_user_id: str,
        oembed_fetcher: OEmbedFetcher | None = None,
        error_handler: ErrorHandler | None = None,
    ) -> None:
        self._event_log = Path(event_log)
        self._expected_user_id = expected_user_id
        self._oembed_fetcher = oembed_fetcher or _fetch_oembed
        self._error_handler = error_handler

    def collect(self) -> list[CollectedItem]:
        if not self._event_log.exists():
            return []
        items: list[CollectedItem] = []
        seen_event_ids: set[str] = set()
        for line_number, line in enumerate(self._event_log.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                self._record_error(f"malformed X Activity JSON at line {line_number}")
                continue
            data = event.get("data") if isinstance(event, dict) else None
            if not isinstance(data, dict) or not self._is_expected_outbound_like(data):
                continue
            event_id = data.get("event_uuid")
            payload = data.get("payload")
            if not isinstance(event_id, str) or event_id in seen_event_ids or not isinstance(payload, dict):
                continue
            tweet_id = payload.get("liked_tweet_id")
            if not isinstance(tweet_id, str) or not tweet_id.isdigit():
                self._record_error(f"X Activity like {event_id} did not include a numeric liked_tweet_id")
                continue
            seen_event_ids.add(event_id)
            tweet_url = _canonical_post_url(data, payload=payload, tweet_id=tweet_id)
            if tweet_url is None:
                self._record_error(f"X Activity like {event_id} did not include the liked Post author username")
                continue
            item = self._hydrate_like(
                event_id=event_id,
                tweet_id=tweet_id,
                tweet_url=tweet_url,
                payload=payload,
            )
            if item is not None:
                items.append(item)
        return items

    def _is_expected_outbound_like(self, data: dict[str, Any]) -> bool:
        event_filter = data.get("filter")
        return (
            data.get("event_type") == "like.create"
            and isinstance(event_filter, dict)
            and event_filter.get("user_id") == self._expected_user_id
            and event_filter.get("direction") == "outbound"
        )

    def _hydrate_like(
        self,
        *,
        event_id: str,
        tweet_id: str,
        tweet_url: str,
        payload: dict[str, Any],
    ) -> CollectedItem | None:
        try:
            oembed = self._oembed_fetcher(tweet_url)
        except Exception as exc:
            self._record_error(f"official oEmbed failed for X post {tweet_id}: {exc}")
            return None
        text = _extract_oembed_text(oembed.get("html"))
        title = _compact(text)[:80] or "Liked X post"
        return CollectedItem(
            id=f"x_likes:{event_id}",
            source="x_likes",
            source_kind="post",
            title=title,
            excerpt=text,
            body=text,
            url=tweet_url,
            timestamps=ItemTimestamps(created_at=_optional_string(payload.get("created_at"))),
            intent_signals=IntentSignals(liked=True),
            provenance=Provenance(
                provider="x.com",
                acquisition_mode="official_api",
                authority_tier="primary",
                primary_source_url=tweet_url,
                raw_record_id=event_id,
            ),
            citation_chain=[CitationLink(label=title, url=tweet_url, relation="primary")],
            metadata={
                "x_signal": "likes",
                "liked_tweet_id": tweet_id,
                "tweet_url": tweet_url,
                "author_name": oembed.get("author_name"),
            },
        )

    def _record_error(self, message: str) -> None:
        if self._error_handler is not None:
            self._error_handler(message)


def _canonical_post_url(data: dict[str, Any], *, payload: dict[str, Any], tweet_id: str) -> str | None:
    author_id = payload.get("tweet_author_id")
    includes = data.get("includes")
    users = includes.get("users") if isinstance(includes, dict) else None
    if not isinstance(author_id, str) or not isinstance(users, list):
        return None
    for user in users:
        if not isinstance(user, dict) or user.get("id") != author_id:
            continue
        username = user.get("username")
        if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
            return f"https://x.com/{username}/status/{tweet_id}"
    return None


def _fetch_oembed(tweet_url: str) -> dict[str, Any]:
    query = urlencode({"url": tweet_url, "omit_script": "true", "dnt": "true"})
    request = Request(
        f"https://publish.twitter.com/oembed?{query}",
        headers={"User-Agent": "HermesPulse/1.0"},
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("official oEmbed returned a non-object response")
    return payload


def _extract_oembed_text(raw_html: Any) -> str:
    if not isinstance(raw_html, str):
        return ""
    paragraph = re.search(r"<p(?:\s[^>]*)?>(.*?)</p>", raw_html, flags=re.IGNORECASE | re.DOTALL)
    content = paragraph.group(1) if paragraph is not None else raw_html
    content = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
    content = re.sub(r"<[^>]+>", "", content)
    return _compact(html.unescape(content))


def _compact(text: str) -> str:
    return " ".join(text.split())


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
