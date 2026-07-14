import argparse
import json
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, TextIO

ACTIVITY_STREAM_URL = "https://api.x.com/2/activity/stream"


def append_activity_events(
    chunks: Iterable[str],
    *,
    output_path: str | Path,
    expected_user_id: str,
) -> int:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    seen_event_ids = _load_seen_event_ids(output_path)
    decoder = json.JSONDecoder()
    buffer = ""
    appended = 0
    with output_path.open("a", encoding="utf-8") as output:
        for chunk in chunks:
            buffer += chunk
            while buffer.strip():
                leading = len(buffer) - len(buffer.lstrip())
                try:
                    payload, end = decoder.raw_decode(buffer, leading)
                except json.JSONDecodeError:
                    break
                buffer = buffer[end:]
                event_id = _expected_outbound_like_event_id(payload, expected_user_id=expected_user_id)
                if event_id is None or event_id in seen_event_ids:
                    continue
                output.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                output.flush()
                seen_event_ids.add(event_id)
                appended += 1
    return appended


def run_activity_stream(
    *,
    output_path: str | Path,
    expected_user_id: str,
    process_factory=subprocess.Popen,
) -> int:
    process = process_factory(
        ["xurl", "--auth", "app", "--stream", ACTIVITY_STREAM_URL],
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stdout = process.stdout
    if stdout is None:
        process.terminate()
        raise RuntimeError("xurl activity stream did not expose stdout")
    append_activity_events(
        _iter_stream_chunks(stdout),
        output_path=output_path,
        expected_user_id=expected_user_id,
    )
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"xurl activity stream exited with status {return_code}")
    return return_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-pulse-x-activity-stream")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-user-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if not args.expected_user_id.isdigit():
        raise ValueError("expected X user id must be numeric")
    return run_activity_stream(
        output_path=args.output,
        expected_user_id=args.expected_user_id,
    )


def _iter_stream_chunks(stream: TextIO) -> Iterable[str]:
    while True:
        chunk = stream.readline()
        if chunk == "":
            return
        yield chunk


def _load_seen_event_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    seen: set[str] = set()
    for line in path.read_text().splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        event_id = data.get("event_uuid") if isinstance(data, dict) else None
        if isinstance(event_id, str):
            seen.add(event_id)
    return seen


def _expected_outbound_like_event_id(payload: Any, *, expected_user_id: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("event_type") != "like.create":
        return None
    event_filter = data.get("filter")
    if not isinstance(event_filter, dict):
        return None
    if event_filter.get("user_id") != expected_user_id or event_filter.get("direction") != "outbound":
        return None
    event_id = data.get("event_uuid")
    return event_id if isinstance(event_id, str) and event_id else None


if __name__ == "__main__":
    raise SystemExit(main())
