import argparse
import importlib.util
import inspect
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from hermes_pulse.archive import write_morning_digest_archive
from hermes_pulse.cli import _archive_label_for_args, _apply_replay_window_if_requested, _build_digest_with_source_errors, _occurred_at_for_command
from hermes_pulse.summarization import CodexCliSummarizer
from hermes_pulse.summarization.base import CODEX_DIGEST_RELATIVE_PATH, SummaryArtifact
from hermes_pulse.summarization.codex_cli import (
    DEFAULT_CODEX_MODEL,
    DEFAULT_SUMMARY_FORMAT,
    build_summary_format_instructions,
)


DEFAULT_SLACK_DIRECT_PATH = Path.home() / ".hermes" / "scripts" / "slack_direct.py"
DEFAULT_SLACK_MESSAGE_LIMIT = 3500
DEFAULT_RETRY_DELAYS_SECONDS = (300, 300)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


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
    rendered_message = _prepend_grok_fallback_notice_if_needed(
        _prepend_source_error_notice_if_needed(_render_digest_for_slack(content), archive_directory),
        archive_directory,
    )
    message_chunks = _split_slack_text(rendered_message, limit=slack_message_limit)
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
    raw_items_path = archive_directory / "raw" / "collected-items.json"
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
        lines.append(f"- {source_id}: {message}")
    if len(lines) == 1:
        return markdown
    return "\n".join(lines) + "\n\n" + markdown


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
    active_thread_ts = thread_ts
    for index, chunk in enumerate(chunks):
        response = poster(
            chunk,
            channel,
            thread_ts=active_thread_ts,
            unfurl_links=False,
            unfurl_media=False,
            blocks=blocks_per_chunk[index],
        )
        responses.append(response)
        if index == 0 and active_thread_ts is None:
            response_ts = response.get("ts") if isinstance(response, dict) else None
            if isinstance(response_ts, str) and response_ts:
                active_thread_ts = response_ts
    return responses


if __name__ == "__main__":
    raise SystemExit(main())
