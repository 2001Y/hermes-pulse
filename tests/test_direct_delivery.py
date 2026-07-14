import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

import hermes_pulse.direct_delivery as direct_delivery
from hermes_pulse.archive import write_morning_digest_archive
from hermes_pulse.db import list_connector_cursor_records
from hermes_pulse.models import CollectedItem, ItemTimestamps, Provenance
from hermes_pulse.summarization.base import SummaryArtifact


ROOT = Path(__file__).resolve().parents[1]
SOURCE_REGISTRY_PATH = ROOT / "fixtures/source_registry/sample_sources.yaml"
FEED_FIXTURE_PATH = ROOT / "fixtures/feed_samples/official_feed.xml"
SEARCH_FIXTURE_PATH = ROOT / "fixtures/search_samples/known_source_results.html"
HERMES_HISTORY_PATH = ROOT / "fixtures/hermes_history/sample_session.json"
CHATGPT_HISTORY_PATH = ROOT / "fixtures/chatgpt_history/sample_export"
GROK_HISTORY_PATH = ROOT / "fixtures/grok_history/sample_export"
NOTES_PATH = ROOT / "fixtures/notes/sample_notes.md"
DEFAULT_CODEX_MODEL = "gpt-5.4"
DEFAULT_SUMMARY_FORMAT = "briefing-v1"
EXPECTED_TITLE = "☀ *Hermes Pulse Morning Briefing*"
EXPECTED_EVENING_TITLE = "☾ *Hermes Pulse Evening Briefing*"
EXPECTED_PRIMARY_HEADING = "▫ 主要トピック"
EXPECTED_SCHEDULE_HEADING = "▫ 今日の予定・期限"
EXPECTED_EVENING_SCHEDULE_HEADING = "▫ 明日の予定・期限"


def _archived_item(source: str, item_id: str, title: str, url: str) -> CollectedItem:
    return CollectedItem(
        id=f"{source}:{item_id}",
        source=source,
        source_kind="document",
        title=title,
        excerpt=f"Excerpt for {title}",
        url=url,
        timestamps=ItemTimestamps(created_at="2026-04-21T08:00:00Z", updated_at="2026-04-21T08:00:00Z"),
        provenance=Provenance(
            provider="example.com",
            acquisition_mode="rss_poll",
            authority_tier="primary",
            primary_source_url=url,
            raw_record_id=item_id,
        ),
    )


def test_post_canonical_digest_to_slack_reads_exact_canonical_artifact(tmp_path: Path) -> None:
    archive_directory = tmp_path / date.today().isoformat()
    digest_path = archive_directory / "summary" / "codex-digest.md"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text("# Codex Digest\n\n- Exact canonical content\n")
    calls: list[dict[str, object]] = []

    def fake_post_message(
        text: str,
        channel: str,
        thread_ts: str | None = None,
        *,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
        blocks: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "text": text,
                "channel": channel,
                "thread_ts": thread_ts,
                "unfurl_links": unfurl_links,
                "unfurl_media": unfurl_media,
                "blocks": blocks,
            }
        )
        return {"ok": True, "channel": channel, "ts": "1712345.6789"}

    result = direct_delivery.post_canonical_digest_to_slack(
        archive_directory,
        channel="C123",
        thread_ts="1712345.6789",
        post_message=fake_post_message,
    )

    assert calls == [
        {
            "text": "# Codex Digest\n\n- Exact canonical content\n",
            "channel": "C123",
            "thread_ts": "1712345.6789",
            "unfurl_links": False,
            "unfurl_media": False,
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {"type": "rich_text_section", "elements": [{"type": "text", "text": "# Codex Digest"}]},
                        {"type": "rich_text_list", "style": "bullet", "elements": [
                            {"type": "rich_text_section", "elements": [{"type": "text", "text": "Exact canonical content"}]}
                        ]},
                    ],
                }
            ],
        }
    ]
    assert result.archive_directory == archive_directory
    assert result.digest_path == digest_path
    assert result.content == digest_path.read_text()
    assert result.slack_response == {"ok": True, "channel": "C123", "ts": "1712345.6789"}


def test_post_canonical_digest_to_slack_prepends_grok_fallback_notice_when_history_used(tmp_path: Path) -> None:
    archive_directory = tmp_path / date.today().isoformat()
    digest_path = archive_directory / "summary" / "codex-digest.md"
    raw_items_path = archive_directory / "raw" / "collected-items.json"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    raw_items_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text("☀ *Hermes Pulse Morning Briefing*\n\n▫ 主要トピック\n- test\n")
    raw_items_path.write_text(
        json.dumps(
            [
                {
                    "id": "conv-1",
                    "source": "grok_history",
                    "source_kind": "conversation",
                    "title": "Fallback title",
                    "excerpt": None,
                    "body": None,
                    "url": None,
                    "timestamps": {"created_at": None, "updated_at": "2026-04-21T12:00:00Z", "start_at": None, "end_at": None},
                    "provenance": {
                        "provider": "grok",
                        "acquisition_mode": "local_browser_history",
                        "authority_tier": None,
                        "primary_source_url": None,
                        "raw_record_id": "conv-1",
                    },
                    "metadata": {"response_count": 0},
                }
            ]
        )
    )
    calls: list[str] = []

    result = direct_delivery.post_canonical_digest_to_slack(
        archive_directory,
        channel="C123",
        post_message=lambda text, *_args, **_kwargs: calls.append(text) or {"ok": True, "channel": "C123", "ts": "1"},
    )

    assert calls == [
        "⚠ Grok履歴はフォールバック（Chrome History）で取得。会話本文は未取得または不完全の可能性があります。\n\n☀ *Hermes Pulse Morning Briefing*\n\n▫ 主要トピック\n- test\n"
    ]
    assert result.content == digest_path.read_text()


def test_post_canonical_digest_to_slack_prepends_source_error_notice_when_metadata_exists(tmp_path: Path) -> None:
    archive_directory = tmp_path / date.today().isoformat()
    digest_path = archive_directory / "summary" / "codex-digest.md"
    metadata_path = archive_directory / "metadata" / "source-errors.json"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text("☀ *Hermes Pulse Morning Briefing*\n\n▫ 主要トピック\n- test\n")
    metadata_path.write_text(json.dumps({"x_signals": "401 Unauthorized"}, ensure_ascii=False) + "\n")
    calls: list[str] = []

    result = direct_delivery.post_canonical_digest_to_slack(
        archive_directory,
        channel="C123",
        post_message=lambda text, *_args, **_kwargs: calls.append(text) or {"ok": True, "channel": "C123", "ts": "1"},
    )

    assert calls == [
        "⚠ 一部ソース取得に失敗:\n- x_signals: 401 Unauthorized\n\n☀ *Hermes Pulse Morning Briefing*\n\n▫ 主要トピック\n- test\n"
    ]
    assert result.content == digest_path.read_text()


def test_post_canonical_digest_to_slack_simplifies_x_spend_cap_source_error(tmp_path: Path) -> None:
    archive_directory = tmp_path / date.today().isoformat()
    digest_path = archive_directory / "summary" / "codex-digest.md"
    metadata_path = archive_directory / "metadata" / "source-errors.json"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text("☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- test\n")
    metadata_path.write_text(
        json.dumps(
            {
                "x_signals": "xurl oauth2 /2/users/42/bookmarks failed: SpendCapReached: X API spend cap reached; blocked until 2026-06-18"
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    calls: list[str] = []

    direct_delivery.post_canonical_digest_to_slack(
        archive_directory,
        channel="C123",
        post_message=lambda text, *_args, **_kwargs: calls.append(text) or {"ok": True, "channel": "C123", "ts": "1"},
    )

    assert calls == [
        "⚠ 一部ソース取得に失敗:\n- x_signals: X API spend cap到達。2026-06-18までX由来をスキップ。\n\n☀ *Hermes Pulse Morning Briefing*\n\n▫ AI\n- test\n"
    ]


def test_post_canonical_digest_to_slack_converts_markdown_links_and_bullets_to_slack_friendly_text(tmp_path: Path) -> None:
    archive_directory = tmp_path / date.today().isoformat()
    digest_path = archive_directory / "summary" / "codex-digest.md"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text("# Codex Digest\n\n- [Launch update](https://example.com/posts/launch-update)\n- Second item\n")
    calls: list[dict[str, object]] = []

    def fake_post_message(
        text: str,
        channel: str,
        thread_ts: str | None = None,
        *,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
        blocks: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "text": text,
                "channel": channel,
                "thread_ts": thread_ts,
                "unfurl_links": unfurl_links,
                "unfurl_media": unfurl_media,
                "blocks": blocks,
            }
        )
        return {"ok": True, "channel": channel, "ts": "1712345.6789"}

    result = direct_delivery.post_canonical_digest_to_slack(
        archive_directory,
        channel="C123",
        post_message=fake_post_message,
    )

    assert calls == [
        {
            "text": "# Codex Digest\n\n- <https://example.com/posts/launch-update|Launch update>\n- Second item\n",
            "channel": "C123",
            "thread_ts": None,
            "unfurl_links": False,
            "unfurl_media": False,
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {"type": "rich_text_section", "elements": [{"type": "text", "text": "# Codex Digest"}]},
                        {"type": "rich_text_list", "style": "bullet", "elements": [
                            {"type": "rich_text_section", "elements": [{"type": "link", "url": "https://example.com/posts/launch-update", "text": "Launch update"}]},
                            {"type": "rich_text_section", "elements": [{"type": "text", "text": "Second item"}]},
                        ]},
                    ],
                }
            ],
        }
    ]
    assert result.content == digest_path.read_text()


def test_post_canonical_digest_to_slack_does_not_rewrite_unlinked_bullets_from_raw_items(tmp_path: Path) -> None:
    archive_directory = tmp_path / date.today().isoformat()
    digest_path = archive_directory / "summary" / "codex-digest.md"
    raw_items_path = archive_directory / "raw" / "collected-items.json"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    raw_items_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(
        "☀ *Hermes Pulse Morning Briefing*\n\n"
        "▫ AI\n"
        "- Anthropic、AI料金ショックで成長鈍化懸念\n"
    )
    raw_items_path.write_text(
        json.dumps(
            [
                {
                    "id": "news:anthropic-pricing",
                    "source": "news",
                    "source_kind": "document",
                    "title": "Anthropic、『AI料金ショック』で成長鈍化懸念",
                    "excerpt": "AnthropicのAI料金ショックが利用企業のコスト増につながっている。",
                    "url": "https://example.com/anthropic-pricing",
                }
            ],
            ensure_ascii=False,
        )
        + "\n"
    )
    calls: list[str] = []

    result = direct_delivery.post_canonical_digest_to_slack(
        archive_directory,
        channel="C123",
        post_message=lambda text, *_args, **_kwargs: calls.append(text) or {"ok": True, "channel": "C123", "ts": "1"},
    )

    assert calls == [
        "☀ *Hermes Pulse Morning Briefing*\n\n"
        "▫ AI\n"
        "- Anthropic、AI料金ショックで成長鈍化懸念\n"
    ]
    assert result.content == digest_path.read_text()


def test_post_canonical_digest_to_slack_splits_oversized_digest_into_parent_with_thread_replies(tmp_path: Path) -> None:
    archive_directory = tmp_path / date.today().isoformat()
    digest_path = archive_directory / "summary" / "codex-digest.md"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(
        "☀ *Hermes Pulse Morning Briefing*\n\n"
        "▫ 主要トピック\n"
        + "\n".join(
            f"- Item {index}: [Link {index}](https://example.com/{index}) " + ("x" * 70)
            for index in range(1, 7)
        )
        + "\n\n▫ 今日の予定・期限\n- なし\n"
    )
    calls: list[dict[str, object]] = []

    def fake_post_message(
        text: str,
        channel: str,
        thread_ts: str | None = None,
        *,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
        blocks: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "text": text,
                "channel": channel,
                "thread_ts": thread_ts,
                "unfurl_links": unfurl_links,
                "unfurl_media": unfurl_media,
                "blocks": blocks,
            }
        )
        return {"ok": True, "channel": channel, "ts": f"1712345.67{len(calls)}"}

    result = direct_delivery.post_canonical_digest_to_slack(
        archive_directory,
        channel="C123",
        post_message=fake_post_message,
        slack_message_limit=180,
    )

    assert len(calls) >= 2
    assert calls[0]["thread_ts"] is None
    parent_ts = result.slack_responses[0]["ts"]
    assert parent_ts == "1712345.671"
    assert all(call["thread_ts"] == parent_ts for call in calls[1:])
    assert all("[Link" not in call["text"] for call in calls)
    assert all("<https://example.com/" in call["text"] or "- なし" in call["text"] for call in calls)
    assert all("xxxxxxxxxx" in call["text"] or "- なし" in call["text"] or "☀ *Hermes Pulse Morning Briefing*" in call["text"] for call in calls)
    assert result.slack_response == {"ok": True, "channel": "C123", "ts": f"1712345.67{len(calls)}"}
    assert result.slack_responses == [
        {"ok": True, "channel": "C123", "ts": f"1712345.67{index}"}
        for index in range(1, len(calls) + 1)
    ]
    assert result.posted_messages == [call["text"] for call in calls]
    joined = "\n".join(call["text"] for call in calls)
    for index in range(1, 7):
        expected_line = f"- Item {index}: <https://example.com/{index}|Link {index}> " + ("x" * 70)
        assert expected_line in joined


def test_split_slack_text_prefers_list_item_boundaries_over_mid_item_splits() -> None:
    text = (
        "☀ *Hermes Pulse Morning Briefing*\n\n"
        "▫ 主要トピック\n"
        "- Item 1: " + ("a" * 60) + "\n"
        "- Item 2: " + ("b" * 60) + "\n"
        "- Item 3: " + ("c" * 60) + "\n"
    )

    chunks = direct_delivery._split_slack_text(text, limit=120)

    assert len(chunks) >= 2
    assert all("\n- Item 2: " not in chunk[-20:] for chunk in chunks[:-1])
    assert all("\n- Item 3: " not in chunk[-20:] for chunk in chunks[:-1])
    assert any(chunk.startswith("- Item 2:") for chunk in chunks[1:])
    assert any(chunk.startswith("- Item 3:") for chunk in chunks[1:])


def test_build_parser_uses_hermes_pulse_direct_delivery_program_name() -> None:
    assert direct_delivery.build_parser().prog == "hermes-pulse-direct-delivery"


def test_run_morning_digest_direct_delivery_can_post_a_week_window_from_source_ledgers(
    monkeypatch, tmp_path: Path
) -> None:
    archive_root = tmp_path / "pulse-archive"
    write_morning_digest_archive(
        items=[_archived_item("weekly-source", "carryover", "Weekly carryover", "https://example.com/weekly-carryover")],
        archive_root=archive_root,
        archive_date="2026-04-21",
        retrieved_at="2026-04-21T08:00:00Z",
    )

    def fake_summarize(archive_directory: Path, **_kwargs) -> SummaryArtifact:
        raw_items = json.loads((archive_directory / "raw" / "collected-items.json").read_text())
        content = "# Codex Digest\n\n" + "".join(f"- {item['title']}\n" for item in raw_items)
        digest_path = archive_directory / "summary" / "codex-digest.md"
        digest_path.parent.mkdir(parents=True, exist_ok=True)
        digest_path.write_text(content)
        return SummaryArtifact(path=digest_path, content=content)

    monkeypatch.setattr(direct_delivery, "_summarize_archive_with_retries", fake_summarize)

    calls: list[dict[str, object]] = []
    args = direct_delivery.build_parser().parse_args(
        [
            "--source-registry",
            str(SOURCE_REGISTRY_PATH),
            "--feed-fixture",
            str(FEED_FIXTURE_PATH),
            "--search-fixture",
            str(SEARCH_FIXTURE_PATH),
            "--hermes-history",
            str(HERMES_HISTORY_PATH),
            "--notes",
            str(NOTES_PATH),
            "--archive-root",
            str(archive_root),
            "--archive-label",
            "2026-week-17",
            "--window-start",
            "2026-04-21",
            "--window-end",
            "2026-04-24",
            "--now",
            "2026-04-23T08:00:00Z",
            "--channel",
            "C123",
        ]
    )

    result = direct_delivery.run_morning_digest_direct_delivery(
        args,
        post_message=lambda text, channel, thread_ts=None, **kwargs: calls.append(
            {"text": text, "channel": channel, "thread_ts": thread_ts, "blocks": kwargs.get("blocks")}
        )
        or {"ok": True, "channel": channel, "ts": "1712345.6789"},
    )

    assert result.archive_directory.name == "2026-week-17"
    assert "Weekly carryover" in calls[0]["text"]
    assert "Launch update" in calls[0]["text"]


def test_post_canonical_digest_to_slack_fails_clearly_when_canonical_artifact_is_missing(
    tmp_path: Path,
) -> None:
    archive_directory = tmp_path / date.today().isoformat()

    with pytest.raises(FileNotFoundError, match=r"summary/codex-digest\.md"):
        direct_delivery.post_canonical_digest_to_slack(
            archive_directory,
            channel="C123",
            post_message=lambda *args, **kwargs: {"ok": True},
        )


def test_run_morning_digest_direct_delivery_posts_only_combined_summary_when_partials_exist(
    monkeypatch,
    tmp_path: Path,
) -> None:
    archive_root = tmp_path / "archive-root"
    posted: list[dict[str, object]] = []

    def fake_post_message(
        text: str,
        channel: str,
        thread_ts: str | None = None,
        *,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
        blocks: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        posted.append({
            "text": text,
            "channel": channel,
            "thread_ts": thread_ts,
            "blocks": blocks,
        })
        return {"ok": True, "channel": channel, "ts": f"1712345.67{len(posted)}"}

    def fake_summarize(archive_directory: Path, **_kwargs) -> SummaryArtifact:
        archive_directory = Path(archive_directory)
        output_path = archive_directory / "summary" / "codex-digest.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("# Combined\n")
        return SummaryArtifact(
            path=output_path,
            content="# Combined\n",
            partial_contents=[
                "# Part 1\n\n- First\n",
                "# Part 2\n\n- Second\n",
            ],
        )

    monkeypatch.setattr(direct_delivery, "_summarize_archive_with_retries", fake_summarize)

    assert (
        direct_delivery.main(
            [
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--feed-fixture",
                str(FEED_FIXTURE_PATH),
                "--search-fixture",
                str(SEARCH_FIXTURE_PATH),
                "--hermes-history",
                str(HERMES_HISTORY_PATH),
                "--notes",
                str(NOTES_PATH),
                "--archive-root",
                str(archive_root),
                "--channel",
                "C123",
            ],
            post_message=fake_post_message,
        )
        == 0
    )

    assert [call["text"] for call in posted] == ["# Combined\n"]
    assert posted[0]["thread_ts"] is None


def test_post_canonical_digest_to_slack_splits_only_combined_summary_when_partials_exist(tmp_path: Path) -> None:
    archive_directory = tmp_path / date.today().isoformat()
    digest_path = archive_directory / "summary" / "codex-digest.md"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(
        "# Combined Digest\n\n"
        + "\n\n".join(
            f"Combined paragraph {index}: [Link {index}](https://example.com/{index}) " + ("y" * 90)
            for index in range(1, 7)
        )
        + "\n"
    )
    calls: list[dict[str, object]] = []

    def fake_post_message(
        text: str,
        channel: str,
        thread_ts: str | None = None,
        *,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
        blocks: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        calls.append({
            "text": text,
            "channel": channel,
            "thread_ts": thread_ts,
            "unfurl_links": unfurl_links,
            "unfurl_media": unfurl_media,
            "blocks": blocks,
        })
        return {"ok": True, "channel": channel, "ts": f"1712345.67{len(calls)}"}

    result = direct_delivery.post_canonical_digest_to_slack(
        archive_directory,
        channel="C123",
        post_message=fake_post_message,
        slack_message_limit=180,
        summary_artifact=SummaryArtifact(
            path=digest_path,
            content=digest_path.read_text(),
            partial_contents=["# Part 1\n\n- First\n", "# Part 2\n\n- Second\n"],
        ),
    )

    assert len(calls) >= 2
    assert calls[0]["thread_ts"] is None
    parent_ts = result.slack_responses[0]["ts"]
    assert all(call["thread_ts"] == parent_ts for call in calls[1:])
    assert all("Part 1" not in call["text"] for call in calls)
    assert all("Part 2" not in call["text"] for call in calls)
    assert all("Combined" in call["text"] for call in calls)
    assert result.posted_messages == [call["text"] for call in calls]


def test_main_runs_morning_digest_pipeline_and_posts_exact_canonical_digest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    summary_template = "# Codex Digest\n\n- Canonical archive summary\n"
    codex_calls: list[Path] = []

    class StubCodexCliSummarizer:
        def __init__(self, *, model: str = DEFAULT_CODEX_MODEL, summary_format: str = DEFAULT_SUMMARY_FORMAT) -> None:
            assert model == DEFAULT_CODEX_MODEL
            assert summary_format == DEFAULT_SUMMARY_FORMAT

        def summarize_archive(self, archive_directory: str | Path) -> SummaryArtifact:
            archive_directory = Path(archive_directory)
            codex_calls.append(archive_directory)
            output_path = archive_directory / "summary" / "codex-digest.md"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(summary_template)
            return SummaryArtifact(path=output_path, content="# Returned content that must not be posted\n")

    monkeypatch.setattr(direct_delivery, "CodexCliSummarizer", StubCodexCliSummarizer)
    archive_root = tmp_path / "archive-root"
    archive_date = date.today().isoformat()
    slack_calls: list[dict[str, object]] = []

    def fake_post_message(
        text: str,
        channel: str,
        thread_ts: str | None = None,
        *,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
        blocks: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        slack_calls.append(
            {
                "text": text,
                "channel": channel,
                "thread_ts": thread_ts,
                "unfurl_links": unfurl_links,
                "unfurl_media": unfurl_media,
                "blocks": blocks,
            }
        )
        return {"ok": True, "channel": channel, "ts": "1712345.6789"}

    assert (
        direct_delivery.main(
            [
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--feed-fixture",
                str(FEED_FIXTURE_PATH),
                "--search-fixture",
                str(SEARCH_FIXTURE_PATH),
                "--hermes-history",
                str(HERMES_HISTORY_PATH),
                "--notes",
                str(NOTES_PATH),
                "--archive-root",
                str(archive_root),
                "--channel",
                "C123",
                "--thread-ts",
                "1712345.6789",
            ],
            post_message=fake_post_message,
        )
        == 0
    )

    archive_directory = archive_root / archive_date
    digest_path = archive_directory / "summary" / "codex-digest.md"

    assert codex_calls == [archive_directory]
    assert digest_path.read_text() == summary_template
    assert slack_calls == [
        {
            "text": summary_template,
            "channel": "C123",
            "thread_ts": "1712345.6789",
            "unfurl_links": False,
            "unfurl_media": False,
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {"type": "rich_text_section", "elements": [{"type": "text", "text": "# Codex Digest"}]},
                        {"type": "rich_text_list", "style": "bullet", "elements": [
                            {"type": "rich_text_section", "elements": [{"type": "text", "text": "Canonical archive summary"}]}
                        ]},
                    ],
                }
            ],
        }
    ]


def test_build_parser_accepts_digest_command_codex_model_and_summary_format() -> None:
    args = direct_delivery.build_parser().parse_args(
        [
            "--command",
            "evening-digest",
            "--channel",
            "D123",
            "--codex-model",
            DEFAULT_CODEX_MODEL,
            "--summary-format",
            DEFAULT_SUMMARY_FORMAT,
        ]
    )

    assert args.command == "evening-digest"
    assert args.codex_model == DEFAULT_CODEX_MODEL
    assert args.summary_format == DEFAULT_SUMMARY_FORMAT
    assert direct_delivery.build_parser().parse_args(["--channel", "D123", "--chatgpt-history", str(CHATGPT_HISTORY_PATH)]).chatgpt_history == CHATGPT_HISTORY_PATH
    assert direct_delivery.build_parser().parse_args(["--channel", "D123", "--grok-history", str(GROK_HISTORY_PATH)]).grok_history == GROK_HISTORY_PATH


def test_run_digest_direct_delivery_uses_requested_digest_command(monkeypatch, tmp_path: Path) -> None:
    archive_root = tmp_path / "archive-root"
    requested_commands: list[str] = []
    occurred_at_commands: list[str] = []
    summarized_archives: list[Path] = []

    def fake_build_digest_with_source_errors(command: str, _args: object):
        requested_commands.append(command)
        return ([_archived_item("digest-source", "item-1", f"{command} item", "https://example.com/item-1")], {}, ["digest-source"])

    def fake_occurred_at_for_command(command: str, _args: object) -> str:
        occurred_at_commands.append(command)
        return "2026-04-21T18:00:00Z"

    def fake_summarize(archive_directory: Path, **_kwargs) -> SummaryArtifact:
        archive_directory = Path(archive_directory)
        summarized_archives.append(archive_directory)
        digest_path = archive_directory / "summary" / "codex-digest.md"
        digest_path.parent.mkdir(parents=True, exist_ok=True)
        digest_path.write_text("# Evening\n\n- Item\n")
        return SummaryArtifact(path=digest_path, content="# Evening\n\n- Item\n")

    monkeypatch.setattr(direct_delivery, "_build_digest_with_source_errors", fake_build_digest_with_source_errors)
    monkeypatch.setattr(direct_delivery, "_occurred_at_for_command", fake_occurred_at_for_command)
    monkeypatch.setattr(direct_delivery, "_summarize_archive_with_retries", fake_summarize)

    args = direct_delivery.build_parser().parse_args(
        [
            "--command",
            "evening-digest",
            "--archive-root",
            str(archive_root),
            "--channel",
            "C123",
        ]
    )

    result = direct_delivery.run_digest_direct_delivery(
        args,
        post_message=lambda text, channel, thread_ts=None, **kwargs: {"ok": True, "text": text, "channel": channel, "thread_ts": thread_ts, "blocks": kwargs.get("blocks"), "ts": "1712345.6789"},
    )

    assert requested_commands == ["evening-digest"]
    assert occurred_at_commands == ["evening-digest"]
    assert summarized_archives == [archive_root / date.today().isoformat()]
    assert result.archive_directory == archive_root / date.today().isoformat()


def test_briefing_v1_summary_format_instructions_define_requested_headings() -> None:
    instructions = direct_delivery.build_summary_format_instructions(DEFAULT_SUMMARY_FORMAT)

    assert EXPECTED_TITLE in instructions[1]
    assert EXPECTED_PRIMARY_HEADING in instructions[1]
    assert EXPECTED_SCHEDULE_HEADING in instructions[1]
    assert "気になるメモ" not in "\n".join(instructions)
    assert "internal source 名に引きずられず" in "\n".join(instructions)


def test_briefing_v1_evening_summary_format_instructions_define_requested_headings() -> None:
    instructions = direct_delivery.build_summary_format_instructions(DEFAULT_SUMMARY_FORMAT, digest_command="evening-digest")

    assert EXPECTED_EVENING_TITLE in instructions[1]
    assert EXPECTED_PRIMARY_HEADING in instructions[1]
    assert EXPECTED_EVENING_SCHEDULE_HEADING in instructions[1]
    assert EXPECTED_TITLE not in instructions[1]


def test_summarize_archive_with_retries_uses_requested_model_and_format() -> None:
    calls: list[dict[str, object]] = []

    class FakeSummarizer:
        def summarize_archive(self, archive_directory: str | Path) -> SummaryArtifact:
            calls.append({"archive_directory": str(archive_directory)})
            return SummaryArtifact(path=Path("/tmp/codex-digest.md"), content="# digest")

    artifact = direct_delivery._summarize_archive_with_retries(
        Path("/tmp/archive"),
        codex_model=DEFAULT_CODEX_MODEL,
        summary_format=DEFAULT_SUMMARY_FORMAT,
        retry_delays_seconds=(),
        summarizer_factory=lambda **kwargs: FakeSummarizer(),
        sleep=lambda _seconds: None,
    )

    assert artifact.content == "# digest"
    assert calls == [{"archive_directory": "/tmp/archive"}]


def test_summarize_archive_with_retries_supports_legacy_summarizer_factories_without_digest_command() -> None:
    captured_kwargs: list[dict[str, object]] = []

    class LegacySummarizer:
        def __init__(self, *, model: str, summary_format: str) -> None:
            captured_kwargs.append({"model": model, "summary_format": summary_format})

        def summarize_archive(self, archive_directory: str | Path) -> SummaryArtifact:
            return SummaryArtifact(path=Path("/tmp/codex-digest.md"), content="# ok")

    artifact = direct_delivery._summarize_archive_with_retries(
        Path("/tmp/archive"),
        codex_model=DEFAULT_CODEX_MODEL,
        summary_format=DEFAULT_SUMMARY_FORMAT,
        digest_command="evening-digest",
        retry_delays_seconds=(),
        summarizer_factory=LegacySummarizer,
        sleep=lambda _seconds: None,
    )

    assert artifact.content == "# ok"
    assert captured_kwargs == [{"model": DEFAULT_CODEX_MODEL, "summary_format": DEFAULT_SUMMARY_FORMAT}]


def test_summarize_archive_with_retries_retries_twice_after_failures() -> None:
    attempts: list[int] = []
    sleeps: list[int] = []

    class FlakySummarizer:
        def summarize_archive(self, archive_directory: str | Path) -> SummaryArtifact:
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                raise RuntimeError("temporary high demand")
            return SummaryArtifact(path=Path("/tmp/codex-digest.md"), content="# ok")

    artifact = direct_delivery._summarize_archive_with_retries(
        Path("/tmp/archive"),
        retry_delays_seconds=(300, 300),
        summarizer_factory=lambda **kwargs: FlakySummarizer(),
        sleep=sleeps.append,
    )

    assert artifact.content == "# ok"
    assert attempts == [1, 2, 3]
    assert sleeps == [300, 300]


def test_summarize_archive_with_retries_raises_after_exhausting_retries() -> None:
    attempts: list[int] = []
    sleeps: list[int] = []

    class AlwaysFailingSummarizer:
        def summarize_archive(self, archive_directory: str | Path) -> SummaryArtifact:
            attempts.append(len(attempts) + 1)
            raise RuntimeError("temporary high demand")

    with pytest.raises(RuntimeError, match="temporary high demand"):
        direct_delivery._summarize_archive_with_retries(
            Path("/tmp/archive"),
            retry_delays_seconds=(300, 300),
            summarizer_factory=lambda **kwargs: AlwaysFailingSummarizer(),
            sleep=sleeps.append,
        )

    assert attempts == [1, 2, 3]
    assert sleeps == [300, 300]


def test_summarize_archive_with_retries_writes_attempt_metadata_for_failures_and_success(tmp_path: Path) -> None:
    attempts: list[int] = []

    class FlakySummarizer:
        def summarize_archive(self, archive_directory: str | Path) -> SummaryArtifact:
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise RuntimeError("temporary high demand")
            return SummaryArtifact(path=Path("/tmp/codex-digest.md"), content="# ok")

    artifact = direct_delivery._summarize_archive_with_retries(
        tmp_path,
        codex_model=DEFAULT_CODEX_MODEL,
        summary_format=DEFAULT_SUMMARY_FORMAT,
        retry_delays_seconds=(0,),
        summarizer_factory=lambda **kwargs: FlakySummarizer(),
        sleep=lambda _seconds: None,
    )

    metadata = json.loads((tmp_path / "metadata" / "codex-attempts.json").read_text())

    assert artifact.content == "# ok"
    assert [entry["attempt"] for entry in metadata["attempts"]] == [1, 2]
    assert metadata["attempts"][0]["status"] == "failed"
    assert metadata["attempts"][0]["error"] == "temporary high demand"
    assert metadata["attempts"][1]["status"] == "succeeded"
    assert metadata["attempts"][1]["error"] is None
    assert metadata["model"] == DEFAULT_CODEX_MODEL
    assert metadata["summary_format"] == DEFAULT_SUMMARY_FORMAT


def test_codex_cli_invocation_raises_timeout_error_when_codex_hangs(monkeypatch, tmp_path: Path) -> None:
    invocation = direct_delivery.CodexCliSummarizer()._invocation

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["codex", "exec"], timeout=123)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="timed out after 123s"):
        invocation.run("prompt", cwd=tmp_path)


def test_summarize_archive_with_retries_ignores_metadata_write_failure_after_success(monkeypatch, tmp_path: Path) -> None:
    attempts: list[int] = []

    class SuccessfulSummarizer:
        def summarize_archive(self, archive_directory: str | Path) -> SummaryArtifact:
            attempts.append(1)
            return SummaryArtifact(path=Path("/tmp/codex-digest.md"), content="# ok")

    monkeypatch.setattr(direct_delivery, "_write_codex_attempt_metadata", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))

    artifact = direct_delivery._summarize_archive_with_retries(
        tmp_path,
        retry_delays_seconds=(),
        summarizer_factory=lambda **kwargs: SuccessfulSummarizer(),
        sleep=lambda _seconds: None,
    )

    assert artifact.content == "# ok"
    assert attempts == [1]


def test_summarize_archive_with_retries_preserves_original_error_when_metadata_write_fails(monkeypatch, tmp_path: Path) -> None:
    class AlwaysFailingSummarizer:
        def summarize_archive(self, archive_directory: str | Path) -> SummaryArtifact:
            raise RuntimeError("temporary high demand")

    monkeypatch.setattr(direct_delivery, "_write_codex_attempt_metadata", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(RuntimeError, match="temporary high demand"):
        direct_delivery._summarize_archive_with_retries(
            tmp_path,
            retry_delays_seconds=(),
            summarizer_factory=lambda **kwargs: AlwaysFailingSummarizer(),
            sleep=lambda _seconds: None,
        )


def test_post_canonical_digest_to_slack_keeps_categorized_digest_in_single_post_when_under_limit(tmp_path: Path) -> None:
    archive_directory = tmp_path / date.today().isoformat()
    digest_path = archive_directory / "summary" / "codex-digest.md"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(
        "☀ *Hermes Pulse Morning Briefing*\n\n"
        "▫ AI\n"
        "- [OpenAI update](https://example.com/ai)\n\n"
        "▫ IT\n"
        "- Redis CVE fixed\n\n"
        "▫ 金融\n"
        "- 日銀見通し\n\n"
        "▫ スケジュール\n"
        "- WWDC keynote\n"
    )
    calls: list[dict[str, object]] = []

    def fake_post_message(
        text: str,
        channel: str,
        thread_ts: str | None = None,
        *,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
        blocks: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        calls.append({"text": text, "channel": channel, "thread_ts": thread_ts, "blocks": blocks})
        return {"ok": True, "channel": channel, "ts": f"1712345.67{len(calls)}"}

    result = direct_delivery.post_canonical_digest_to_slack(
        archive_directory,
        channel="C123",
        post_message=fake_post_message,
        slack_message_limit=3500,
    )

    assert len(calls) == 1
    text = str(calls[0]["text"])
    assert text.startswith("☀ *Hermes Pulse Morning Briefing*")
    assert "▫ AI" in text
    assert "▫ IT" in text
    assert "▫ 金融" in text
    assert "▫ スケジュール" in text
    assert "<https://example.com/ai|OpenAI update>" in text
    assert calls[0]["thread_ts"] is None
    assert result.posted_messages == [call["text"] for call in calls]


def test_post_canonical_digest_to_slack_physically_splits_inside_large_category_before_next_category(tmp_path: Path) -> None:
    archive_directory = tmp_path / date.today().isoformat()
    digest_path = archive_directory / "summary" / "codex-digest.md"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(
        "☀ *Hermes Pulse Morning Briefing*\n\n"
        "▫ AI\n"
        + "\n".join(f"- AI item {index} " + ("x" * 45) for index in range(1, 6))
        + "\n\n▫ カメラ\n- Camera item\n"
    )
    calls: list[dict[str, object]] = []

    def fake_post_message(text: str, channel: str, thread_ts: str | None = None, **kwargs) -> dict[str, object]:
        calls.append({"text": text, "thread_ts": thread_ts})
        return {"ok": True, "channel": channel, "ts": f"1712345.67{len(calls)}"}

    direct_delivery.post_canonical_digest_to_slack(
        archive_directory,
        channel="C123",
        post_message=fake_post_message,
        slack_message_limit=170,
    )

    camera_index = next(index for index, call in enumerate(calls) if "▫ カメラ" in call["text"])
    assert camera_index >= 1
    assert all("▫ カメラ" not in call["text"] for call in calls[:camera_index])
    assert "AI item 1" in calls[0]["text"]


def test_direct_delivery_records_home_cursor_only_after_successful_slack_post(monkeypatch, tmp_path: Path) -> None:
    state_db = tmp_path / "state" / "hermes-pulse.db"
    item = CollectedItem(
        id="x-home:2027277539262235109",
        source="x_home_timeline_reverse_chronological",
        source_kind="post",
        title="New timeline post",
        excerpt="New since the last successful run.",
        url="https://x.com/example/status/2027277539262235109",
        timestamps=ItemTimestamps(created_at="2026-07-14T00:00:00Z"),
        provenance=Provenance(
            provider="x.com",
            acquisition_mode="official_api",
            authority_tier="primary",
            primary_source_url="https://x.com/example/status/2027277539262235109",
            raw_record_id="2027277539262235109",
        ),
    )
    monkeypatch.setattr(
        direct_delivery,
        "_build_digest_with_source_errors",
        lambda command, args: ([item], {}, {"x_likes", "x_home_timeline_reverse_chronological"}),
    )

    def fake_summarize(archive_directory: Path, **_kwargs) -> SummaryArtifact:
        digest_path = archive_directory / "summary" / "codex-digest.md"
        digest_path.parent.mkdir(parents=True, exist_ok=True)
        digest_path.write_text("- New timeline post\n")
        return SummaryArtifact(path=digest_path, content=digest_path.read_text())

    monkeypatch.setattr(direct_delivery, "_summarize_archive_with_retries", fake_summarize)
    args = direct_delivery.build_parser().parse_args(
        [
            "--source-registry",
            str(SOURCE_REGISTRY_PATH),
            "--x-signals",
            "likes,home_timeline_reverse_chronological",
            "--x-expected-username",
            "Y20010920T",
            "--x-likes-reconcile-interval-hours",
            "24",
            "--state-db",
            str(state_db),
            "--archive-root",
            str(tmp_path / "Pulse"),
            "--now",
            "2026-07-14T00:00:00Z",
            "--channel",
            "C123",
        ]
    )

    direct_delivery.run_digest_direct_delivery(
        args,
        post_message=lambda text, channel, thread_ts=None, **kwargs: {
            "ok": True,
            "channel": channel,
            "ts": "1712345.6789",
        },
    )

    records = {record["connector_id"]: record for record in list_connector_cursor_records(state_db)}
    assert records["x_home_timeline_reverse_chronological"]["cursor"] == "2027277539262235109"
    assert records["x_likes_reconcile"]["last_success_at"] == "2026-07-14T00:00:00Z"
    assert "x_likes" not in records


def test_direct_delivery_failure_does_not_commit_x_items_to_source_ledger(tmp_path, monkeypatch) -> None:
    archive_root = tmp_path / "Pulse"
    state_db = tmp_path / "state" / "pulse.db"
    item = CollectedItem(
        id="x_home_timeline_reverse_chronological:2027277539262235109",
        source="x_home_timeline_reverse_chronological",
        source_kind="post",
        title="New post",
        excerpt="New post body",
        url="https://x.com/example/status/2027277539262235109",
        timestamps=ItemTimestamps(created_at="2026-07-14T07:55:00Z"),
        provenance=Provenance(
            provider="x.com",
            acquisition_mode="official_api",
            authority_tier="primary",
            primary_source_url="https://x.com/example/status/2027277539262235109",
            raw_record_id="2027277539262235109",
        ),
    )
    monkeypatch.setattr(
        direct_delivery,
        "_build_digest_with_source_errors",
        lambda command, args: ([item], {}, {"x_likes", "x_home_timeline_reverse_chronological"}),
    )

    def fake_summarize(archive_directory, **kwargs):
        output_path = archive_directory / "summary" / "codex-digest.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("# Digest\n")
        return SummaryArtifact(path=output_path, content="# Digest\n")

    monkeypatch.setattr(direct_delivery, "_summarize_archive_with_retries", fake_summarize)
    args = direct_delivery.build_parser().parse_args(
        [
            "--channel",
            "C123",
            "--archive-root",
            str(archive_root),
            "--x-signals",
            "likes,home_timeline_reverse_chronological",
            "--state-db",
            str(state_db),
            "--now",
            "2026-07-14T08:00:00Z",
        ]
    )

    def fail_post(text, channel, **kwargs):
        raise RuntimeError("Slack unavailable")

    try:
        direct_delivery.run_digest_direct_delivery(args, post_message=fail_post)
    except RuntimeError as exc:
        assert "Slack unavailable" in str(exc)
    else:
        raise AssertionError("expected Slack delivery failure")

    assert not (archive_root / "sources" / "x_home_timeline_reverse_chronological.jsonl").exists()
    assert list_connector_cursor_records(state_db) == []
