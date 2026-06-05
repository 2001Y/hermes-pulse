import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from hermes_pulse.categories import category_label, group_raw_items_by_category
from hermes_pulse.summarization.base import (
    CODEX_DIGEST_RELATIVE_PATH,
    RAW_ITEMS_RELATIVE_PATH,
    CodexInvocation,
    SummaryArtifact,
)
from hermes_pulse.title_resolution import fetch_title_from_url, synthesize_title_with_codex_spark

DEFAULT_CODEX_TIMEOUT_SECONDS = 900
DEFAULT_CODEX_MODEL = "gpt-5.4"
DEFAULT_SUMMARY_FORMAT = "briefing-v1"
MAX_PROMPT_RAW_ITEMS = 50
HEADLINE_STYLE_INSTRUCTION = (
    "各箇条書き項目は新聞・ニュースタイトル並みに短く、事実が伝わる最小文字数で書いてください"
    "（例: `LocallyAI、LM Studio公式iPhoneアプリ化。LM Link対応`）。"
    "説明調・背景説明は必要な場合だけにしてください。"
)
INLINE_SOURCE_LINK_INSTRUCTION = "リンクが必要な箇所は、source の URL を使って文中の重要語句を Markdown リンク `[ラベル](URL)` にしてください。"
NO_URL_LIST_INSTRUCTION = "URL を文末に列挙しないでください。裸の URL を単独で並べるのも避けてください。"


class CodexCliSummarizer:
    def __init__(
        self,
        invocation: CodexInvocation | None = None,
        *,
        model: str = DEFAULT_CODEX_MODEL,
        summary_format: str = DEFAULT_SUMMARY_FORMAT,
        digest_command: str = "morning-digest",
        title_fetcher=None,
        title_synthesizer=None,
    ) -> None:
        self._invocation = invocation or CodexCliInvocation(model=model)
        self._summary_format = summary_format
        self._digest_command = digest_command
        self._title_fetcher = title_fetcher or fetch_title_from_url
        self._title_synthesizer = title_synthesizer or synthesize_title_with_codex_spark

    def summarize_archive(self, archive_directory: str | Path) -> SummaryArtifact:
        archive_directory = Path(archive_directory)
        raw_items_path = archive_directory / RAW_ITEMS_RELATIVE_PATH
        raw_items = raw_items_path.read_text()
        items = json.loads(raw_items)
        category_groups = group_raw_items_by_category(items)
        with tempfile.TemporaryDirectory(prefix="hermes-pulse-codex-") as temp_dir:
            codex_context = Path(temp_dir)
            _stage_sanitized_codex_context(archive_directory, codex_context)
            partial_summaries: list[str] = []
            for category, category_items in category_groups.items():
                chunks = _chunk_items([dict(item) for item in category_items], MAX_PROMPT_RAW_ITEMS)
                for chunk_index, chunk in enumerate(chunks, start=1):
                    prompt = build_codex_digest_prompt(
                        archive_directory,
                        json.dumps(chunk, ensure_ascii=False),
                        summary_format=self._summary_format,
                        digest_command=self._digest_command,
                        title_fetcher=self._title_fetcher,
                        title_synthesizer=self._title_synthesizer,
                        chunk_index=chunk_index,
                        chunk_total=len(chunks),
                        category=category,
                    )
                    partial_summaries.append(self._invocation.run(prompt, cwd=codex_context))
            merge_prompt = build_codex_merge_prompt(
                partial_summaries,
                summary_format=self._summary_format,
                digest_command=self._digest_command,
            )
            content = self._invocation.run(merge_prompt, cwd=codex_context)

        output_path = archive_directory / CODEX_DIGEST_RELATIVE_PATH
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content)
        return SummaryArtifact(
            path=output_path,
            content=content,
            partial_contents=partial_summaries if len(partial_summaries) > 1 else None,
        )


class CodexCliInvocation:
    def __init__(
        self,
        executable: str = "codex",
        *,
        model: str = DEFAULT_CODEX_MODEL,
        timeout_seconds: int = DEFAULT_CODEX_TIMEOUT_SECONDS,
    ) -> None:
        self._executable = executable
        self._model = model
        self._timeout_seconds = timeout_seconds

    def run(self, prompt: str, *, cwd: Path) -> str:
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".md") as output_file:
            try:
                completed = subprocess.run(
                    [
                        self._executable,
                        "exec",
                        "--model",
                        self._model,
                        "--cd",
                        str(cwd),
                        "--skip-git-repo-check",
                        "--ephemeral",
                        "--output-last-message",
                        output_file.name,
                        "-",
                    ],
                    input=prompt,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=self._timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                timeout_seconds = int(error.timeout) if error.timeout else self._timeout_seconds
                raise RuntimeError(f"codex exec timed out after {timeout_seconds}s") from error
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "codex exec failed")
            return Path(output_file.name).read_text()


def build_codex_digest_prompt(
    archive_directory: Path,
    raw_items: str,
    *,
    summary_format: str = DEFAULT_SUMMARY_FORMAT,
    digest_command: str = "morning-digest",
    title_fetcher=None,
    title_synthesizer=None,
    chunk_index: int = 1,
    chunk_total: int = 1,
    category: str | None = None,
) -> str:
    category_heading = category_label(category) if category is not None else None
    prepared_raw_items = json.dumps(_prepare_items_for_prompt(json.loads(raw_items)), ensure_ascii=False)
    compact_raw_items, raw_item_counts = _compact_raw_items_for_prompt(
        prepared_raw_items,
        title_fetcher=title_fetcher,
        title_synthesizer=title_synthesizer,
    )
    lines = [
        "あなたは Hermes Pulse の要約担当です。",
        "以下の sanitized archive context から canonical digest を作成してください。",
        "出力は日本語の Markdown のみを返してください。前置きや説明は不要です。",
        "一次情報としてこの prompt に埋め込まれた sanitized grounding を最優先で根拠にしてください。",
        "本文中のリンクは可能な限り保持し、URL を壊さないでください。",
        "不明な点は断定せず、与えられた情報だけで簡潔に要約してください。",
        "内部的な source 名や流入元ラベルではなく、見えている内容そのものを同列に扱ってください。",
        "同じサービス・製品・AIモデルに関する話題は、会社・組織単位より優先してサービスごとにまとまりを意識して整理してください。",
        "同じ会社・組織に関する話題は、上のサービス単位の整理を優先したうえで必要に応じて補助的にまとめてください。",
        "自動車・EV関連の重要な製品動向、充電、電池、ソフトウェア更新も通常の主要トピック候補として扱ってください。",
        "エンタメ・芸能・作品紹介そのものは原則として主要トピックに含めないでください。",
        *(_category_prompt_instructions(category_heading) if category_heading is not None else []),
        f"この prompt は収集差分 chunk {chunk_index}/{chunk_total} です。chunk 内の重要事項を取りこぼさず要約してください。",
        "",
        *(
            build_category_summary_format_instructions(category_heading)
            if category_heading is not None
            else build_summary_format_instructions(summary_format, digest_command=digest_command)
        ),
        "",
        "## item counts",
        "```json",
        json.dumps(raw_item_counts, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Primary grounding: normalized content snapshot",
        "```json",
        compact_raw_items.rstrip(),
        "```",
        "",
    ]
    return "\n".join(lines)


def build_codex_merge_prompt(
    chunk_summaries: list[str],
    *,
    summary_format: str = DEFAULT_SUMMARY_FORMAT,
    digest_command: str = "morning-digest",
) -> str:
    lines = [
        "あなたは Hermes Pulse の最終編集担当です。",
        "以下は複数 chunk から作った部分要約です。重要事項を重複なく統合し、最終版だけを返してください。",
        "重要な事実は維持し、表現はニュース見出し並みに短くしてください。",
        "明らかに関連する項目だけを軽く統合し、項目数を不必要に減らさないでください。",
        "情報量を落としすぎず、同一テーマの重複 bullet は最小限だけ統合してください。",
        "最終版は必要なカテゴリだけを `AI / IT / 金融 / カメラ / 車 / スケジュール` の順で大カテゴリ見出しに分けてください。",
        "カテゴリ見出しは `▫ AI` / `▫ IT` / `▫ 金融` / `▫ カメラ` / `▫ 車` / `▫ スケジュール` を使い、空カテゴリは省略してください。",
        "同じサービス・製品・AIモデルに関する話題は、会社・組織単位より優先してサービスごとにまとまりを意識して整理してください。",
        "同じ会社・組織に関する話題は、上のサービス単位の整理を優先したうえで必要に応じて補助的にまとめてください。",
        "自動車・EV関連の重要な製品動向、充電、電池、ソフトウェア更新も通常の主要トピック候補として扱ってください。",
        "エンタメ・芸能・作品紹介そのものは原則として主要トピックに含めないでください。",
        "",
        *build_categorized_summary_format_instructions(summary_format, digest_command=digest_command),
        "",
    ]
    for index, summary in enumerate(chunk_summaries, start=1):
        lines.extend(
            [
                f"## Partial summary {index}",
                summary.rstrip(),
                "",
            ]
        )
    return "\n".join(lines)


def _stage_sanitized_codex_context(archive_directory: Path, codex_context: Path) -> None:
    codex_context.mkdir(parents=True, exist_ok=True)


def _category_prompt_instructions(category_heading: str) -> list[str]:
    return [
        f"この prompt は大カテゴリ `{category_heading}` 専用です。他カテゴリの見出しや全体タイトルは出さないでください。",
        "カテゴリ境界をまたいだ取捨選択を避け、このカテゴリ内の記事だけで重要度順に整理してください。",
    ]


def build_category_summary_format_instructions(category_heading: str) -> list[str]:
    return [
        "出力フォーマットはカテゴリ部分要約を厳守してください。",
        f"先頭行は必ず `▫ {category_heading}` にしてください。全体タイトルは書かないでください。",
        "その下に必要な件数だけ箇条書きしてください。各項目は 1 行で要点→必要なら文中リンク。",
        "各箇条書き項目は 1 つの完結した短い行として書き、1 項目を複数行に分けないでください。",
        HEADLINE_STYLE_INSTRUCTION,
        INLINE_SOURCE_LINK_INSTRUCTION,
        NO_URL_LIST_INSTRUCTION,
        "当日または近い日時の予定・期限だけは `▫ スケジュール` カテゴリで扱い、他カテゴリでは本文の補足に留めてください。",
    ]


def build_categorized_summary_format_instructions(summary_format: str, *, digest_command: str = "morning-digest") -> list[str]:
    if summary_format == "briefing-v1":
        title, schedule_heading = _briefing_v1_headings_for_command(digest_command)
        return [
            "出力フォーマットは category-briefing-v1 を厳守してください。",
            f"全体タイトルは `{title}` を先頭に 1 回だけ書いてください。",
            "その下は必要なカテゴリだけを `▫ AI` / `▫ IT` / `▫ 金融` / `▫ カメラ` / `▫ 車` / `▫ スケジュール` の順で出してください。空カテゴリは省略してください。",
            f"`▫ スケジュール` は `{schedule_heading}` 相当の当日または近い日時の予定・期限だけを書く。無ければカテゴリごと省略してよい。",
            "各カテゴリの下は必要な件数だけ箇条書きにし、重要事項の取りこぼしを避けてください。",
            "各箇条書き項目は 1 つの完結した短い行として書き、1 項目を複数行に分けないでください。",
            HEADLINE_STYLE_INSTRUCTION,
            INLINE_SOURCE_LINK_INSTRUCTION,
            NO_URL_LIST_INSTRUCTION,
        ]
    raise ValueError(f"Unsupported summary format: {summary_format}")


def build_summary_format_instructions(summary_format: str, *, digest_command: str = "morning-digest") -> list[str]:
    if summary_format == "briefing-v1":
        title, schedule_heading = _briefing_v1_headings_for_command(digest_command)
        return [
            "出力フォーマットは briefing-v1 を厳守してください。",
            f"見出しはこの順番で固定してください: `{title}` / `▫ 主要トピック` / `{schedule_heading}`。",
            INLINE_SOURCE_LINK_INSTRUCTION,
            NO_URL_LIST_INSTRUCTION,
            "`▫ 主要トピック` は必要な件数だけ箇条書きにしてよい。重要事項の取りこぼしを避け、各項目は 1 行で要点→必要なら文中リンク。",
            "各箇条書き項目は 1 つの完結した短い行として書き、1 項目を複数行に分けないでください。",
            HEADLINE_STYLE_INSTRUCTION,
            "`▫ 主要トピック` は internal source 名に引きずられず、与えられた URL/title/本文断片を同列に見て重要度順に選んでください。",
            f"`{schedule_heading}` は当日または近い日時の予定だけを書く。無ければ `- 目立った予定なし`。",
        ]
    raise ValueError(f"Unsupported summary format: {summary_format}")


def _briefing_v1_headings_for_command(digest_command: str) -> tuple[str, str]:
    if digest_command == "evening-digest":
        return ("☾ *Hermes Pulse Evening Briefing*", "▫ 明日の予定・期限")
    return ("☀ *Hermes Pulse Morning Briefing*", "▫ 今日の予定・期限")


def _chunk_items(items: list[dict[str, object]], chunk_size: int) -> list[list[dict[str, object]]]:
    if not items:
        return [[]]
    chunk_count = max(1, (len(items) + chunk_size - 1) // chunk_size)
    token_weights = [_estimate_item_tokens(item) for item in items]
    total_tokens = sum(token_weights)
    target_tokens_per_chunk = total_tokens / chunk_count
    chunks: list[list[dict[str, object]]] = []
    current_chunk: list[dict[str, object]] = []
    current_tokens = 0
    for index, item in enumerate(items):
        current_chunk.append(item)
        current_tokens += token_weights[index]
        remaining_items = len(items) - index - 1
        remaining_chunks = chunk_count - len(chunks) - 1
        should_split = (
            remaining_chunks > 0
            and current_tokens >= target_tokens_per_chunk
            and remaining_items >= remaining_chunks
        )
        if should_split:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0
    if current_chunk:
        chunks.append(current_chunk)
    return chunks


def _estimate_item_tokens(item: dict[str, object]) -> int:
    text_parts: list[str] = []
    for field_name in ("title", "excerpt", "body", "url"):
        value = item.get(field_name)
        if isinstance(value, str):
            text_parts.append(value)
    estimated = max(1, len(" ".join(text_parts)) // 24)
    return estimated


def _compact_raw_items_for_prompt(raw_items: str, *, title_fetcher=None, title_synthesizer=None) -> tuple[str, dict[str, int]]:
    items = json.loads(raw_items)
    compact_items: list[dict[str, object]] = []
    fetcher = title_fetcher or fetch_title_from_url
    synthesizer = title_synthesizer or synthesize_title_with_codex_spark
    for item in items[:MAX_PROMPT_RAW_ITEMS]:
        timestamps = item.get("timestamps") or {}
        compact_items.append(
            {
                "title": _resolve_item_title(item, fetcher=fetcher, synthesizer=synthesizer),
                "excerpt": _truncate_text(item.get("excerpt"), max_length=280),
                "body": _truncate_text(item.get("body"), max_length=280),
                "url": item.get("url"),
                "timestamps": {
                    "created_at": timestamps.get("created_at"),
                    "updated_at": timestamps.get("updated_at"),
                    "start_at": timestamps.get("start_at"),
                    "end_at": timestamps.get("end_at"),
                },
            }
        )
    raw_item_counts = {
        "total_items": len(items),
        "included_in_prompt": len(compact_items),
        "omitted_from_prompt": max(len(items) - len(compact_items), 0),
    }
    return json.dumps(compact_items, ensure_ascii=False, indent=2) + "\n", raw_item_counts


def _prepare_items_for_prompt(items: list[dict[str, object]]) -> list[dict[str, object]]:
    deduped_items = _dedupe_items_by_url(items)
    return _order_items_for_prompt(deduped_items)


def _dedupe_items_by_url(items: list[dict[str, object]]) -> list[dict[str, object]]:
    deduped_by_url: dict[str, dict[str, object]] = {}
    passthrough: list[dict[str, object]] = []
    for item in items:
        url = item.get("url")
        if not isinstance(url, str) or not url:
            passthrough.append(item)
            continue
        existing = deduped_by_url.get(url)
        if existing is None or _item_text_weight(item) > _item_text_weight(existing):
            deduped_by_url[url] = item
    return passthrough + list(deduped_by_url.values())


def _order_items_for_prompt(items: list[dict[str, object]]) -> list[dict[str, object]]:
    if len(items) <= 1:
        return items
    indexed_items = list(enumerate(items))
    clusters: list[dict[str, object]] = []
    for index, item in indexed_items:
        signature = _item_signature(item)
        placed = False
        for cluster in clusters:
            cluster_signature = cluster["signature"]
            if isinstance(cluster_signature, set) and len(signature & cluster_signature) >= 1:
                cluster_items = cluster["items"]
                if isinstance(cluster_items, list):
                    cluster_items.append((index, item))
                cluster_signature.update(signature)
                placed = True
                break
        if not placed:
            clusters.append({"signature": set(signature), "items": [(index, item)]})
    ordered: list[dict[str, object]] = []
    for cluster in clusters:
        cluster_items = cluster["items"]
        if isinstance(cluster_items, list):
            cluster_items.sort(key=lambda value: value[0])
            ordered.extend(item for _, item in cluster_items)
    return ordered


def _item_signature(item: dict[str, object]) -> set[str]:
    tokens: set[str] = set()
    stopwords = {"item", "items", "update", "updates", "note", "notes", "news", "launch", "launches", "ships", "ship", "first", "second", "third"}
    url = item.get("url")
    if isinstance(url, str) and url:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host and host != "example.com":
            tokens.add(f"host:{host}")
        for token in re.findall(r"[A-Za-z0-9]{3,}", parsed.path.lower()):
            if not token.isdigit() and token not in stopwords:
                tokens.add(f"path:{token}")
    for field_name in ("title", "excerpt", "body"):
        value = item.get(field_name)
        if not isinstance(value, str):
            continue
        for token in re.findall(r"[A-Za-z0-9]{3,}", value.lower()):
            if not token.isdigit() and token not in stopwords:
                tokens.add(token)
    return tokens


def _item_text_weight(item: dict[str, object]) -> int:
    score = 0
    for field_name in ("title", "excerpt", "body"):
        value = item.get(field_name)
        if isinstance(value, str):
            score += len(value)
    return score


def _resolve_item_title(item: dict[str, object], *, fetcher, synthesizer) -> str | None:
    existing_title = _truncate_text(item.get("title"))
    if existing_title is not None:
        return existing_title
    url = item.get("url")
    if isinstance(url, str) and url:
        fetched_title = _truncate_text(fetcher(url))
        if fetched_title is not None:
            return fetched_title
        body_text = _truncate_text(item.get("body"), max_length=280) or _truncate_text(item.get("excerpt"), max_length=280)
        if body_text:
            synthesized_title = _truncate_text(synthesizer(body_text, url))
            if synthesized_title is not None:
                return synthesized_title
        return _fallback_title_for_url_item(url)
    return _truncate_text(item.get("excerpt")) or _truncate_text(item.get("body")) or "Untitled item"


def _truncate_text(value: object, *, max_length: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 1]}…"


def _fallback_title_for_url_item(url: str) -> str:
    return url.split("//", 1)[-1]
