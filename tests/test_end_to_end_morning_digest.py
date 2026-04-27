import json
from datetime import date
from pathlib import Path

import hermes_pulse.cli
from hermes_pulse.collection import collect_for_trigger
from hermes_pulse.connectors.feed_registry import FeedRegistryConnector
from hermes_pulse.connectors.hermes_history import HermesHistoryConnector
from hermes_pulse.connectors.known_source_search import KnownSourceSearchConnector
from hermes_pulse.connectors.notes import NotesConnector
from hermes_pulse.archive import write_morning_digest_archive
from hermes_pulse.models import CollectedItem, ItemTimestamps, Provenance, TriggerEvent, TriggerScope
from hermes_pulse.source_registry import load_source_registry
from hermes_pulse.summarization.base import SummaryArtifact
from hermes_pulse.synthesis import synthesize_candidates
from hermes_pulse.trigger_registry import get_trigger_profile


ROOT = Path(__file__).resolve().parents[1]
SOURCE_REGISTRY_PATH = ROOT / "fixtures/source_registry/sample_sources.yaml"
FEED_FIXTURE_PATH = ROOT / "fixtures/feed_samples/official_feed.xml"
SEARCH_FIXTURE_PATH = ROOT / "fixtures/search_samples/known_source_results.html"
HERMES_HISTORY_PATH = ROOT / "fixtures/hermes_history/sample_session.json"
NOTES_PATH = ROOT / "fixtures/notes/sample_notes.md"


def _install_stub_codex_summarizer(monkeypatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    class StubCodexCliSummarizer:
        def summarize_archive(self, archive_directory: str | Path) -> SummaryArtifact:
            archive_directory = Path(archive_directory)
            raw_items = json.loads((archive_directory / "raw" / "collected-items.json").read_text())
            content = "# Codex Digest\n\n" + "".join(f"- {item['title']}\n" for item in raw_items)
            output_path = archive_directory / "summary" / "codex-digest.md"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content)
            calls.append({"archive_directory": archive_directory, "raw_items": raw_items})
            return SummaryArtifact(path=output_path, content=content)

    monkeypatch.setattr(hermes_pulse.cli, "CodexCliSummarizer", StubCodexCliSummarizer)
    return calls


class BoundConnector:
    def __init__(self, collector):
        self._collector = collector

    def collect(self):
        return self._collector()


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


def test_end_to_end_scheduled_morning_digest_runs_against_fixtures(monkeypatch, tmp_path: Path) -> None:
    codex_calls = _install_stub_codex_summarizer(monkeypatch)
    profile = get_trigger_profile("digest.morning.default")
    assert profile.event_type == "digest.morning"
    assert profile.collection_preset == "broad_day_start"

    source_registry = load_source_registry(SOURCE_REGISTRY_PATH)
    assert [entry.id for entry in source_registry] == [
        "official-blog",
        "trusted-secondary-blog",
        "discovery-only-source",
    ]

    trigger = TriggerEvent(
        id="trigger-1",
        type=profile.event_type,
        profile_id=profile.id,
        occurred_at="2026-04-20T08:00:00Z",
        scope=TriggerScope(),
    )
    feed_fixture = FEED_FIXTURE_PATH.read_text()
    search_fixture = SEARCH_FIXTURE_PATH.read_text()
    collected = collect_for_trigger(
        trigger,
        profile,
        {
            "feed_registry": BoundConnector(
                lambda: FeedRegistryConnector(fetcher=lambda url: feed_fixture).collect(source_registry)
            ),
            "known_source_search": BoundConnector(
                lambda: KnownSourceSearchConnector(fetcher=lambda url: search_fixture).collect(source_registry)
            ),
            "hermes_history": BoundConnector(lambda: HermesHistoryConnector().collect(HERMES_HISTORY_PATH)),
            "notes": BoundConnector(lambda: NotesConnector().collect(NOTES_PATH)),
        },
    )

    assert {item.source for item in collected} == {
        "official-blog",
        "trusted-secondary-blog",
        "discovery-only-source",
    }

    candidates = synthesize_candidates(collected)

    assert candidates
    assert candidates[0].score >= candidates[-1].score
    assert any(candidate.item_ids[0].startswith("official-blog:") for candidate in candidates)
    assert any(candidate.item_ids[0].startswith("discovery-only-source:") for candidate in candidates)

    output_path = tmp_path / "deliveries" / "morning-digest.md"

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
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
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    markdown = output_path.read_text()
    assert output_path.exists()
    assert markdown.startswith("# Codex Digest\n")
    assert "Launch update" in markdown
    assert "Discovery scoop" in markdown
    assert "Morning planning" not in markdown
    assert "Notes" not in markdown
    assert codex_calls[0]["archive_directory"].name == date.today().isoformat()
    assert any(item["url"] == "https://example.com/posts/launch-update" for item in codex_calls[0]["raw_items"])


def test_end_to_end_morning_digest_archives_feed_and_local_context_items(
    monkeypatch, tmp_path: Path
) -> None:
    _install_stub_codex_summarizer(monkeypatch)
    output_path = tmp_path / "deliveries" / "morning-digest.md"
    archive_root = tmp_path / "pulse-archive"
    archive_date = date.today().isoformat()

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
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
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    summary_path = archive_root / archive_date / "summary" / "codex-digest.md"
    raw_items_path = archive_root / archive_date / "raw" / "collected-items.json"
    raw_items = json.loads(raw_items_path.read_text())

    assert summary_path.read_text() == output_path.read_text()
    assert not (archive_root / archive_date / "summary" / "morning-digest.md").exists()
    assert {item["source"] for item in raw_items} == {
        "official-blog",
        "trusted-secondary-blog",
        "discovery-only-source",
    }
    assert any(item["id"].startswith("official-blog:") for item in raw_items)


def test_morning_digest_continues_when_x_signals_fail(monkeypatch, tmp_path: Path) -> None:
    _install_stub_codex_summarizer(monkeypatch)
    output_path = tmp_path / "deliveries" / "morning-digest.md"

    class FailingXUrlConnector:
        def collect(self, signal_types):
            raise RuntimeError("x auth missing")

    monkeypatch.setattr(hermes_pulse.cli, "XUrlConnector", FailingXUrlConnector)

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
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
                "--x-signals",
                "bookmarks,likes,home_timeline_reverse_chronological",
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    markdown = output_path.read_text()
    assert "Launch update" in markdown
    assert "Morning planning" not in markdown


def test_morning_digest_can_replay_a_week_window_from_source_ledgers(monkeypatch, tmp_path: Path) -> None:
    codex_calls = _install_stub_codex_summarizer(monkeypatch)
    archive_root = tmp_path / "pulse-archive"
    output_path = tmp_path / "deliveries" / "weekly-digest.md"

    write_morning_digest_archive(
        items=[_archived_item("weekly-source", "carryover", "Weekly carryover", "https://example.com/weekly-carryover")],
        archive_root=archive_root,
        archive_date="2026-04-21",
        retrieved_at="2026-04-21T08:00:00Z",
    )

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
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
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    summary_path = archive_root / "2026-week-17" / "summary" / "codex-digest.md"
    raw_items_path = archive_root / "2026-week-17" / "raw" / "collected-items.json"
    raw_items = json.loads(raw_items_path.read_text())
    markdown = output_path.read_text()

    assert summary_path.read_text() == markdown
    assert {item["title"] for item in raw_items} >= {"Weekly carryover", "Launch update"}
    assert "Weekly carryover" in markdown
    assert "Launch update" in markdown
    assert codex_calls[0]["archive_directory"].name == "2026-week-17"
