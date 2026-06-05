import json
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from hermes_pulse.models import CitationLink, CollectedItem, IntentSignals, ItemTimestamps, Provenance
from hermes_pulse.title_resolution import fetch_title_from_url, synthesize_title_with_codex_spark

Runner = Callable[[str, str], dict[str, Any]]
TitleFetcher = Callable[[str], str | None]
TitleSynthesizer = Callable[[str, str], str | None]

SignalType = str
_REQUEST_FIELDS = "tweet.fields=created_at,author_id,text,entities"
_SIGNAL_PATHS: dict[SignalType, tuple[str, str]] = {
    "bookmarks": ("x_bookmarks", "/2/users/{user_id}/bookmarks?max_results=100&" + _REQUEST_FIELDS),
    "likes": ("x_likes", "/2/users/{user_id}/liked_tweets?max_results=100&" + _REQUEST_FIELDS),
    "home_timeline_reverse_chronological": (
        "x_home_timeline_reverse_chronological",
        "/2/users/{user_id}/timelines/reverse_chronological?max_results=100&" + _REQUEST_FIELDS,
    ),
}
_SPEND_CAP_ERROR_TITLES = {"CreditsDepleted", "SpendCapReached"}


class XUrlConnector:
    id = "x_signals"
    source_family = "x"

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        title_fetcher: TitleFetcher | None = None,
        title_synthesizer: TitleSynthesizer | None = None,
        max_external_title_resolutions: int = 3,
        enable_title_synthesis: bool = False,
    ) -> None:
        self._runner = runner or _run_xurl_json
        self._title_fetcher = title_fetcher or fetch_title_from_url
        self._title_synthesizer = title_synthesizer or synthesize_title_with_codex_spark
        self._max_external_title_resolutions = max_external_title_resolutions
        self._enable_title_synthesis = enable_title_synthesis

    def collect(self, signal_types: Sequence[str]) -> list[CollectedItem]:
        unsupported = [signal_type for signal_type in signal_types if signal_type not in _SIGNAL_PATHS]
        if unsupported:
            raise ValueError(f"Unsupported X signal type: {unsupported[0]}")

        if not signal_types:
            return []

        auth_type, me_payload = self._resolve_auth("/2/users/me")
        me = me_payload.get("data") or {}
        user_id = me.get("id")
        if not user_id:
            raise ValueError("xurl /2/users/me did not return a user id")

        items: list[CollectedItem] = []
        resolved_external_titles = 0
        for signal_type in signal_types:
            source, path_template = _SIGNAL_PATHS[signal_type]
            payload = self._runner(path_template.format(user_id=user_id), auth_type)
            new_items, used_external_title_resolutions = _parse_items(
                source,
                signal_type,
                payload,
                title_fetcher=self._title_fetcher,
                title_synthesizer=self._title_synthesizer,
                remaining_external_title_resolutions=max(self._max_external_title_resolutions - resolved_external_titles, 0),
                enable_title_synthesis=self._enable_title_synthesis,
            )
            items.extend(new_items)
            resolved_external_titles += used_external_title_resolutions
        return items

    def _resolve_auth(self, path: str) -> tuple[str, dict[str, Any]]:
        last_error: Exception | None = None
        for auth_type in ("oauth2", "oauth1"):
            try:
                return auth_type, self._runner(path, auth_type)
            except Exception as exc:
                if _is_spend_cap_error(str(exc)):
                    raise exc
                last_error = exc
        assert last_error is not None
        raise last_error


def _parse_items(
    source: str,
    signal_type: str,
    payload: dict[str, Any],
    *,
    title_fetcher: TitleFetcher,
    title_synthesizer: TitleSynthesizer,
    remaining_external_title_resolutions: int,
    enable_title_synthesis: bool,
) -> tuple[list[CollectedItem], int]:
    users = {
        user.get("id"): user
        for user in ((payload.get("includes") or {}).get("users") or [])
        if user.get("id")
    }
    items: list[CollectedItem] = []
    used_external_title_resolutions = 0
    for record in payload.get("data") or []:
        tweet_id = record["id"]
        text = record.get("text") or ""
        author = users.get(record.get("author_id"), {})
        username = author.get("username")
        tweet_url = f"https://x.com/{username}/status/{tweet_id}" if username else f"https://x.com/i/web/status/{tweet_id}"
        target_url = _extract_target_url(record) or tweet_url
        title, used_external_resolution = _resolve_title(
            text=text,
            target_url=target_url,
            tweet_url=tweet_url,
            title_fetcher=title_fetcher,
            title_synthesizer=title_synthesizer,
            allow_external_resolution=used_external_title_resolutions < remaining_external_title_resolutions,
            enable_title_synthesis=enable_title_synthesis,
        )
        if used_external_resolution:
            used_external_title_resolutions += 1
        intent = IntentSignals(saved=signal_type == "bookmarks", liked=signal_type == "likes")
        items.append(
            CollectedItem(
                id=f"{source}:{tweet_id}",
                source=source,
                source_kind="post",
                title=title,
                excerpt=text,
                body=text,
                url=target_url,
                timestamps=ItemTimestamps(created_at=record.get("created_at")),
                intent_signals=intent,
                provenance=Provenance(
                    provider="x.com",
                    acquisition_mode="official_api",
                    authority_tier="primary",
                    primary_source_url=target_url,
                    raw_record_id=tweet_id,
                ),
                citation_chain=[CitationLink(label=title, url=target_url, relation="primary")],
                metadata={
                    "x_signal": signal_type,
                    "author_id": record.get("author_id"),
                    "author_username": username,
                    "tweet_url": tweet_url,
                    "target_url": target_url,
                },
            )
        )
    return items, used_external_title_resolutions


def _extract_target_url(record: dict[str, Any]) -> str | None:
    entities = record.get("entities") or {}
    urls = entities.get("urls") or []
    for candidate in urls:
        if not isinstance(candidate, dict):
            continue
        for key in ("expanded_url", "unwound_url", "url"):
            value = candidate.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
    return None


def _resolve_title(
    *,
    text: str,
    target_url: str,
    tweet_url: str,
    title_fetcher: TitleFetcher,
    title_synthesizer: TitleSynthesizer,
    allow_external_resolution: bool,
    enable_title_synthesis: bool,
) -> tuple[str, bool]:
    if target_url == tweet_url:
        return _title_from_text(text), False
    if not allow_external_resolution:
        return _title_from_text(text), False
    fetched_title = title_fetcher(target_url)
    if fetched_title:
        return _normalize_title(fetched_title), True
    if enable_title_synthesis:
        synthesized_title = title_synthesizer(text, target_url)
        if synthesized_title:
            return _normalize_title(synthesized_title), True
    return _title_from_text(text), True


def _normalize_title(text: str) -> str:
    compact = " ".join(text.split())
    return compact[:120] if compact else "X post"


def _title_from_text(text: str) -> str:
    compact = " ".join(text.split())
    return compact[:80] if compact else "X post"


def _run_xurl_json(path: str, auth_type: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["xurl", "--auth", auth_type, path],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(_format_xurl_failure(path, auth_type, error)) from error
    return json.loads(result.stdout)


def _format_xurl_failure(path: str, auth_type: str, error: subprocess.CalledProcessError) -> str:
    stderr = (error.stderr or "").strip()
    stdout = (error.stdout or "").strip()
    detail = _extract_xurl_error_detail(stderr) or _extract_xurl_error_detail(stdout)
    suffix = f": {detail}" if detail else ""
    return f"xurl {auth_type} {path} failed{suffix}"


def _is_spend_cap_error(message: str) -> bool:
    normalized = message.lower()
    return (
        "creditsdepleted" in normalized
        or "spendcapreached" in normalized
        or "spend cap" in normalized
        or "does not have any credits" in normalized
    )


def _extract_xurl_error_detail(text: str) -> str | None:
    if not text:
        return None
    decoder = json.JSONDecoder()
    for start_index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[start_index:])
        except json.JSONDecodeError:
            continue
        detail = _detail_from_xurl_payload(payload)
        if detail:
            return detail
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _detail_from_xurl_payload(payload: Any) -> str | None:
    if isinstance(payload, dict):
        title = payload.get("title")
        detail = payload.get("detail")
        if isinstance(title, str) and (title in _SPEND_CAP_ERROR_TITLES or _is_spend_cap_error(title)):
            return _format_spend_cap_detail(title, detail if isinstance(detail, str) else None)
        if isinstance(detail, str) and _is_spend_cap_error(detail):
            return _format_spend_cap_detail(title if isinstance(title, str) else "SpendCapReached", detail)
        if isinstance(title, str) and isinstance(detail, str):
            return f"{title}: {detail}"
        if isinstance(detail, str):
            return detail
        if isinstance(title, str):
            return title
    return None


def _format_spend_cap_detail(title: str, detail: str | None) -> str:
    reset_date = _extract_reset_date(detail or "")
    suffix = f"; blocked until {reset_date}" if reset_date else ""
    return f"{title}: X API spend cap reached{suffix}"


def _extract_reset_date(text: str) -> str | None:
    import re

    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    return match.group(1) if match else None
