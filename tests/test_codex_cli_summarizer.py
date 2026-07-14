import json
import logging
import re
from pathlib import Path

import pytest

import hermes_pulse.direct_delivery as direct_delivery
import hermes_pulse.summarization.codex_cli as codex_cli
from hermes_pulse.summarization.codex_cli import (
    CodexCliSummarizer,
    _combine_skillization_candidate_summaries,
    _extract_markdown_link_urls,
    _is_http_url,
    _normalize_skillization_candidate_output,
    build_category_summary_format_instructions,
    build_categorized_summary_format_instructions,
    build_codex_digest_prompt,
    build_codex_merge_prompt,
    build_skillization_candidates_prompt,
    build_summary_format_instructions,
)


def test_build_skillization_candidates_prompt_is_independent_and_allows_normal_category_overlap() -> None:
    raw_items = json.dumps(
        [
            {
                "title": "Reusable workflow",
                "excerpt": "A repeatable five-step procedure with verification.",
                "url": "https://example.com/workflow",
            }
        ],
        ensure_ascii=False,
    )

    prompt = build_skillization_candidates_prompt(raw_items, chunk_index=1, chunk_total=1)

    assert "通常ニュースカテゴリとは独立した横断overlay" in prompt
    assert "通常カテゴリとの同一URL・同一記事の重複を許可" in prompt
    for destination in ("既存Skill更新", "reference追加", "script・template追加", "新規class-level Skill"):
        assert destination in prompt
    assert "単なる発表、感想、一般論、孤立した事実" in prompt
    assert "空のJSON配列 `[]`" in prompt
    assert '"url": "https://example.com/workflow"' in prompt


def test_skillization_overlay_deduplicates_repeated_source_url_within_and_across_chunks() -> None:
    candidate = {
        "source_id": "source-1",
        "capability": "再利用可能なデバッグ手順",
        "destination": "既存Skill更新",
        "value": "高",
    }
    normalized = _normalize_skillization_candidate_output(
        json.dumps([candidate, candidate], ensure_ascii=False),
        source_context=[
            {
                "source_id": "source-1",
                "title": "Reusable workflow",
                "url": "https://example.com/workflow",
            }
        ],
    )

    assert len(normalized) == 1
    repeated_in_later_chunk = [{**normalized[0], "source_id": "source-2"}]
    overlay = _combine_skillization_candidate_summaries([normalized, repeated_in_later_chunk])

    assert overlay is not None
    assert overlay.count("https://example.com/workflow") == 1


def test_skillization_overlay_sanitizes_source_title_markup_and_unicode_controls() -> None:
    overlay = _combine_skillization_candidate_summaries(
        [
            [
                {
                    "source_id": "source-1",
                    "url": "https://example.com/workflow",
                    "title": "*bold* _italic_ ~strike~ `code` &lt;tag&gt; safe\u202etext",
                    "capability": "再利用可能な検証手順",
                    "destination": "既存Skill更新",
                    "value": "高",
                }
            ]
        ]
    )

    assert overlay is not None
    label = overlay.split("](", 1)[0]
    assert not any(character in label for character in "*_~`&")
    assert "\u202e" not in label
    assert "＊bold＊" in label


@pytest.mark.parametrize("capability", ["*bold*", "_italic_", "~strike~", "`code`", "safe\u202etext"])
def test_skillization_capability_rejects_slack_markup_and_unicode_format_controls(capability: str) -> None:
    output = json.dumps(
        [
            {
                "source_id": "source-1",
                "capability": capability,
                "destination": "既存Skill更新",
                "value": "高",
            }
        ],
        ensure_ascii=False,
    )

    with pytest.raises(RuntimeError, match="plain text"):
        _normalize_skillization_candidate_output(
            output,
            source_context=[{"source_id": "source-1", "url": "https://example.com/workflow"}],
        )


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_skillization_capability_rejects_unicode_line_separators_before_slack_sink(separator: str) -> None:
    normal_digest = "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- [Normal](https://example.com/normal)\n"
    output = json.dumps(
        [
            {
                "source_id": "source-1",
                "capability": f"safe{separator}- injected overlay bullet",
                "destination": "既存Skill更新",
                "value": "高",
            }
        ],
        ensure_ascii=False,
    )

    try:
        normalized = _normalize_skillization_candidate_output(
            output,
            source_context=[
                {
                    "source_id": "source-1",
                    "url": "https://example.com/workflow",
                    "title": "Workflow",
                }
            ],
        )
        overlay = _combine_skillization_candidate_summaries([normalized])
        rendered = codex_cli._append_skillization_overlay(normal_digest, overlay)
    except RuntimeError:
        rendered = normal_digest

    blocks = direct_delivery._build_slack_blocks(rendered)

    assert rendered == normal_digest
    assert "injected overlay bullet" not in json.dumps(blocks, ensure_ascii=False)


def test_skillization_overlay_deduplicates_canonical_url_variants_and_mirrored_content() -> None:
    source_context = codex_cli._skillization_source_context_from_items(
        [
            {
                "url": "https://EXAMPLE.com/%77orkflow?utm_source=pulse#section",
                "title": "Reusable agent workflow",
                "excerpt": "Five ordered steps with a verification gate.",
            },
            {
                "url": "https://example.com/workflow",
                "title": "Different title on canonical variant",
                "excerpt": "Different source text.",
            },
            {
                "url": "https://mirror.example.net/reprint",
                "title": "Reusable agent workflow",
                "excerpt": "Five ordered steps with a verification gate.",
            },
            {
                "url": "https://example.com/distinct",
                "title": "Distinct workflow",
                "excerpt": "A different reusable procedure.",
            },
        ]
    )
    assert source_context[0]["content_fingerprint"] == source_context[2]["content_fingerprint"]
    assert source_context[0]["content_fingerprint"] != source_context[1]["content_fingerprint"]
    output = json.dumps(
        [
            {
                "source_id": entry["source_id"],
                "capability": f"Reusable capability {index}",
                "destination": "既存Skill更新",
                "value": "高",
            }
            for index, entry in enumerate(source_context, start=1)
        ],
        ensure_ascii=False,
    )
    normalized = _normalize_skillization_candidate_output(output, source_context=source_context)

    overlay = _combine_skillization_candidate_summaries([normalized[:2], normalized[2:]])

    assert overlay is not None
    assert overlay.count("- [") == 2
    assert "https://EXAMPLE.com/%77orkflow?utm_source=pulse#section" in overlay
    assert "https://example.com/workflow" not in overlay
    assert "https://mirror.example.net/reprint" not in overlay
    assert "https://example.com/distinct" in overlay


def test_skillization_url_dedupe_is_conservative_and_normalizes_rfc_variants() -> None:
    canonicalize = codex_cli._canonicalize_url_for_dedupe

    assert canonicalize("https://example.com/render?source=alpha") != canonicalize(
        "https://example.com/render?source=beta"
    )
    assert canonicalize("https://example.com/render?id=1&id=2") != canonicalize(
        "https://example.com/render?id=2&id=1"
    )
    assert canonicalize("https://example.com/%7Euser") == canonicalize("https://example.com/~user")
    assert canonicalize("https://例え.テスト/path") == canonicalize("https://xn--r8jz45g.xn--zckzah/path")
    assert canonicalize("https://example.com/render?utm_source=pulse#section") == canonicalize(
        "https://example.com/render"
    )
    assert canonicalize("https://example.com/workflow") != canonicalize("https://example.com/workflow/")
    slash_overlay = _combine_skillization_candidate_summaries(
        [
            [
                {"url": "https://example.com/workflow", "title": "No slash", "capability": "One", "destination": "既存Skill更新", "value": "高"},
                {"url": "https://example.com/workflow/", "title": "With slash", "capability": "Two", "destination": "既存Skill更新", "value": "高"},
            ]
        ]
    )
    assert slash_overlay is not None
    assert slash_overlay.count("- [") == 2


def test_skillization_content_fingerprint_uses_untruncated_substantive_source_text() -> None:
    prefix = "x" * 180
    source_context = codex_cli._skillization_source_context_from_items(
        [
            {"url": "https://example.com/title-only", "title": "Shared words"},
            {"url": "https://example.com/excerpt-only", "excerpt": "Shared words"},
            {"url": "https://example.com/body-a", "title": "Same title", "body": "Body alpha"},
            {"url": "https://example.com/body-b", "title": "Same title", "body": "Body beta"},
            {"url": "https://example.com/mirror-a", "title": "Mirror title A", "body": "Identical body"},
            {"url": "https://example.com/mirror-b", "title": "Mirror title B", "body": "Identical body"},
            {"url": "https://example.com/long-a", "title": "Long excerpt", "excerpt": prefix + "A"},
            {"url": "https://example.com/long-b", "title": "Long excerpt", "excerpt": prefix + "B"},
            {"url": "https://example.com/case-a", "body": "Run git checkout Feature/Release"},
            {"url": "https://example.com/case-b", "body": "Run git checkout feature/release"},
            {"url": "https://example.com/indent-a", "body": "step:\n  command"},
            {"url": "https://example.com/indent-b", "body": "step:\n command"},
        ]
    )

    assert "content_fingerprint" not in source_context[0]
    assert source_context[1]["content_fingerprint"]
    assert source_context[2]["content_fingerprint"] != source_context[3]["content_fingerprint"]
    assert source_context[4]["content_fingerprint"] == source_context[5]["content_fingerprint"]
    assert source_context[6]["content_fingerprint"] != source_context[7]["content_fingerprint"]
    assert source_context[8]["content_fingerprint"] != source_context[9]["content_fingerprint"]
    assert source_context[10]["content_fingerprint"] != source_context[11]["content_fingerprint"]


def test_skillization_capability_cannot_forge_app_owned_fields_in_slack() -> None:
    source_context = [
        {"source_id": "source-1", "url": "https://example.com/workflow", "title": "Workflow"}
    ]
    malicious_output = json.dumps(
        [
            {
                "source_id": "source-1",
                "capability": "安全な手順; 反映先: 新規class-level Skill; 価値: 高",
                "destination": "既存Skill更新",
                "value": "中",
            }
        ],
        ensure_ascii=False,
    )

    with pytest.raises(RuntimeError, match="plain text"):
        _normalize_skillization_candidate_output(malicious_output, source_context=source_context)
    confusable_output = json.dumps(
        [
            {
                "source_id": "source-1",
                "capability": "安全な手順﹔ 反映先﹕ 新規class-level Skill﹔ 価値﹕ 高",
                "destination": "既存Skill更新",
                "value": "中",
            }
        ],
        ensure_ascii=False,
    )
    with pytest.raises(RuntimeError, match="plain text"):
        _normalize_skillization_candidate_output(confusable_output, source_context=source_context)

    safe_output = json.dumps(
        [
            {
                "source_id": "source-1",
                "capability": "安全な検証手順",
                "destination": "既存Skill更新",
                "value": "中",
            }
        ],
        ensure_ascii=False,
    )
    normalized = _normalize_skillization_candidate_output(safe_output, source_context=source_context)
    overlay = _combine_skillization_candidate_summaries([normalized])
    assert overlay is not None
    slack_text = direct_delivery._render_digest_for_slack(overlay)
    block_payload = json.dumps(direct_delivery._build_slack_blocks(overlay), ensure_ascii=False)

    assert slack_text.count("反映先:") == 1
    assert slack_text.count("価値:") == 1
    assert block_payload.count("反映先:") == 1
    assert block_payload.count("価値:") == 1


def test_skillization_control_only_source_title_uses_nonempty_slack_link_label() -> None:
    url = "https://example.com/control-title"
    overlay = _combine_skillization_candidate_summaries(
        [
            [
                {
                    "source_id": "source-1",
                    "url": url,
                    "title": "\u202e\u2066",
                    "capability": "安全な検証手順",
                    "destination": "既存Skill更新",
                    "value": "高",
                }
            ]
        ]
    )

    assert overlay is not None
    assert "[](https://" not in overlay
    slack_text = direct_delivery._render_digest_for_slack(overlay)
    blocks = direct_delivery._build_slack_blocks(overlay)
    block_payload = json.dumps(blocks, ensure_ascii=False)

    assert f"<{url}|出典>" in slack_text
    assert url in block_payload
    assert "出典" in block_payload


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


def test_summary_prompt_layers_require_source_url_inline_markdown_links() -> None:
    instruction_sets = [
        build_summary_format_instructions("briefing-v1"),
        build_category_summary_format_instructions("AI"),
        build_categorized_summary_format_instructions("briefing-v1"),
        build_codex_merge_prompt(["▫ AI\n- Anthropic、AI料金ショックで成長鈍化懸念"]).splitlines(),
    ]

    for instructions in instruction_sets:
        joined = "\n".join(instructions)
        assert "source の URL を使って" in joined
        assert "文中の重要語句を Markdown リンク" in joined
        assert "リンク可能なニュース箇条書きは必ず" in joined
        assert "URL を文末に列挙しない" in joined
        assert "1 項目を複数行に分けない" in joined


def test_codex_cli_summarizer_repairs_linkless_category_summary_at_summary_time(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "ai-1",
                    "source": "google-news-ai",
                    "title": "Anthropic、『AI料金ショック』で成長鈍化懸念",
                    "excerpt": "AnthropicのAI料金ショックが利用企業のコスト増につながっている。",
                    "url": "https://example.com/anthropic-pricing",
                    "metadata": {"category_hint": "ai"},
                }
            ],
            ensure_ascii=False,
        )
        + "\n"
    )
    prompts: list[str] = []

    class LinkRepairInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            prompts.append(prompt)
            if "スキル化候補選定担当" in prompt:
                return "[]\n"
            if len(prompts) == 1:
                return "▫ AI\n- Anthropic、AI料金ショックで成長鈍化懸念\n"
            if len(prompts) == 2:
                assert "前回のカテゴリ要約" in prompt
                assert "https://example.com/anthropic-pricing" in prompt
                return "▫ AI\n- Anthropic、[AI料金ショック](https://example.com/anthropic-pricing)で成長鈍化懸念\n"
            if len(prompts) == 3:
                return "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- Anthropic、[AI料金ショック](https://example.com/anthropic-pricing)で成長鈍化懸念\n"
            raise AssertionError(prompt)

    artifact = CodexCliSummarizer(invocation=LinkRepairInvocation()).summarize_archive(archive_directory)

    assert len(prompts) == 4
    assert artifact.partial_contents is None
    assert "[AI料金ショック](https://example.com/anthropic-pricing)" in artifact.content
    assert artifact.path.read_text() == artifact.content


def test_codex_cli_summarizer_repairs_final_merge_when_it_strips_markdown_links(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "ai-1",
                    "source": "google-news-ai",
                    "title": "Anthropic、『AI料金ショック』で成長鈍化懸念",
                    "url": "https://example.com/anthropic-pricing",
                    "metadata": {"category_hint": "ai"},
                }
            ],
            ensure_ascii=False,
        )
        + "\n"
    )
    prompts: list[str] = []

    class MergeRepairInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            prompts.append(prompt)
            if "スキル化候補選定担当" in prompt:
                return "[]\n"
            if len(prompts) == 1:
                return "▫ AI\n- Anthropic、[AI料金ショック](https://example.com/anthropic-pricing)で成長鈍化懸念\n"
            if len(prompts) == 2:
                return "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- Anthropic、AI料金ショックで成長鈍化懸念\n"
            if len(prompts) == 3:
                assert "前回の最終要約" in prompt
                assert "https://example.com/anthropic-pricing" in prompt
                return "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- Anthropic、[AI料金ショック](https://example.com/anthropic-pricing)で成長鈍化懸念\n"
            raise AssertionError(prompt)

    artifact = CodexCliSummarizer(invocation=MergeRepairInvocation()).summarize_archive(archive_directory)

    assert len(prompts) == 4
    assert "[AI料金ショック](https://example.com/anthropic-pricing)" in artifact.content
    assert artifact.path.read_text() == artifact.content


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
            if "スキル化候補選定担当" in prompt:
                return "[]\n"
            if "最終編集担当" in prompt:
                return (
                    "☀ *Hermes Pulse Morning Briefing*\n\n"
                    "▫ AI\n"
                    "- [AI summary](https://example.com/ai)\n\n"
                    "▫ カメラ\n"
                    "- [Camera summary](https://example.com/camera)\n"
                )
            if "大カテゴリ `AI`" in prompt:
                return "▫ AI\n- [AI summary](https://example.com/ai)\n"
            if "大カテゴリ `カメラ`" in prompt:
                return "▫ カメラ\n- [Camera summary](https://example.com/camera)\n"
            raise AssertionError(prompt)

    artifact = CodexCliSummarizer(invocation=RecordingInvocation()).summarize_archive(archive_directory)

    assert len(prompts) == 4
    assert "大カテゴリ `AI`" in prompts[0]
    assert "OpenAI launches agent runtime" in prompts[0]
    assert "Sony announces compact full-frame camera" not in prompts[0]
    assert "大カテゴリ `カメラ`" in prompts[1]
    assert "Sony announces compact full-frame camera" in prompts[1]
    assert "OpenAI launches agent runtime" not in prompts[1]
    assert "AI / IT / 金融 / カメラ / 車 / スケジュール" in prompts[2]
    assert "スキル化候補選定担当" in prompts[3]
    assert artifact.content.startswith("☀ *Hermes Pulse Morning Briefing*")
    assert artifact.path.read_text() == artifact.content


def test_codex_cli_summarizer_appends_skillization_overlay_without_changing_normal_digest(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "ai-workflow",
                    "title": "Agent debugging workflow",
                    "excerpt": "Five ordered steps and a verification gate.",
                    "url": "https://example.com/workflow",
                    "metadata": {"category_hint": "ai"},
                },
                {
                    "id": "camera-news",
                    "title": "Camera launch",
                    "url": "https://example.com/camera",
                    "metadata": {"category_hint": "camera"},
                },
            ],
            ensure_ascii=False,
        )
    )
    prompts: list[str] = []
    normal_digest = (
        "☀ *Hermes Pulse Morning Briefing*\n\n"
        "▫ AI\n- [Agent debugging workflow](https://example.com/workflow)\n\n"
        "▫ カメラ\n- [Camera launch](https://example.com/camera)\n"
    )

    class OverlayInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            prompts.append(prompt)
            if "スキル化候補選定担当" in prompt:
                return json.dumps(
                    [
                        {
                            "source_id": "source-1",
                            "capability": "再利用可能なデバッグ手順",
                            "destination": "既存Skill更新",
                            "value": "高",
                        }
                    ],
                    ensure_ascii=False,
                )
            if "最終編集担当" in prompt:
                return normal_digest
            if "大カテゴリ `AI`" in prompt:
                return "▫ AI\n- [Agent debugging workflow](https://example.com/workflow)\n"
            if "大カテゴリ `カメラ`" in prompt:
                return "▫ カメラ\n- [Camera launch](https://example.com/camera)\n"
            raise AssertionError(prompt)

    artifact = CodexCliSummarizer(invocation=OverlayInvocation()).summarize_archive(archive_directory)

    assert len(prompts) == 4
    assert all("スキル化候補" not in prompt for prompt in prompts[:3])
    assert "https://example.com/workflow" in prompts[0]
    assert "https://example.com/workflow" in prompts[3]
    assert artifact.content.startswith(normal_digest)
    assert artifact.content.count("https://example.com/workflow") == 2
    assert artifact.content.count("https://example.com/camera") == 1
    assert artifact.content.endswith("価値: 高\n")
    assert artifact.path.read_text() == artifact.content


def test_codex_cli_summarizer_preserves_normal_digest_exactly_when_no_skillization_candidates(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "announcement",
                    "title": "Product launch announcement",
                    "url": "https://example.com/announcement",
                    "metadata": {"category_hint": "it"},
                }
            ],
            ensure_ascii=False,
        )
    )
    normal_digest = "☀ *Hermes Pulse Morning Briefing*\n\n▫ IT\n- [Product launch](https://example.com/announcement)\n"

    class NoCandidateInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            if "スキル化候補選定担当" in prompt:
                return "[]\n"
            if "最終編集担当" in prompt:
                return normal_digest
            if "大カテゴリ `IT`" in prompt:
                return "▫ IT\n- [Product launch](https://example.com/announcement)\n"
            raise AssertionError(prompt)

    artifact = CodexCliSummarizer(invocation=NoCandidateInvocation()).summarize_archive(archive_directory)

    assert artifact.content == normal_digest
    assert artifact.path.read_text() == normal_digest


def test_codex_cli_summarizer_repairs_invalid_skillization_candidate_output(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "workflow",
                    "title": "Reusable debugging workflow",
                    "url": "https://example.com/workflow",
                    "metadata": {"category_hint": "ai"},
                }
            ],
            ensure_ascii=False,
        )
    )
    prompts: list[str] = []

    class RepairingInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            prompts.append(prompt)
            if "スキル化候補修正担当" in prompt:
                assert "前回のスキル化候補" in prompt
                assert "https://example.com/workflow" in prompt
                return json.dumps(
                    [
                        {
                            "source_id": "source-1",
                            "capability": "デバッグ手順",
                            "destination": "既存Skill更新",
                            "value": "高",
                        }
                    ],
                    ensure_ascii=False,
                )
            if "スキル化候補選定担当" in prompt:
                return "Reusable debugging workflow is useful but this output is malformed.\n"
            if "最終編集担当" in prompt:
                return "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- [Workflow](https://example.com/workflow)\n"
            if "大カテゴリ `AI`" in prompt:
                return "▫ AI\n- [Workflow](https://example.com/workflow)\n"
            raise AssertionError(prompt)

    artifact = CodexCliSummarizer(invocation=RepairingInvocation()).summarize_archive(archive_directory)

    assert len(prompts) == 4
    assert artifact.content.count("https://example.com/workflow") == 2
    assert "▫ スキル化候補" in artifact.content


def test_codex_cli_summarizer_binds_source_id_to_the_same_url_shown_in_overlay_prompt(
    tmp_path: Path,
) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    items = [
        {
            "id": "alpha",
            "title": "alpha",
            "url": "https://example.com/one",
            "metadata": {"category_hint": "ai"},
        },
        {
            "id": "bravo",
            "title": "bravo",
            "url": "https://example.com/two",
            "metadata": {"category_hint": "ai"},
        },
        {
            "id": "bravo-charlie",
            "title": "bravo charlie",
            "url": "https://example.com/three",
            "metadata": {"category_hint": "ai"},
        },
        {
            "id": "alpha-charlie",
            "title": "alpha charlie",
            "url": "https://example.com/four",
            "metadata": {"category_hint": "ai"},
        },
    ]
    (raw_directory / "collected-items.json").write_text(json.dumps(items))
    target_url = "https://example.com/three"

    class BridgeSignatureInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            if "スキル化候補選定担当" in prompt:
                grounding = json.loads(prompt.split("## Candidate grounding\n```json\n", 1)[1].split("\n```", 1)[0])
                target = next(entry for entry in grounding if entry["url"] == target_url)
                return json.dumps(
                    [
                        {
                            "source_id": target["source_id"],
                            "capability": "再利用可能な検証手順",
                            "destination": "既存Skill更新",
                            "value": "高",
                        }
                    ],
                    ensure_ascii=False,
                )
            if "最終編集担当" in prompt:
                return "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- [Alpha](https://example.com/one)\n"
            if "大カテゴリ `AI`" in prompt:
                return "▫ AI\n- [Alpha](https://example.com/one)\n"
            raise AssertionError(prompt)

    artifact = CodexCliSummarizer(invocation=BridgeSignatureInvocation()).summarize_archive(archive_directory)

    overlay = artifact.content.split("▫ スキル化候補", 1)[1]
    assert target_url in overlay
    assert "https://example.com/two" not in overlay


def test_codex_cli_summarizer_renders_only_grounded_source_url_from_structured_candidate(
    tmp_path: Path,
) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "workflow",
                    "title": "Reusable debugging workflow",
                    "url": "https://example.com/workflow",
                    "metadata": {"category_hint": "ai"},
                }
            ],
            ensure_ascii=False,
        )
    )
    normal_digest = "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- [Workflow](https://example.com/workflow)\n"
    prompts: list[str] = []

    class StructuredCandidateInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            prompts.append(prompt)
            if "スキル化候補修正担当" in prompt:
                return json.dumps(
                    [
                        {
                            "source_id": "source-1",
                            "capability": "再利用可能な安全なデバッグ手順",
                            "destination": "既存Skill更新",
                            "value": "高",
                        }
                    ],
                    ensure_ascii=False,
                )
            if "スキル化候補選定担当" in prompt:
                assert '"source_id": "source-1"' in prompt
                return json.dumps(
                    [
                        {
                            "source_id": "source-1",
                            "capability": "デバッグ手順 <https://evil.example/phish|認証が必要>",
                            "destination": "既存Skill更新",
                            "value": "高",
                        }
                    ],
                    ensure_ascii=False,
                )
            if "最終編集担当" in prompt:
                return normal_digest
            if "大カテゴリ `AI`" in prompt:
                return "▫ AI\n- [Workflow](https://example.com/workflow)\n"
            raise AssertionError(prompt)

    artifact = CodexCliSummarizer(invocation=StructuredCandidateInvocation()).summarize_archive(archive_directory)

    assert len(prompts) == 4
    assert artifact.content.count("https://example.com/workflow") == 2
    assert "https://evil.example/phish" not in artifact.content
    assert "再利用可能な安全なデバッグ手順" in artifact.content
    assert artifact.path.read_text() == artifact.content


def test_codex_cli_summarizer_preserves_parenthesized_source_url_in_structured_overlay(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    source_url = "https://example.com/wiki/Foo_(bar)"
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "workflow",
                    "title": "Foo workflow",
                    "url": source_url,
                    "metadata": {"category_hint": "ai"},
                }
            ]
        )
    )
    prompts: list[str] = []
    normal_digest = f"☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- [Foo workflow]({source_url})\n"

    class ParenthesizedUrlInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            prompts.append(prompt)
            if "スキル化候補選定担当" in prompt:
                return json.dumps(
                    [
                        {
                            "source_id": "source-1",
                            "capability": "再利用可能な検証手順",
                            "destination": "既存Skill更新",
                            "value": "高",
                        }
                    ],
                    ensure_ascii=False,
                )
            if "Markdown 要約修正担当" in prompt:
                return normal_digest
            if "最終編集担当" in prompt:
                return normal_digest
            if "大カテゴリ `AI`" in prompt:
                return f"▫ AI\n- [Foo workflow]({source_url})\n"
            raise AssertionError(prompt)

    artifact = CodexCliSummarizer(invocation=ParenthesizedUrlInvocation()).summarize_archive(archive_directory)

    assert not any("Markdown 要約修正担当" in prompt for prompt in prompts)
    assert artifact.content.count(source_url) == 2
    assert artifact.path.read_text() == artifact.content


def test_markdown_link_extraction_preserves_nested_parentheses_in_url() -> None:
    url = "https://example.com/wiki/A_(B_(C))"

    assert _extract_markdown_link_urls(f"- [Nested workflow]({url})\n") == [url]


def test_http_url_validation_accepts_bracketed_ipv6_literal() -> None:
    assert _is_http_url("http://[2001:db8::1]/workflow")


def test_codex_cli_summarizer_keeps_normal_digest_when_skillization_invocation_raises_oserror(
    tmp_path: Path,
    caplog,
) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "workflow",
                    "title": "Reusable debugging workflow",
                    "url": "https://example.com/workflow",
                    "metadata": {"category_hint": "ai"},
                }
            ]
        )
    )
    normal_digest = "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- [Workflow](https://example.com/workflow)\n"

    class FailedOverlayInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            if "スキル化候補選定担当" in prompt:
                raise OSError("overlay transport failed")
            if "最終編集担当" in prompt:
                return normal_digest
            if "大カテゴリ `AI`" in prompt:
                return "▫ AI\n- [Workflow](https://example.com/workflow)\n"
            raise AssertionError(prompt)

    with caplog.at_level(logging.WARNING):
        artifact = CodexCliSummarizer(invocation=FailedOverlayInvocation()).summarize_archive(archive_directory)

    assert artifact.content == normal_digest
    assert artifact.path.read_text() == normal_digest
    assert "Skipping skillization candidate chunk 1/1" in caplog.text
    assert "overlay transport failed" in caplog.text


def test_codex_cli_summarizer_keeps_normal_digest_when_skillization_combination_raises_valueerror(
    tmp_path: Path,
    caplog,
    monkeypatch,
) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "workflow",
                    "title": "Reusable debugging workflow",
                    "url": "https://example.com/workflow",
                    "metadata": {"category_hint": "ai"},
                }
            ]
        )
    )
    normal_digest = "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- [Workflow](https://example.com/workflow)\n"

    class CandidateInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            if "スキル化候補選定担当" in prompt:
                return json.dumps(
                    [
                        {
                            "source_id": "source-1",
                            "capability": "再利用可能な検証手順",
                            "destination": "既存Skill更新",
                            "value": "高",
                        }
                    ],
                    ensure_ascii=False,
                )
            if "最終編集担当" in prompt:
                return normal_digest
            if "大カテゴリ `AI`" in prompt:
                return "▫ AI\n- [Workflow](https://example.com/workflow)\n"
            raise AssertionError(prompt)

    monkeypatch.setattr(
        codex_cli,
        "_combine_skillization_candidate_summaries",
        lambda _summaries: (_ for _ in ()).throw(ValueError("overlay combination failed")),
    )

    with caplog.at_level(logging.WARNING):
        artifact = CodexCliSummarizer(invocation=CandidateInvocation()).summarize_archive(archive_directory)

    assert artifact.content == normal_digest
    assert artifact.path.read_text() == normal_digest
    assert "Skipping skillization overlay after finalization failure" in caplog.text
    assert "overlay combination failed" in caplog.text


def test_codex_cli_summarizer_keeps_normal_digest_when_skillization_preparation_raises_valueerror(
    tmp_path: Path,
    caplog,
    monkeypatch,
) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "workflow",
                    "title": "Reusable debugging workflow",
                    "url": "https://example.com/workflow",
                    "metadata": {"category_hint": "ai"},
                }
            ]
        )
    )
    normal_digest = "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- [Workflow](https://example.com/workflow)\n"
    original_prepare = codex_cli._prepare_items_for_prompt
    prepare_calls = 0

    def fail_overlay_preparation(items):
        nonlocal prepare_calls
        prepare_calls += 1
        if prepare_calls == 2:
            raise ValueError("overlay preparation failed")
        return original_prepare(items)

    class NormalDigestInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            if "最終編集担当" in prompt:
                return normal_digest
            if "大カテゴリ `AI`" in prompt:
                return "▫ AI\n- [Workflow](https://example.com/workflow)\n"
            raise AssertionError(prompt)

    monkeypatch.setattr(codex_cli, "_prepare_items_for_prompt", fail_overlay_preparation)

    with caplog.at_level(logging.WARNING):
        artifact = CodexCliSummarizer(invocation=NormalDigestInvocation()).summarize_archive(archive_directory)

    assert artifact.content == normal_digest
    assert artifact.path.read_text() == normal_digest
    assert "Skipping skillization overlay after preparation failure" in caplog.text
    assert "overlay preparation failed" in caplog.text


def test_codex_cli_summarizer_keeps_normal_digest_when_skillization_repair_fails(
    tmp_path: Path,
    caplog,
) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "workflow",
                    "title": "Reusable debugging workflow",
                    "url": "https://example.com/workflow",
                    "metadata": {"category_hint": "ai"},
                }
            ],
            ensure_ascii=False,
        )
    )
    normal_digest = "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- [Workflow](https://example.com/workflow)\n"

    class FailedOverlayInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            if "スキル化候補修正担当" in prompt:
                return "still malformed\n"
            if "スキル化候補選定担当" in prompt:
                return "malformed\n"
            if "最終編集担当" in prompt:
                return normal_digest
            if "大カテゴリ `AI`" in prompt:
                return "▫ AI\n- [Workflow](https://example.com/workflow)\n"
            raise AssertionError(prompt)

    with caplog.at_level(logging.WARNING):
        artifact = CodexCliSummarizer(invocation=FailedOverlayInvocation()).summarize_archive(archive_directory)

    assert artifact.content == normal_digest
    assert artifact.path.read_text() == normal_digest
    assert "Skipping skillization candidate chunk 1/1" in caplog.text


def test_codex_cli_summarizer_keeps_normal_digest_when_skillization_prompt_build_fails(
    tmp_path: Path,
    caplog,
) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    (raw_directory / "collected-items.json").write_text(
        json.dumps(
            [
                {
                    "id": "workflow",
                    "title": None,
                    "url": "https://example.com/workflow",
                    "metadata": {"category_hint": "ai"},
                }
            ]
        )
    )
    normal_digest = "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- [Workflow](https://example.com/workflow)\n"
    fetch_count = 0

    def title_fetcher(_url: str) -> str:
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count == 1:
            return "Resolved workflow title"
        raise RuntimeError("overlay title resolution failed")

    class NormalDigestInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            if "最終編集担当" in prompt:
                return normal_digest
            if "大カテゴリ `AI`" in prompt:
                return "▫ AI\n- [Workflow](https://example.com/workflow)\n"
            raise AssertionError(prompt)

    with caplog.at_level(logging.WARNING):
        artifact = CodexCliSummarizer(
            invocation=NormalDigestInvocation(),
            title_fetcher=title_fetcher,
        ).summarize_archive(archive_directory)

    assert artifact.content == normal_digest
    assert artifact.path.read_text() == normal_digest
    assert "Skipping skillization candidate chunk 1/1" in caplog.text


def test_codex_cli_summarizer_skillization_chunks_cover_every_input_with_hard_item_limit(tmp_path: Path) -> None:
    archive_directory = tmp_path / "archive"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)
    items = [
        {
            "id": f"item-{index}",
            "title": f"Workflow {index}",
            "body": "x" * (100_000 if index == 0 else 10),
            "url": f"https://example.com/{index}",
            "metadata": {"category_hint": "ai"},
        }
        for index in range(100)
    ]
    (raw_directory / "collected-items.json").write_text(json.dumps(items))
    prompts: list[str] = []

    class RecordingInvocation:
        def run(self, prompt: str, *, cwd: Path) -> str:
            prompts.append(prompt)
            if "スキル化候補選定担当" in prompt:
                return "[]\n"
            if "最終編集担当" in prompt:
                return "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- [Workflow 0](https://example.com/0)\n"
            if "大カテゴリ `AI`" in prompt:
                match = re.search(r'"url": "(https://example.com/\d+)"', prompt)
                assert match is not None
                return f"▫ AI\n- [Workflow]({match.group(1)})\n"
            raise AssertionError(prompt)

    CodexCliSummarizer(invocation=RecordingInvocation()).summarize_archive(archive_directory)

    overlay_prompts = [prompt for prompt in prompts if "スキル化候補選定担当" in prompt]
    groundings = [
        json.loads(prompt.split("## Candidate grounding\n```json\n", 1)[1].split("\n```", 1)[0])
        for prompt in overlay_prompts
    ]
    assert [len(grounding) for grounding in groundings] == [50, 50]
    assert {item["url"] for grounding in groundings for item in grounding} == {
        f"https://example.com/{index}" for index in range(100)
    }


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
            if "スキル化候補選定担当" in prompt:
                return "[]\n"
            if "最終編集担当" in prompt:
                return "☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- [AI summary](https://example.com/ai)\n"
            if "大カテゴリ `AI`" in prompt:
                return "▫ AI\n- [AI summary](https://example.com/ai)\n"
            raise AssertionError(prompt)

    artifact = CodexCliSummarizer(invocation=RecordingInvocation()).summarize_archive(archive_directory)

    assert len(prompts) == 3
    assert "大カテゴリ `AI`" in prompts[0]
    assert "最終編集担当" in prompts[1]
    assert "スキル化候補選定担当" in prompts[2]
    assert artifact.content.startswith("☀ *Hermes Pulse Morning Briefing*")
