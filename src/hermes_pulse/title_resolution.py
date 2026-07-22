from __future__ import annotations

import subprocess
from html.parser import HTMLParser

from hermes_pulse.model_policy import PULSE_CODEX_MODEL, require_pulse_codex_model

DEFAULT_TITLE_SYNTH_MODEL = PULSE_CODEX_MODEL
DEFAULT_TITLE_FETCH_TIMEOUT_SECONDS = 5
DEFAULT_TITLE_SYNTH_TIMEOUT_SECONDS = 30


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._in_title:
            self.parts.append(data)


def fetch_title_from_url(
    url: str,
    *,
    timeout_seconds: int = DEFAULT_TITLE_FETCH_TIMEOUT_SECONDS,
    curl_executable: str = "curl",
) -> str | None:
    try:
        completed = subprocess.run(
            [
                curl_executable,
                "-LfsS",
                "--max-time",
                str(timeout_seconds),
                url,
            ],
            capture_output=True,
            check=False,
            text=False,
        )
    except OSError:
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    html = completed.stdout.decode("utf-8", errors="replace")
    parser = _TitleParser()
    parser.feed(html)
    parser.close()
    title = " ".join(part.strip() for part in parser.parts if part.strip())
    normalized = " ".join(title.split())
    return normalized or None


def synthesize_title_with_codex_spark(
    text: str,
    url: str,
    *,
    executable: str = "codex",
    model: str = DEFAULT_TITLE_SYNTH_MODEL,
    timeout_seconds: int = DEFAULT_TITLE_SYNTH_TIMEOUT_SECONDS,
) -> str | None:
    require_pulse_codex_model(model)
    prompt = "\n".join(
        [
            "次の URL と本文断片から、情報量を落としすぎない 1 行タイトルだけを返してください。",
            "条件:",
            "- 80文字以内",
            "- 前置き・説明・引用符なし",
            "- 固有名詞と主要トピックを残す",
            "- URL自体をそのまま返さない",
            f"URL: {url}",
            f"本文断片: {text}",
        ]
    )
    try:
        completed = subprocess.run(
            [
                executable,
                "exec",
                "--model",
                model,
                "--skip-git-repo-check",
                "--ephemeral",
                "-",
            ],
            input=prompt,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    output = (completed.stdout or "").strip()
    if not output:
        return None
    return " ".join(output.splitlines()[-1].split()) or None
