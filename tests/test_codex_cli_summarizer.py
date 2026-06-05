import json
from pathlib import Path

from hermes_pulse.summarization.codex_cli import (
    CodexCliSummarizer,
    build_category_summary_format_instructions,
    build_categorized_summary_format_instructions,
    build_codex_digest_prompt,
    build_codex_merge_prompt,
    build_summary_format_instructions,
)


def test_build_codex_digest_prompt_limits_embedded_raw_items_and_reports_omissions(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    items = [
        {
            "id": f"item-{index}",
            "source": "test-source",
            "source_kind": "document",
            "title": f"Title {index}",
            "excerpt": "excerpt " + ("x" * 500),
            "body": "body " + ("y" * 500),
            "url": f"https://example.com/{index}",
            "timestamps": {
                "created_at": f"2026-04-21T00:{index:02d}:00Z",
                "updated_at": None,
                "start_at": None,
                "end_at": None,
            },
            "provenance": {
                "provider": "example.com",
                "acquisition_mode": "fixture",
                "authority_tier": "primary",
                "primary_source_url": f"https://example.com/{index}",
                "raw_record_id": f"raw-{index}",
            },
        }
        for index in range(250)
    ]
    raw_items = json.dumps(items, ensure_ascii=False)
    (raw_directory / "collected-items.json").write_text(raw_items)

    prompt = build_codex_digest_prompt(archive_directory, raw_items)

    assert '"title": "Title 0"' in prompt
    assert '"title": "Title 249"' not in prompt
    assert "## item counts" in prompt
    assert '"included_in_prompt": 50' in prompt
    assert '"omitted_from_prompt": 200' in prompt


def test_build_summary_format_instructions_requires_inline_markdown_links_in_briefing_v1() -> None:
    instructions = build_summary_format_instructions("briefing-v1")

    assert any("文中" in line and "Markdown リンク" in line for line in instructions)
    assert any("URL を文末に列挙しない" in line for line in instructions)
    assert any("1 項目を複数行に分けない" in line for line in instructions)


def test_summary_prompt_layers_request_news_headline_length() -> None:
    instruction_sets = [
        build_summary_format_instructions("briefing-v1"),
        build_category_summary_format_instructions("AI"),
        build_categorized_summary_format_instructions("briefing-v1"),
        build_codex_merge_prompt(["▫ AI\n- Locally AI、LM Studio公式iPhoneアプリ化。LM Link対応"]).splitlines(),
    ]

    for instructions in instruction_sets:
        joined = "\n".join(instructions)
        assert "新聞・ニュースタイトル並み" in joined
        assert "事実が伝わる最小文字数" in joined
        assert "LocallyAI、LM Studio公式iPhoneアプリ化。LM Link対応" in joined


def test_build_summary_format_instructions_does_not_limit_primary_topics_to_fixed_small_range() -> None:
    instructions = build_summary_format_instructions("briefing-v1")

    assert not any("3〜6 件" in line for line in instructions)
    assert any("必要な件数" in line for line in instructions)


def test_build_codex_digest_prompt_embeds_fetched_titles_inline_without_separate_url_index(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    items = [
        {
            "id": f"item-{index}",
            "source": f"source-{index % 3}",
            "source_kind": "document",
            "title": f"Title {index}",
            "url": f"https://example.com/{index}",
        }
        for index in range(250)
    ]
    raw_items = json.dumps(items, ensure_ascii=False)
    (raw_directory / "collected-items.json").write_text(raw_items)

    prompt = build_codex_digest_prompt(archive_directory, raw_items)

    assert "## URL/title index for all URL-bearing items" not in prompt
    assert '"url": "https://example.com/0"' in prompt
    assert '"title": "Title 0"' in prompt
    assert '"url": "https://example.com/49"' in prompt
    assert '"title": "Title 49"' in prompt


def test_build_codex_digest_prompt_omits_internal_source_labels_from_llm_grounding(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    raw_items = json.dumps(
        [
            {
                "id": "x-home:1",
                "source": "x_home_timeline_reverse_chronological",
                "source_kind": "social_post",
                "title": "Qwen post",
                "excerpt": "Qwen3.6-27B on timeline",
                "url": "https://x.com/example/status/1",
                "provenance": {"acquisition_mode": "oauth2"},
            },
            {
                "id": "openai-news:1",
                "source": "openai-newsroom",
                "source_kind": "document",
                "title": "OpenAI launch",
                "excerpt": "Launch post",
                "url": "https://openai.com/index/launch",
                "provenance": {"acquisition_mode": "rss_poll"},
            },
        ],
        ensure_ascii=False,
    )
    (raw_directory / "collected-items.json").write_text(raw_items)

    prompt = build_codex_digest_prompt(archive_directory, raw_items)

    assert '"url": "https://x.com/example/status/1"' in prompt
    assert '"url": "https://openai.com/index/launch"' in prompt
    assert '"title": "Qwen post"' in prompt
    assert '"title": "OpenAI launch"' in prompt
    assert "x_home_timeline_reverse_chronological" not in prompt
    assert "openai-newsroom" not in prompt
    assert '"source_kind"' not in prompt
    assert '"provenance"' not in prompt
    assert "raw/collected-items.json" not in prompt
    assert str(archive_directory) not in prompt


def test_build_codex_digest_prompt_fetches_missing_title_for_url_items(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    raw_items = json.dumps(
        [
            {
                "id": "x-home:1",
                "source": "x_home_timeline_reverse_chronological",
                "source_kind": "social_post",
                "title": None,
                "excerpt": "Timeline excerpt should not become title",
                "url": "https://example.com/missing-title",
            }
        ],
        ensure_ascii=False,
    )
    (raw_directory / "collected-items.json").write_text(raw_items)

    prompt = build_codex_digest_prompt(
        archive_directory,
        raw_items,
        title_fetcher=lambda url: "Fetched title" if url == "https://example.com/missing-title" else None,
    )

    assert '"url": "https://example.com/missing-title"' in prompt
    assert '"title": "Fetched title"' in prompt
    assert "Timeline excerpt should not become title" in prompt


def test_build_codex_digest_prompt_synthesizes_missing_title_with_codex_spark_when_fetch_fails(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    raw_items = json.dumps(
        [
            {
                "id": "x-home:1",
                "source": "x_home_timeline_reverse_chronological",
                "source_kind": "social_post",
                "title": None,
                "excerpt": "Timeline excerpt should not become title",
                "body": "Body text",
                "url": "https://example.com/missing-title",
            }
        ],
        ensure_ascii=False,
    )
    (raw_directory / "collected-items.json").write_text(raw_items)

    prompt = build_codex_digest_prompt(
        archive_directory,
        raw_items,
        title_fetcher=lambda _url: None,
        title_synthesizer=lambda text, url: f"Spark title for {url}",
    )

    assert '"title": "Spark title for https://example.com/missing-title"' in prompt


def test_build_codex_digest_prompt_uses_neutral_fallback_title_for_untitled_urls(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    raw_items = json.dumps(
        [
            {
                "id": "internal:42",
                "source": "x_home_timeline_reverse_chronological",
                "source_kind": "social_post",
                "title": None,
                "excerpt": None,
                "body": None,
                "url": "https://example.com/no-title",
            }
        ],
        ensure_ascii=False,
    )
    (raw_directory / "collected-items.json").write_text(raw_items)

    prompt = build_codex_digest_prompt(
        archive_directory,
        raw_items,
        title_fetcher=lambda _url: None,
    )

    assert '"url": "https://example.com/no-title"' in prompt
    assert '"title": "example.com/no-title"' in prompt
    assert "x_home_timeline_reverse_chronological" not in prompt
    assert "internal:42" not in prompt


def test_build_codex_digest_prompt_prefers_longer_record_when_urls_match(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    raw_items = json.dumps(
        [
            {
                "id": "x-home:short",
                "source": "x_home_timeline_reverse_chronological",
                "source_kind": "social_post",
                "title": "Short title",
                "excerpt": "short excerpt",
                "body": None,
                "url": "https://example.com/shared",
            },
            {
                "id": "openai-news:long",
                "source": "openai-newsroom",
                "source_kind": "document",
                "title": "Long title wins",
                "excerpt": "longer excerpt with more detail",
                "body": "This is the much longer canonical body for the same URL and should survive dedupe.",
                "url": "https://example.com/shared",
            },
        ],
        ensure_ascii=False,
    )
    (raw_directory / "collected-items.json").write_text(raw_items)

    prompt = build_codex_digest_prompt(archive_directory, raw_items)

    assert prompt.count('"url": "https://example.com/shared"') == 1
    assert '"title": "Long title wins"' in prompt
    assert "much longer canonical body" in prompt
    assert '"title": "Short title"' not in prompt


def test_build_codex_digest_prompt_groups_related_titles_near_each_other(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    raw_items = json.dumps(
        [
            {
                "id": "misc-1",
                "title": "Apple supply chain note",
                "excerpt": "Unrelated Apple item",
                "url": "https://example.com/apple",
            },
            {
                "id": "openai-1",
                "title": "OpenAI launches Responses API update",
                "excerpt": "first OpenAI item",
                "url": "https://openai.com/blog/responses-api-update",
            },
            {
                "id": "misc-2",
                "title": "Bank of Japan outlook",
                "excerpt": "Unrelated finance item",
                "url": "https://example.com/boj",
            },
            {
                "id": "openai-2",
                "title": "OpenAI ships GPT-5 Responses improvements",
                "excerpt": "second OpenAI item",
                "url": "https://openai.com/blog/gpt-5-responses-improvements",
            },
        ],
        ensure_ascii=False,
    )
    (raw_directory / "collected-items.json").write_text(raw_items)

    prompt = build_codex_digest_prompt(archive_directory, raw_items)

    first = prompt.index('"title": "OpenAI launches Responses API update"')
    second = prompt.index('"title": "OpenAI ships GPT-5 Responses improvements"')
    apple = prompt.index('"title": "Apple supply chain note"')
    boj = prompt.index('"title": "Bank of Japan outlook"')
    assert first < second
    assert not (first < apple < second)
    assert not (first < boj < second)


def test_build_codex_merge_prompt_requests_light_compression_only() -> None:
    prompt = build_codex_merge_prompt([
        "☀ *Hermes Pulse Morning Briefing*\n\n▫ 主要トピック\n- A\n- B\n\n▫ 今日の予定・期限\n- なし",
        "☀ *Hermes Pulse Morning Briefing*\n\n▫ 主要トピック\n- C\n- D\n\n▫ 今日の予定・期限\n- なし",
    ])

    assert "最終版だけを返してください" in prompt
    assert "重要な事実は維持" in prompt
    assert "ニュース見出し並みに短く" in prompt
    assert "ほぼそのまま維持" not in prompt
    assert "明らかに関連する項目だけを軽く統合" in prompt
    assert "項目数を不必要に減らさない" in prompt
    assert "同じサービス・製品・AIモデルに関する話題は、会社・組織単位より優先してサービスごとにまとまりを意識して整理してください" in prompt
    assert "同じ会社・組織に関する話題は、上のサービス単位の整理を優先したうえで必要に応じて補助的にまとめてください" in prompt
    assert "自動車・EV関連の重要な製品動向、充電、電池、ソフトウェア更新も通常の主要トピック候補として扱ってください" in prompt
    assert "エンタメ・芸能・作品紹介そのものは原則として主要トピックに含めないでください" in prompt


def test_build_codex_merge_prompt_uses_category_headings_without_legacy_primary_topic_heading() -> None:
    prompt = build_codex_merge_prompt(["▫ AI\n- AI summary"])

    assert "▫ AI" in prompt
    assert "▫ IT" in prompt
    assert "▫ 金融" in prompt
    assert "▫ カメラ" in prompt
    assert "▫ 車" in prompt
    assert "▫ スケジュール" in prompt
    assert "▫ 主要トピック" not in prompt


def test_build_codex_merge_prompt_supports_evening_digest_headings() -> None:
    prompt = build_codex_merge_prompt(
        [
            "☾ *Hermes Pulse Evening Briefing*\n\n▫ 主要トピック\n- A\n\n▫ 明日の予定・期限\n- なし",
            "☾ *Hermes Pulse Evening Briefing*\n\n▫ 主要トピック\n- B\n\n▫ 明日の予定・期限\n- なし",
        ],
        digest_command="evening-digest",
    )

    assert "☾ *Hermes Pulse Evening Briefing*" in prompt
    assert "▫ 明日の予定・期限" in prompt
    assert "☀ *Hermes Pulse Morning Briefing*" not in prompt


def test_build_codex_digest_prompt_requests_company_grouping_and_excludes_entertainment(tmp_path: Path) -> None:
    archive_directory = tmp_path / "2026-04-25"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    raw_items = json.dumps(
        [
            {
                "id": "company-1",
                "title": "OpenAI ships enterprise feature",
                "excerpt": "OpenAI released a new business feature",
                "url": "https://example.com/openai-enterprise",
            },
            {
                "id": "ent-1",
                "title": "Movie trailer announced",
                "excerpt": "Entertainment only update",
                "url": "https://example.com/movie-trailer",
            },
        ],
        ensure_ascii=False,
    )
    (raw_directory / "collected-items.json").write_text(raw_items)

    prompt = build_codex_digest_prompt(archive_directory, raw_items)

    assert "同じサービス・製品・AIモデルに関する話題は、会社・組織単位より優先してサービスごとにまとまりを意識して整理してください" in prompt
    assert "同じ会社・組織に関する話題は、上のサービス単位の整理を優先したうえで必要に応じて補助的にまとめてください" in prompt
    assert "自動車・EV関連の重要な製品動向、充電、電池、ソフトウェア更新も通常の主要トピック候補として扱ってください" in prompt
    assert "エンタメ・芸能・作品紹介そのものは原則として主要トピックに含めないでください" in prompt


def test_codex_cli_summarizer_runs_category_prompts_before_final_merge(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "ai-1",
                    "source": "google-news-ai",
                    "title": "OpenAI launches agent runtime",
                    "url": "https://example.com/ai",
                    "metadata": {"category_hint": "ai"},
                },
                {
                    "id": "camera-1",
                    "source": "digital-camera-watch",
                    "title": "Sony announces compact full-frame camera",
                    "url": "https://example.com/camera",
                    "metadata": {"category_hint": "camera"},
                },
            ],
            ensure_ascii=False,
        )
    )
    prompts: list[str] = []

    class RecordingInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            prompts.append(prompt)
            if "最終編集担当" in prompt:
                return "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- AI summary\n\n▫ カメラ\n- Camera summary\n"
            if "大カテゴリ `AI`" in prompt:
                return "▫ AI\n- AI summary\n"
            if "大カテゴリ `カメラ`" in prompt:
                return "▫ カメラ\n- Camera summary\n"
            raise AssertionError(prompt)

    artifact = CodexCliSummarizer(invocation=RecordingInvocation()).summarize_archive(archive_directory)

    assert len(prompts) == 3
    assert "大カテゴリ `AI`" in prompts[0]
    assert "OpenAI launches agent runtime" in prompts[0]
    assert "Sony announces compact full-frame camera" not in prompts[0]
    assert "大カテゴリ `カメラ`" in prompts[1]
    assert "Sony announces compact full-frame camera" in prompts[1]
    assert "OpenAI launches agent runtime" not in prompts[1]
    assert "AI / IT / 金融 / カメラ / 車 / スケジュール" in prompts[2]
    assert artifact.content.startswith("☀ *Hermes Pulse Morning Briefing*")
    assert artifact.path.read_text() == artifact.content


def test_codex_cli_summarizer_runs_final_merge_even_for_single_category(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "ai-1",
                    "source": "google-news-ai",
                    "title": "OpenAI launches agent runtime",
                    "url": "https://example.com/ai",
                    "metadata": {"category_hint": "ai"},
                }
            ],
            ensure_ascii=False,
        )
    )
    prompts: list[str] = []

    class RecordingInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            prompts.append(prompt)
            if "最終編集担当" in prompt:
                return "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- AI summary\n"
            if "大カテゴリ `AI`" in prompt:
                return "▫ AI\n- AI summary\n"
            raise AssertionError(prompt)

    artifact = CodexCliSummarizer(invocation=RecordingInvocation()).summarize_archive(archive_directory)

    assert len(prompts) == 2
    assert "大カテゴリ `AI`" in prompts[0]
    assert "最終編集担当" in prompts[1]
    assert artifact.content.startswith("☀ *Hermes Pulse Morning Briefing*")
