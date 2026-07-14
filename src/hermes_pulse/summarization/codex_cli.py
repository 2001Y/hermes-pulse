import hashlib
import json
import logging
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import unquote_plus, urlparse

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
INLINE_SOURCE_LINK_INSTRUCTION = (
    "リンク可能なニュース箇条書きは必ず 1 つ以上、source の URL を使って"
    "文中の重要語句を Markdown リンク `[ラベル](URL)` にしてください"
    "（source URL がない予定・ローカル記録だけリンク不要）。"
)
PRESERVE_INLINE_SOURCE_LINKS_INSTRUCTION = "統合・短縮時も既存の Markdown リンクを消さず、リンク先 URL を別 URL に置き換えないでください。"
NO_URL_LIST_INSTRUCTION = "URL を文末に列挙しないでください。裸の URL を単独で並べるのも避けてください。"
MARKDOWN_INLINE_LINK_START_RE = re.compile(r"\[([^\]\n]+)\]\(")
SKILLIZATION_HEADING = "▫ スキル化候補"

logger = logging.getLogger(__name__)


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
                    source_context = _source_link_context_from_items(chunk)
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
                    partial_summaries.append(
                        _run_prompt_requiring_inline_source_links(
                            self._invocation,
                            prompt,
                            cwd=codex_context,
                            source_context=source_context,
                            previous_output_label="カテゴリ要約",
                        )
                    )
            merge_prompt = build_codex_merge_prompt(
                partial_summaries,
                summary_format=self._summary_format,
                digest_command=self._digest_command,
            )
            content = _run_prompt_requiring_inline_source_links(
                self._invocation,
                merge_prompt,
                cwd=codex_context,
                source_context=_source_link_context_from_markdown("\n".join(partial_summaries)),
                previous_output_label="最終要約",
            )
            skillization_candidates: list[list[dict[str, str]]] = []
            try:
                skillization_items = _prepare_items_for_prompt([dict(item) for item in items])
                skillization_chunks = _chunk_items_by_count(skillization_items, MAX_PROMPT_RAW_ITEMS)
            except Exception as error:
                logger.warning("Skipping skillization overlay after preparation failure: %s", error)
                skillization_chunks = []
            for chunk_index, chunk in enumerate(skillization_chunks, start=1):
                try:
                    source_context = _skillization_source_context_from_items(chunk)
                    prompt = build_skillization_candidates_prompt(
                        json.dumps(chunk, ensure_ascii=False),
                        chunk_index=chunk_index,
                        chunk_total=len(skillization_chunks),
                        title_fetcher=self._title_fetcher,
                        title_synthesizer=self._title_synthesizer,
                        source_context=source_context,
                    )
                    normalized = _run_skillization_prompt(
                        self._invocation,
                        prompt,
                        cwd=codex_context,
                        source_context=source_context,
                    )
                except Exception as error:
                    logger.warning(
                        "Skipping skillization candidate chunk %s/%s after optional overlay failure: %s",
                        chunk_index,
                        len(skillization_chunks),
                        error,
                    )
                    continue
                if normalized:
                    skillization_candidates.append(normalized)
            try:
                skillization_overlay = _combine_skillization_candidate_summaries(skillization_candidates)
                content = _append_skillization_overlay(content, skillization_overlay)
            except Exception as error:
                logger.warning("Skipping skillization overlay after finalization failure: %s", error)

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


def _run_prompt_requiring_inline_source_links(
    invocation: CodexInvocation,
    prompt: str,
    *,
    cwd: Path,
    source_context: list[dict[str, str]],
    previous_output_label: str,
) -> str:
    output = invocation.run(prompt, cwd=cwd)
    if _has_required_inline_source_link(output, source_context):
        return output
    repair_prompt = build_codex_inline_link_repair_prompt(
        previous_output=output,
        source_context=source_context,
        previous_output_label=previous_output_label,
    )
    repaired_output = invocation.run(repair_prompt, cwd=cwd)
    if _has_required_inline_source_link(repaired_output, source_context):
        return repaired_output
    raise RuntimeError(f"Codex {previous_output_label} lacks required inline source Markdown links after repair")


def build_codex_inline_link_repair_prompt(
    *,
    previous_output: str,
    source_context: list[dict[str, str]],
    previous_output_label: str,
) -> str:
    lines = [
        "あなたは Hermes Pulse の Markdown 要約修正担当です。",
        f"前回の{previous_output_label}には、source URL を使った Markdown インラインリンクが不足しています。",
        "新しい事実は追加せず、前回出力の文言とカテゴリ構造を極力維持してください。",
        INLINE_SOURCE_LINK_INSTRUCTION,
        PRESERVE_INLINE_SOURCE_LINKS_INSTRUCTION,
        NO_URL_LIST_INSTRUCTION,
        "リンク可能な各ニュース箇条書きに、下記 source context の URL を使った `[重要語句](URL)` を最低 1 つ入れてください。",
        "source context に対応 URL が見当たらない予定・ローカル記録だけはリンクなしで構いません。",
        "出力は修正版 Markdown のみ。前置きや説明は不要です。",
        "",
        "## source context",
        "```json",
        json.dumps(source_context, ensure_ascii=False, indent=2),
        "```",
        "",
        f"## 前回の{previous_output_label}",
        previous_output.rstrip(),
        "",
    ]
    return "\n".join(lines)


def _has_required_inline_source_link(markdown: str, source_context: list[dict[str, str]]) -> bool:
    expected_urls = {entry.get("url", "") for entry in source_context if entry.get("url")}
    if not expected_urls:
        return True
    actual_urls = set(_extract_markdown_link_urls(markdown))
    return bool(expected_urls & actual_urls)


def _extract_markdown_link_urls(markdown: str) -> list[str]:
    return [url for _label, url, _start, _end in _find_markdown_inline_links(markdown)]


def _find_markdown_inline_links(markdown: str) -> list[tuple[str, str, int, int]]:
    links: list[tuple[str, str, int, int]] = []
    consumed_until = 0
    for match in MARKDOWN_INLINE_LINK_START_RE.finditer(markdown):
        if match.start() < consumed_until:
            continue
        url_start = match.end()
        parenthesis_depth = 0
        for index in range(url_start, len(markdown)):
            character = markdown[index]
            if character.isspace():
                break
            if character == "(":
                parenthesis_depth += 1
                continue
            if character != ")":
                continue
            if parenthesis_depth > 0:
                parenthesis_depth -= 1
                continue
            url = markdown[url_start:index]
            if _is_http_url(url):
                links.append((match.group(1), url, match.start(), index + 1))
                consumed_until = index + 1
            break
    return links


def _run_skillization_prompt(
    invocation: CodexInvocation,
    prompt: str,
    *,
    cwd: Path,
    source_context: list[dict[str, str]],
) -> list[dict[str, str]]:
    output = invocation.run(prompt, cwd=cwd)
    try:
        return _normalize_skillization_candidate_output(output, source_context=source_context)
    except RuntimeError:
        repair_prompt = build_skillization_candidates_repair_prompt(
            previous_output=output,
            source_context=source_context,
        )
        repaired_output = invocation.run(repair_prompt, cwd=cwd)
        return _normalize_skillization_candidate_output(repaired_output, source_context=source_context)


def build_skillization_candidates_repair_prompt(
    *,
    previous_output: str,
    source_context: list[dict[str, str]],
) -> str:
    lines = [
        "あなたは Hermes Pulse のスキル化候補修正担当です。",
        "前回のスキル化候補は出力契約を満たしません。新しい事実、URL、source_idを追加せずに修正してください。",
        "出力はJSON配列だけにし、各要素を source_id / capability / destination / value の4フィールドにしてください。",
        "source_idはsource contextから選び、capabilityにはURL・Markdown・Slackリンクを含めないでください。",
        "destinationは `既存Skill更新` / `reference追加` / `script・template追加` / `新規class-level Skill` のいずれかにしてください。",
        "valueは `高` / `中` / `高・要検証` / `中・要検証` のいずれかにしてください。",
        "候補がない場合は空のJSON配列 `[]` を返してください。前置き、コードフェンス、説明は不要です。",
        "",
        "## source context",
        "```json",
        json.dumps(source_context, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 前回のスキル化候補",
        previous_output.rstrip(),
        "",
    ]
    return "\n".join(lines)


def _normalize_skillization_candidate_output(
    output: str,
    *,
    source_context: list[dict[str, str]],
) -> list[dict[str, str]]:
    try:
        payload = json.loads(output.strip())
    except json.JSONDecodeError as error:
        raise RuntimeError("Codex skillization candidate output must be a JSON array") from error
    if not isinstance(payload, list):
        raise RuntimeError("Codex skillization candidate output must be a JSON array")

    source_by_id = {entry["source_id"]: entry for entry in source_context if entry.get("source_id")}
    if len(payload) > MAX_PROMPT_RAW_ITEMS:
        raise RuntimeError("Codex skillization candidate output exceeds the per-chunk candidate limit")

    required_fields = {"source_id", "capability", "destination", "value"}
    allowed_destinations = {"既存Skill更新", "reference追加", "script・template追加", "新規class-level Skill"}
    allowed_values = {"高", "中", "高・要検証", "中・要検証"}
    seen_source_ids: set[str] = set()
    normalized: list[dict[str, str]] = []
    for candidate in payload:
        if not isinstance(candidate, dict) or set(candidate) != required_fields:
            raise RuntimeError("Codex skillization candidate must contain exactly the required fields")
        if not all(isinstance(candidate[field], str) for field in required_fields):
            raise RuntimeError("Codex skillization candidate fields must be strings")

        source_id = candidate["source_id"]
        source = source_by_id.get(source_id)
        if source is None:
            raise RuntimeError("Codex skillization candidate references an unknown source_id")
        capability = candidate["capability"]
        destination = candidate["destination"]
        value = candidate["value"]
        if not _is_safe_skillization_plain_text(capability):
            raise RuntimeError("Codex skillization capability must be plain text without links")
        if destination not in allowed_destinations or value not in allowed_values:
            raise RuntimeError("Codex skillization candidate has an invalid destination or value")
        if source_id in seen_source_ids:
            continue
        seen_source_ids.add(source_id)
        normalized.append(
            {
                "source_id": source_id,
                "url": source["url"],
                "title": source.get("title", ""),
                "content_fingerprint": source.get("content_fingerprint", ""),
                "capability": capability,
                "destination": destination,
                "value": value,
            }
        )
    return normalized


def _is_safe_skillization_plain_text(value: str) -> bool:
    if not value or value != value.strip() or len(value) > 280 or len(value.splitlines()) != 1:
        return False
    normalized_value = unicodedata.normalize("NFKC", value)
    if normalized_value != normalized_value.strip() or len(normalized_value) > 280 or len(normalized_value.splitlines()) != 1:
        return False
    if any(character in normalized_value for character in "<>[]*_~`"):
        return False
    if ";" in normalized_value or re.search(r"(?:能力|反映先|価値)\s*[:：]", normalized_value):
        return False
    if any(unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in value):
        return False
    if re.search(r"(?i)(?:https?://|www\.|\b[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:[/?:#][^\s]*)?)", normalized_value):
        return False
    return True


_TRACKING_QUERY_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}
_UNRESERVED_URL_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT_ESCAPE_RE = re.compile(r"%([0-9A-Fa-f]{2})")


def _normalize_percent_encoded_unreserved(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        character = chr(int(match.group(1), 16))
        if character in _UNRESERVED_URL_CHARACTERS:
            return character
        return f"%{match.group(1).upper()}"

    return _PERCENT_ESCAPE_RE.sub(replace, value)


def _normalize_query_for_dedupe(query: str) -> str:
    kept_components: list[str] = []
    for component in query.split("&"):
        raw_key = component.split("=", 1)[0]
        normalized_key = unquote_plus(raw_key).casefold()
        if normalized_key.startswith("utm_") or normalized_key in _TRACKING_QUERY_PARAMETERS:
            continue
        kept_components.append(_normalize_percent_encoded_unreserved(component))
    return "&".join(kept_components)


def _canonicalize_url_for_dedupe(url: str) -> str:
    parsed = urlparse(url)
    hostname = parsed.hostname
    if hostname is None:
        return url
    hostname = hostname.rstrip(".")
    if ":" in hostname:
        normalized_hostname = hostname.lower()
        host = f"[{normalized_hostname}]"
    else:
        try:
            normalized_hostname = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            normalized_hostname = hostname.lower()
        host = normalized_hostname
    if "@" in parsed.netloc:
        host = f"{parsed.netloc.rsplit('@', 1)[0]}@{host}"
    try:
        port = parsed.port
    except ValueError:
        return url
    if port is not None and not (
        (parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=host,
        path=_normalize_percent_encoded_unreserved(path),
        params=_normalize_percent_encoded_unreserved(parsed.params),
        query=_normalize_query_for_dedupe(parsed.query),
        fragment="",
    ).geturl()


def _normalize_source_fingerprint_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    return value


def _skillization_content_fingerprint(source: dict[str, object]) -> str:
    body = _normalize_source_fingerprint_text(source.get("body"))
    excerpt = _normalize_source_fingerprint_text(source.get("excerpt"))
    if body:
        payload = {"field": "body", "text": body}
    elif excerpt:
        payload = {"field": "excerpt", "text": excerpt}
    else:
        return ""
    canonical_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def _combine_skillization_candidate_summaries(summaries: list[list[dict[str, str]]]) -> str | None:
    seen_urls: set[str] = set()
    seen_content_fingerprints: set[str] = set()
    bullets: list[str] = []
    for summary in summaries:
        for candidate in summary:
            url = candidate["url"]
            canonical_url = _canonicalize_url_for_dedupe(url)
            content_fingerprint = candidate.get("content_fingerprint", "")
            if canonical_url in seen_urls or (
                content_fingerprint and content_fingerprint in seen_content_fingerprints
            ):
                continue
            seen_urls.add(canonical_url)
            if content_fingerprint:
                seen_content_fingerprints.add(content_fingerprint)
            label = _sanitize_markdown_link_label(candidate.get("title") or url)
            bullets.append(
                f"- [{label}]({url}) — 能力: {candidate['capability']}; "
                f"反映先: {candidate['destination']}; 価値: {candidate['value']}"
            )
    if not bullets:
        return None
    return "\n".join([SKILLIZATION_HEADING, *bullets]) + "\n"


def _sanitize_markdown_link_label(value: str) -> str:
    translations = str.maketrans(
        {
            "[": "［",
            "]": "］",
            "<": "‹",
            ">": "›",
            "|": "｜",
            "*": "＊",
            "_": "＿",
            "~": "～",
            "`": "｀",
            "&": "＆",
            "\\": "＼",
        }
    )
    collapsed = " ".join(value.split())
    sanitized = "".join(
        character
        for character in collapsed.translate(translations)
        if unicodedata.category(character) not in {"Cc", "Cf", "Zl", "Zp"}
    )
    return sanitized or "出典"


def _append_skillization_overlay(content: str, overlay: str | None) -> str:
    if overlay is None:
        return content
    if content.endswith("\n\n"):
        separator = ""
    elif content.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return f"{content}{separator}{overlay}"


def _source_link_context_from_items(items: list[dict[str, object]]) -> list[dict[str, str]]:
    context: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in items:
        url = item.get("url")
        if not isinstance(url, str) or not _is_http_url(url) or url in seen_urls:
            continue
        seen_urls.add(url)
        entry = {"url": url}
        title = _truncate_text(item.get("title"))
        excerpt = _truncate_text(item.get("excerpt"), max_length=180)
        if title:
            entry["title"] = title
        if excerpt:
            entry["excerpt"] = excerpt
        context.append(entry)
    return context


def _skillization_source_context_from_items(items: list[dict[str, object]]) -> list[dict[str, str]]:
    source_by_url: dict[str, dict[str, object]] = {}
    for item in items:
        url = item.get("url")
        if isinstance(url, str) and _is_http_url(url) and url not in source_by_url:
            source_by_url[url] = item
    context: list[dict[str, str]] = []
    for index, entry in enumerate(_source_link_context_from_items(items), start=1):
        enriched_entry = {"source_id": f"source-{index}", **entry}
        fingerprint = _skillization_content_fingerprint(source_by_url[entry["url"]])
        if fingerprint:
            enriched_entry["content_fingerprint"] = fingerprint
        context.append(enriched_entry)
    return context


def _source_link_context_from_markdown(markdown: str) -> list[dict[str, str]]:
    context: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for label, url, _start, _end in _find_markdown_inline_links(markdown):
        if url in seen_urls:
            continue
        seen_urls.add(url)
        context.append({"label": label, "url": url})
    return context


def _is_http_url(value: str) -> bool:
    if any(character.isspace() or character in '<>|"' for character in value):
        return False
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    return parsed.scheme in {"https", "http"} and bool(parsed.netloc) and bool(hostname)


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


def build_skillization_candidates_prompt(
    raw_items: str,
    *,
    chunk_index: int = 1,
    chunk_total: int = 1,
    title_fetcher=None,
    title_synthesizer=None,
    source_context: list[dict[str, str]] | None = None,
) -> str:
    parsed_items = json.loads(raw_items)
    prepared_items = parsed_items if source_context is not None else _prepare_items_for_prompt(parsed_items)
    effective_source_context = (
        source_context if source_context is not None else _skillization_source_context_from_items(prepared_items)
    )
    source_id_by_url = {entry["url"]: entry["source_id"] for entry in effective_source_context}
    compact_raw_items, _ = _compact_raw_items_for_prompt(
        json.dumps(prepared_items, ensure_ascii=False),
        title_fetcher=title_fetcher,
        title_synthesizer=title_synthesizer,
    )
    grounding = [
        {"source_id": source_id_by_url[item["url"]], **item}
        for item in json.loads(compact_raw_items)
        if isinstance(item.get("url"), str) and item["url"] in source_id_by_url
    ]
    raw_item_counts = {
        "total_items": len(prepared_items),
        "included_in_prompt": len(grounding),
        "omitted_from_prompt": max(len(prepared_items) - len(grounding), 0),
    }
    lines = [
        "あなたは Hermes Pulse のスキル化候補選定担当です。",
        "これは通常ニュースカテゴリとは独立した横断overlayです。通常カテゴリの分類・選定・順序・要約を変更しないでください。",
        "通常カテゴリとの同一URL・同一記事の重複を許可します。このoverlay内だけは同じsource_idを1回にしてください。",
        "将来のAgent実行を改善する、順序立った再利用可能workflow、非自明なCLI/API/tool技法、検証可能なprompt/判断framework、debug/test/research/design/automation手順、既存Skillやrepoのimport候補、既存Skillへ追加すべきpitfall/quality gateを選んでください。",
        "単なる発表、感想、一般論、孤立した事実、現在の人気だけが価値の情報、移植可能な手順がない宣伝は除外してください。",
        "候補化はSkill自動作成ではありません。未検証の主張は要検証とし、変換済み・導入済みとは書かないでください。",
        f"このpromptは候補選定chunk {chunk_index}/{chunk_total} です。",
        "",
        "出力はJSON配列だけにし、各要素を source_id / capability / destination / value の4フィールドにしてください。",
        "source_idはCandidate groundingから選び、URLは出力しないでください。capabilityはリンクを含まない簡潔な1行のplain textにしてください。",
        "destinationは `既存Skill更新` / `reference追加` / `script・template追加` / `新規class-level Skill` のいずれかにしてください。",
        "valueは `高` / `中` / `高・要検証` / `中・要検証` のいずれかにしてください。",
        "候補が1件もない場合は空のJSON配列 `[]` を返してください。前置き、コードフェンス、説明は不要です。",
        "",
        "## item counts",
        "```json",
        json.dumps(raw_item_counts, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Candidate grounding",
        "```json",
        json.dumps(grounding, ensure_ascii=False, indent=2),
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
        PRESERVE_INLINE_SOURCE_LINKS_INSTRUCTION,
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


def _chunk_items_by_count(items: list[dict[str, object]], chunk_size: int) -> list[list[dict[str, object]]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if not items:
        return [[]]
    return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]


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
