import argparse
import importlib.util
import inspect
import json
import re
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Protocol

from hermes_pulse.archive import write_morning_digest_archive
from hermes_pulse.cli import _archive_label_for_args, _apply_replay_window_if_requested, _build_digest_with_source_errors, _occurred_at_for_command
from hermes_pulse.summarization import CodexCliSummarizer
from hermes_pulse.summarization.base import CODEX_DIGEST_RELATIVE_PATH, RAW_ITEMS_RELATIVE_PATH, SummaryArtifact
from hermes_pulse.summarization.codex_cli import (
    DEFAULT_CODEX_MODEL,
    DEFAULT_SUMMARY_FORMAT,
    build_summary_format_instructions,
)


DEFAULT_SLACK_DIRECT_PATH = Path.home() / ".hermes" / "scripts" / "slack_direct.py"
DEFAULT_SLACK_MESSAGE_LIMIT = 3500
DEFAULT_RETRY_DELAYS_SECONDS = (300, 300)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
HTML_TAG_RE = re.compile(r"<[^>]+>")
SIGNIFICANT_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{1,}[A-Za-z0-9]|[一-龯ぁ-んァ-ンー]{3,}")
AUTOLINK_MIN_SCORE = 0.40
AUTOLINK_STOPWORDS = {
    "ai",
    "it",
    "ev",
    "ニュース",
    "報道",
    "発表",
    "発売",
    "開始",
    "検討",
    "更新",
    "予定",
    "導入",
}


class SlackPoster(Protocol):
    def __call__(
        self,
        text: str,
        channel: str,
        thread_ts: str | None = None,
        *,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
        blocks: list[dict[str, Any]] | None = None,
    ) -> Any:
        ...


@dataclass(frozen=True)
class DirectDeliveryResult:
    archive_directory: Path
    digest_path: Path
    content: str
    posted_messages: list[str]
    slack_response: Any
    slack_responses: list[Any]


@dataclass(frozen=True)
class _DigestLinkCandidate:
    url: str
    text: str
    normalized_text: str
    grams: frozenset[str]
    tokens: frozenset[str]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-pulse-direct-delivery")
    parser.add_argument("--command", choices=("morning-digest", "evening-digest"), default="morning-digest")
    parser.add_argument("--source-registry", type=Path)
    parser.add_argument("--feed-fixture", type=Path)
    parser.add_argument("--search-fixture", type=Path)
    parser.add_argument("--calendar-fixture", type=Path)
    parser.add_argument("--gmail-fixture", type=Path)
    parser.add_argument("--chatgpt-history", type=Path)
    parser.add_argument("--grok-history", type=Path)
    parser.add_argument("--hermes-history", type=Path)
    parser.add_argument("--notes", type=Path)
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--archive-label")
    parser.add_argument("--window-start")
    parser.add_argument("--window-end")
    parser.add_argument("--now")
    parser.add_argument("--x-signals")
    parser.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL)
    parser.add_argument("--summary-format", default=DEFAULT_SUMMARY_FORMAT)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--thread-ts")
    return parser


def main(argv: Sequence[str] | None = None, *, post_message: SlackPoster | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    run_digest_direct_delivery(args, post_message=post_message)
    return 0


def run_digest_direct_delivery(
    args: argparse.Namespace,
    *,
    post_message: SlackPoster | None = None,
) -> DirectDeliveryResult:
    command = getattr(args, "command", "morning-digest")
    items, source_errors, _successful_sources = _build_digest_with_source_errors(command, args)
    archive_root = args.archive_root or Path.home() / "Pulse"
    occurred_at = _occurred_at_for_command(command, args)
    archive_directory = write_morning_digest_archive(
        items=items,
        archive_root=archive_root,
        archive_date=_archive_label_for_args(args),
        retrieved_at=occurred_at,
    )
    _write_source_errors_metadata(archive_directory, source_errors)
    _apply_replay_window_if_requested(
        archive_directory,
        archive_root=archive_root,
        args=args,
    )
    artifact = _summarize_archive_with_retries(
        archive_directory,
        codex_model=args.codex_model,
        summary_format=args.summary_format,
        digest_command=command,
    )
    return post_canonical_digest_to_slack(
        archive_directory,
        channel=args.channel,
        thread_ts=args.thread_ts,
        post_message=post_message,
        summary_artifact=artifact,
    )


def run_morning_digest_direct_delivery(
    args: argparse.Namespace,
    *,
    post_message: SlackPoster | None = None,
) -> DirectDeliveryResult:
    return run_digest_direct_delivery(args, post_message=post_message)


def _summarize_archive_with_retries(
    archive_directory: Path,
    *,
    codex_model: str = DEFAULT_CODEX_MODEL,
    summary_format: str = DEFAULT_SUMMARY_FORMAT,
    digest_command: str = "morning-digest",
    retry_delays_seconds: Sequence[int] = DEFAULT_RETRY_DELAYS_SECONDS,
    summarizer_factory: Callable[..., Any] | None = None,
    sleep: Callable[[int], None] = time.sleep,
) -> SummaryArtifact:
    last_error: Exception | None = None
    attempts = len(tuple(retry_delays_seconds)) + 1
    delays = list(retry_delays_seconds)
    factory = summarizer_factory or CodexCliSummarizer
    attempt_metadata: list[dict[str, Any]] = []
    metadata_path = archive_directory / "metadata" / "codex-attempts.json"
    for attempt_index in range(attempts):
        started_at = _utc_now_isoformat()
        try:
            summarizer = _build_summarizer(
                factory,
                codex_model=codex_model,
                summary_format=summary_format,
                digest_command=digest_command,
            )
            artifact = summarizer.summarize_archive(archive_directory)
            attempt_metadata.append(
                {
                    "attempt": attempt_index + 1,
                    "status": "succeeded",
                    "started_at": started_at,
                    "finished_at": _utc_now_isoformat(),
                    "error": None,
                }
            )
            _persist_codex_attempt_metadata(
                metadata_path,
                codex_model=codex_model,
                summary_format=summary_format,
                attempts=attempt_metadata,
            )
            return artifact
        except Exception as error:
            last_error = error
            attempt_metadata.append(
                {
                    "attempt": attempt_index + 1,
                    "status": "failed",
                    "started_at": started_at,
                    "finished_at": _utc_now_isoformat(),
                    "error": str(error),
                }
            )
            _persist_codex_attempt_metadata(
                metadata_path,
                codex_model=codex_model,
                summary_format=summary_format,
                attempts=attempt_metadata,
            )
            if attempt_index >= len(delays):
                break
            sleep(delays[attempt_index])
    assert last_error is not None
    raise last_error


def _build_summarizer(
    factory: Callable[..., Any],
    *,
    codex_model: str,
    summary_format: str,
    digest_command: str,
) -> Any:
    kwargs: dict[str, Any] = {"model": codex_model, "summary_format": summary_format}
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        parameters = {}
    supports_var_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    if supports_var_kwargs or "digest_command" in parameters:
        kwargs["digest_command"] = digest_command
    return factory(**kwargs)


def _persist_codex_attempt_metadata(
    path: Path,
    *,
    codex_model: str,
    summary_format: str,
    attempts: list[dict[str, Any]],
) -> None:
    try:
        _write_codex_attempt_metadata(
            path,
            codex_model=codex_model,
            summary_format=summary_format,
            attempts=attempts,
        )
    except OSError:
        return


def _write_codex_attempt_metadata(
    path: Path,
    *,
    codex_model: str,
    summary_format: str,
    attempts: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "model": codex_model,
                "summary_format": summary_format,
                "attempts": attempts,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _utc_now_isoformat() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def post_canonical_digest_to_slack(
    archive_directory: str | Path,
    *,
    channel: str,
    thread_ts: str | None = None,
    post_message: SlackPoster | None = None,
    slack_message_limit: int = DEFAULT_SLACK_MESSAGE_LIMIT,
    summary_artifact: SummaryArtifact | None = None,
) -> DirectDeliveryResult:
    archive_directory = Path(archive_directory)
    digest_path = archive_directory / CODEX_DIGEST_RELATIVE_PATH
    if not digest_path.exists():
        raise FileNotFoundError(f"Canonical Codex digest artifact is missing: {digest_path}")

    content = digest_path.read_text()
    linked_content = _autolink_digest_markdown_from_archive(content, archive_directory)
    rendered_message = _prepend_grok_fallback_notice_if_needed(
        _prepend_source_error_notice_if_needed(_render_digest_for_slack(linked_content), archive_directory),
        archive_directory,
    )
    message_chunks = _split_slack_digest_text(rendered_message, limit=slack_message_limit)
    message_chunk_blocks = [_build_slack_blocks(chunk) for chunk in message_chunks]
    poster = post_message or load_slack_direct_post_message()
    slack_responses = _post_slack_chunks(
        poster,
        message_chunks,
        blocks_per_chunk=message_chunk_blocks,
        channel=channel,
        thread_ts=thread_ts,
    )
    slack_response = slack_responses[-1]
    return DirectDeliveryResult(
        archive_directory=archive_directory,
        digest_path=digest_path,
        content=content,
        posted_messages=message_chunks,
        slack_response=slack_response,
        slack_responses=slack_responses,
    )


def load_slack_direct_post_message(script_path: str | Path = DEFAULT_SLACK_DIRECT_PATH) -> SlackPoster:
    script_path = Path(script_path)
    if not script_path.exists():
        raise FileNotFoundError(f"Slack direct poster script is missing: {script_path}")

    spec = importlib.util.spec_from_file_location("hermes_pulse_slack_direct", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Slack direct poster script: {script_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    post_message = getattr(module, "post_message", None)
    if not callable(post_message):
        raise RuntimeError(f"Slack direct poster script does not define callable post_message: {script_path}")
    return post_message


def _render_digest_for_slack(markdown: str) -> str:
    return MARKDOWN_LINK_RE.sub(lambda match: f"<{match.group(2)}|{match.group(1)}>", markdown)


def _autolink_digest_markdown_from_archive(markdown: str, archive_directory: Path) -> str:
    candidates = _load_digest_link_candidates(archive_directory)
    if not candidates:
        return markdown
    lines = [_autolink_digest_line(line, candidates) for line in markdown.splitlines()]
    trailing_newline = "\n" if markdown.endswith("\n") else ""
    return "\n".join(lines) + trailing_newline


def _load_digest_link_candidates(archive_directory: Path) -> list[_DigestLinkCandidate]:
    raw_items_path = archive_directory / RAW_ITEMS_RELATIVE_PATH
    if not raw_items_path.exists():
        return []
    try:
        payload = json.loads(raw_items_path.read_text())
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []

    candidates: list[_DigestLinkCandidate] = []
    seen: set[tuple[str, str]] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        for url, text in _iter_link_candidate_values(item):
            normalized_text = _normalize_for_digest_link_match(text)
            if not normalized_text:
                continue
            key = (url, normalized_text[:240])
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                _DigestLinkCandidate(
                    url=url,
                    text=text,
                    normalized_text=normalized_text,
                    grams=frozenset(_character_grams(normalized_text)),
                    tokens=frozenset(_significant_tokens(text)),
                )
            )
    return candidates


def _iter_link_candidate_values(item: dict[str, Any]) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    item_url = item.get("url")
    item_text = " ".join(
        part for part in (_plain_text(item.get(field_name)) for field_name in ("title", "excerpt", "body")) if part
    )
    if isinstance(item_url, str) and _is_http_url(item_url) and item_text:
        values.append((item_url, item_text))

    citation_chain = item.get("citation_chain") or []
    if isinstance(citation_chain, list):
        for citation in citation_chain:
            if not isinstance(citation, dict):
                continue
            citation_url = citation.get("url")
            citation_label = _plain_text(citation.get("label"))
            if isinstance(citation_url, str) and _is_http_url(citation_url) and citation_label:
                item_title = _plain_text(item.get("title"))
                values.append((citation_url, " ".join(part for part in (citation_label, item_title) if part)))
    return values


def _autolink_digest_line(line: str, candidates: list[_DigestLinkCandidate]) -> str:
    stripped = line.lstrip()
    indentation = line[: len(line) - len(stripped)]
    if not stripped.startswith("- "):
        return line
    if _line_already_has_link(stripped):
        return line

    bullet_text = stripped[2:]
    candidate = _best_link_candidate_for_bullet(bullet_text, candidates)
    if candidate is None:
        return line
    anchor = _select_digest_link_anchor(bullet_text, candidate.text)
    if not anchor:
        return line
    return f"{indentation}- {bullet_text.replace(anchor, f'[{anchor}]({candidate.url})', 1)}"


def _line_already_has_link(line: str) -> bool:
    return bool(
        MARKDOWN_LINK_RE.search(line)
        or re.search(r"<https?://[^|>]+\|[^>]+>", line)
        or re.search(r"https?://", line)
    )


def _best_link_candidate_for_bullet(
    bullet_text: str,
    candidates: list[_DigestLinkCandidate],
) -> _DigestLinkCandidate | None:
    normalized_bullet = _normalize_for_digest_link_match(bullet_text)
    if not normalized_bullet:
        return None
    bullet_grams = _character_grams(normalized_bullet)
    bullet_tokens = _significant_tokens(bullet_text)
    best_candidate: _DigestLinkCandidate | None = None
    best_score = 0.0
    for candidate in candidates:
        score = _digest_link_match_score(normalized_bullet, bullet_grams, bullet_tokens, candidate)
        if score > best_score:
            best_score = score
            best_candidate = candidate
    if best_candidate is None or best_score < AUTOLINK_MIN_SCORE:
        return None
    return best_candidate


def _digest_link_match_score(
    normalized_bullet: str,
    bullet_grams: set[str],
    bullet_tokens: set[str],
    candidate: _DigestLinkCandidate,
) -> float:
    if not bullet_grams or not candidate.grams:
        return 0.0
    candidate_grams = set(candidate.grams)
    overlap = len(bullet_grams & candidate_grams)
    score = (2 * overlap) / (len(bullet_grams) + len(candidate_grams))
    if normalized_bullet in candidate.normalized_text or candidate.normalized_text in normalized_bullet:
        score = max(score, 0.80)
    token_overlap = bullet_tokens & set(candidate.tokens)
    token_bonus = min(0.35, sum(len(token) for token in token_overlap) / 30)
    return score + token_bonus


def _select_digest_link_anchor(bullet_text: str, candidate_text: str) -> str | None:
    for quoted_phrase in _quoted_phrases(candidate_text):
        if anchor := _find_original_substring_by_normalized(bullet_text, _normalize_for_digest_link_match(quoted_phrase)):
            return anchor

    normalized_candidate = _normalize_for_digest_link_match(candidate_text)
    if not normalized_candidate:
        return None
    normalized_bullet = _normalize_for_digest_link_match(bullet_text)
    if normalized_bullet and normalized_bullet in normalized_candidate and len(bullet_text) <= 40:
        return bullet_text
    best_anchor: str | None = None
    best_normalized_length = 0
    for segment in re.split(r"[、。:：,，()（）「」『』【】\s]+", bullet_text):
        if not segment:
            continue
        max_width = min(len(segment), 24)
        for start in range(len(segment)):
            for end in range(start + 3, min(len(segment), start + max_width) + 1):
                anchor = segment[start:end]
                normalized_anchor = _normalize_for_digest_link_match(anchor)
                if len(normalized_anchor) < 3:
                    continue
                if normalized_anchor in normalized_candidate and len(normalized_anchor) > best_normalized_length:
                    best_anchor = anchor
                    best_normalized_length = len(normalized_anchor)
    return best_anchor


def _quoted_phrases(text: str) -> list[str]:
    phrases: list[str] = []
    for match in re.finditer(r"[『「\"'“‘]([^』」\"'”’]{3,})[』」\"'”’]", text):
        phrase = match.group(1).strip()
        if phrase:
            phrases.append(phrase)
    return phrases


def _find_original_substring_by_normalized(text: str, normalized_needle: str) -> str | None:
    if len(normalized_needle) < 3:
        return None
    for start in range(len(text)):
        if not _normalize_for_digest_link_match(text[start]):
            continue
        for end in range(start + 1, len(text) + 1):
            if not _normalize_for_digest_link_match(text[end - 1]):
                continue
            if _normalize_for_digest_link_match(text[start:end]) == normalized_needle:
                return text[start:end]
    return None


def _plain_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unescape(HTML_TAG_RE.sub(" ", value)).split())


def _is_http_url(value: object) -> bool:
    return isinstance(value, str) and (value.startswith("https://") or value.startswith("http://"))


def _normalize_for_digest_link_match(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = re.sub(r"https?://\S+", " ", normalized)
    normalized = re.sub(r"[\s\-_./|()（）【】「」『』,，、。:：;；!！?？=+&・\[\]<>]+", "", normalized)
    return normalized


def _character_grams(text: str, *, size: int = 2) -> set[str]:
    if len(text) < size:
        return {text} if text else set()
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def _significant_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for token in SIGNIFICANT_TOKEN_RE.findall(text):
        normalized = unicodedata.normalize("NFKC", token).casefold()
        if normalized in AUTOLINK_STOPWORDS:
            continue
        tokens.add(normalized)
    return tokens


def _build_slack_blocks(markdown: str) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    bullet_items: list[dict[str, Any]] = []
    for line in markdown.splitlines():
        if not line.strip():
            if bullet_items:
                elements.append({"type": "rich_text_list", "style": "bullet", "elements": bullet_items})
                bullet_items = []
            continue
        if line.startswith("- "):
            bullet_items.append({"type": "rich_text_section", "elements": _parse_slack_rich_text_inline(line[2:])})
            continue
        if bullet_items:
            elements.append({"type": "rich_text_list", "style": "bullet", "elements": bullet_items})
            bullet_items = []
        elements.append({"type": "rich_text_section", "elements": _parse_slack_rich_text_inline(line)})
    if bullet_items:
        elements.append({"type": "rich_text_list", "style": "bullet", "elements": bullet_items})
    return [{"type": "rich_text", "elements": elements}] if elements else []


def _parse_slack_rich_text_inline(text: str) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    cursor = 0
    for match in re.finditer(r"<([^|>]+)\|([^>]+)>", text):
        if match.start() > cursor:
            elements.extend(_parse_bold_segments(text[cursor:match.start()]))
        elements.append({"type": "link", "url": match.group(1), "text": match.group(2)})
        cursor = match.end()
    if cursor < len(text):
        elements.extend(_parse_bold_segments(text[cursor:]))
    return elements or [{"type": "text", "text": ""}]


def _parse_bold_segments(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    elements: list[dict[str, Any]] = []
    cursor = 0
    for match in re.finditer(r"\*([^*]+)\*", text):
        if match.start() > cursor:
            elements.append({"type": "text", "text": text[cursor:match.start()]})
        elements.append({"type": "text", "text": match.group(1), "style": {"bold": True}})
        cursor = match.end()
    if cursor < len(text):
        elements.append({"type": "text", "text": text[cursor:]})
    return elements


GROK_FALLBACK_NOTICE = "⚠ Grok履歴はフォールバック（Chrome History）で取得。会話本文は未取得または不完全の可能性があります。"
SOURCE_ERRORS_RELATIVE_PATH = Path("metadata/source-errors.json")


def _prepend_grok_fallback_notice_if_needed(markdown: str, archive_directory: Path) -> str:
    raw_items_path = archive_directory / RAW_ITEMS_RELATIVE_PATH
    if not raw_items_path.exists():
        return markdown
    try:
        payload = json.loads(raw_items_path.read_text())
    except json.JSONDecodeError:
        return markdown
    if not isinstance(payload, list):
        return markdown
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("source") != "grok_history":
            continue
        provenance = item.get("provenance") or {}
        if isinstance(provenance, dict) and provenance.get("acquisition_mode") == "local_browser_history":
            return f"{GROK_FALLBACK_NOTICE}\n\n{markdown}"
    return markdown


def _write_source_errors_metadata(archive_directory: Path, source_errors: dict[str, str]) -> None:
    metadata_path = archive_directory / SOURCE_ERRORS_RELATIVE_PATH
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(source_errors, ensure_ascii=False, indent=2) + "\n")


def _prepend_source_error_notice_if_needed(markdown: str, archive_directory: Path) -> str:
    metadata_path = archive_directory / SOURCE_ERRORS_RELATIVE_PATH
    if not metadata_path.exists():
        return markdown
    try:
        payload = json.loads(metadata_path.read_text())
    except json.JSONDecodeError:
        return markdown
    if not isinstance(payload, dict) or not payload:
        return markdown
    lines = ["⚠ 一部ソース取得に失敗:"]
    for source_id, message in sorted(payload.items()):
        if not isinstance(source_id, str) or not isinstance(message, str):
            continue
        lines.append(f"- {source_id}: {_format_source_error_message(source_id, message)}")
    if len(lines) == 1:
        return markdown
    return "\n".join(lines) + "\n\n" + markdown


def _format_source_error_message(source_id: str, message: str) -> str:
    if source_id == "x_signals" and _is_x_spend_cap_error(message):
        reset_date = _extract_x_reset_date(message)
        return f"X API spend cap到達。{reset_date}までX由来をスキップ。" if reset_date else "X API spend cap到達。X由来のみスキップ。"
    return message


def _is_x_spend_cap_error(message: str) -> bool:
    normalized = message.lower()
    return "spend cap" in normalized or "creditsdepleted" in normalized or "spendcapreached" in normalized


def _extract_x_reset_date(message: str) -> str | None:
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", message)
    return match.group(1) if match else None


def _split_slack_digest_text(text: str, *, limit: int = DEFAULT_SLACK_MESSAGE_LIMIT) -> list[str]:
    return _split_slack_text(text, limit=limit)


def _split_slack_text(text: str, *, limit: int = DEFAULT_SLACK_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at, separator_length = _find_slack_split_point(remaining, limit)
        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at + separator_length :].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks or [text]


def _find_slack_split_point(text: str, limit: int) -> tuple[int, int]:
    preferred_splitters = (
        ("\n\n", 2),
        ("\n- ", 1),
        ("\n▫ ", 1),
        ("\n# ", 1),
        ("\n## ", 1),
    )
    for marker, separator_length in preferred_splitters:
        split_at = text.rfind(marker, 0, limit + 1)
        if split_at != -1 and split_at >= limit // 2:
            return split_at, separator_length

    split_at = text.rfind("\n", 0, limit + 1)
    if split_at != -1 and split_at >= limit // 2:
        return split_at, 1
    return limit, 0


def _post_slack_chunks(
    poster: SlackPoster,
    chunks: list[str],
    *,
    blocks_per_chunk: list[list[dict[str, Any]]],
    channel: str,
    thread_ts: str | None,
) -> list[Any]:
    responses: list[Any] = []
    for index, chunk in enumerate(chunks):
        response = poster(
            chunk,
            channel,
            thread_ts=thread_ts,
            unfurl_links=False,
            unfurl_media=False,
            blocks=blocks_per_chunk[index],
        )
        responses.append(response)
    return responses


if __name__ == "__main__":
    raise SystemExit(main())
