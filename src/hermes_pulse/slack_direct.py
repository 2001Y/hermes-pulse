from __future__ import annotations

import argparse
import importlib.util
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

DEFAULT_SLACK_DIRECT_PATH = Path.home() / ".hermes" / "scripts" / "slack_direct.py"


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-pulse-slack-direct")
    parser.add_argument("--input-file", type=Path, required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--thread-ts")
    return parser


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


def post_input_file_to_slack(
    input_file: str | Path,
    *,
    channel: str,
    thread_ts: str | None = None,
    post_message: SlackPoster | None = None,
) -> Any:
    input_path = Path(input_file)
    text = input_path.read_text()
    poster = post_message or load_slack_direct_post_message()
    return poster(text, channel, thread_ts=thread_ts, unfurl_links=False, unfurl_media=False, blocks=None)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    post_input_file_to_slack(args.input_file, channel=args.channel, thread_ts=args.thread_ts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
