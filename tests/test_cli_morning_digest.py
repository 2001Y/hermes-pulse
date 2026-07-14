import json
import tomllib
from datetime import date
from pathlib import Path

import pytest

import hermes_pulse.cli
from hermes_pulse.db import upsert_connector_cursor
from hermes_pulse.models import CitationLink, CollectedItem, ItemTimestamps, Provenance
from hermes_pulse.summarization.base import SummaryArtifact


ROOT = Path(__file__).resolve().parents[1]
SOURCE_REGISTRY_PATH = ROOT / "fixtures/source_registry/sample_sources.yaml"
HERMES_HISTORY_PATH = ROOT / "fixtures/hermes_history/sample_session.json"
CHATGPT_HISTORY_PATH = ROOT / "fixtures/chatgpt_history/sample_export"
GROK_HISTORY_PATH = ROOT / "fixtures/grok_history/sample_export"
NOTES_PATH = ROOT / "fixtures/notes/sample_notes.md"


@pytest.fixture(autouse=True)
def _stub_default_network_connectors(monkeypatch):
    class EmptyFeedRegistryConnector:
        def __init__(self, fetcher=None) -> None:
            self.fetcher = fetcher

        def collect(self, entries):
            return []

    class EmptyKnownSourceSearchConnector:
        def __init__(self, fetcher=None) -> None:
            self.fetcher = fetcher

        def collect(self, entries):
            return []

    monkeypatch.setattr(hermes_pulse.cli, "FeedRegistryConnector", EmptyFeedRegistryConnector)
    monkeypatch.setattr(hermes_pulse.cli, "KnownSourceSearchConnector", EmptyKnownSourceSearchConnector)


def _install_stub_codex_summarizer(monkeypatch, template: str | None = None) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    class StubCodexCliSummarizer:
        def summarize_archive(self, archive_directory: str | Path) -> SummaryArtifact:
            archive_directory = Path(archive_directory)
            raw_items = json.loads((archive_directory / "raw" / "collected-items.json").read_text())
            content = template or "# Codex Digest\n\n" + "".join(
                f"- {item['title']}\n" for item in raw_items
            )
            output_path = archive_directory / "summary" / "codex-digest.md"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content)
            calls.append(
                {
                    "archive_directory": archive_directory,
                    "raw_items": raw_items,
                    "content": content,
                }
            )
            return SummaryArtifact(path=output_path, content=content)

    monkeypatch.setattr(hermes_pulse.cli, "CodexCliSummarizer", StubCodexCliSummarizer)
    return calls


def test_main_entrypoint_exists_and_exits_successfully() -> None:
    assert hermes_pulse.cli.main([]) == 0


def test_main_supports_evening_digest_command(monkeypatch, tmp_path: Path) -> None:
    codex_calls = _install_stub_codex_summarizer(monkeypatch, template="# Codex Digest\n\n- Evening summary\n")
    output_path = tmp_path / "deliveries" / "evening-digest.md"

    assert (
        hermes_pulse.cli.main(
            [
                "evening-digest",
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--chatgpt-history",
                str(CHATGPT_HISTORY_PATH),
                "--grok-history",
                str(GROK_HISTORY_PATH),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    assert output_path.read_text() == "# Codex Digest\n\n- Evening summary\n"
    assert codex_calls[0]["archive_directory"].exists()


def test_main_with_output_only_does_not_write_fallback_digest(tmp_path: Path) -> None:
    output_path = tmp_path / "deliveries" / "morning-digest.md"

    assert hermes_pulse.cli.main(["--output", str(output_path)]) == 0
    assert not output_path.exists()


def test_morning_digest_uses_live_feed_fetching_when_no_fixture_is_provided(
    monkeypatch, tmp_path: Path
) -> None:
    fetchers: list[object] = []
    codex_calls = _install_stub_codex_summarizer(monkeypatch)

    class FakeFeedRegistryConnector:
        def __init__(self, fetcher=None) -> None:
            fetchers.append(fetcher)

        def collect(self, entries):
            assert [entry.id for entry in entries if entry.rss_url] == [
                "official-blog",
                "trusted-secondary-blog",
            ]
            return [
                CollectedItem(
                    id="official-blog:live-fetch-item",
                    source="official-blog",
                    source_kind="feed_item",
                    title="Live fetch item",
                    excerpt="Fetched from registry URL.",
                    url="https://example.com/posts/live-fetch-item",
                    timestamps=ItemTimestamps(created_at="Mon, 20 Apr 2026 08:00:00 GMT"),
                    provenance=Provenance(
                        provider="example.com",
                        acquisition_mode="rss_poll",
                        authority_tier="primary",
                        primary_source_url="https://example.com/posts/live-fetch-item",
                        raw_record_id="live-fetch-item",
                    ),
                    citation_chain=[
                        CitationLink(
                            label="Live fetch item",
                            url="https://example.com/posts/live-fetch-item",
                            relation="primary",
                        )
                    ],
                )
            ]

    class EmptyKnownSourceSearchConnector:
        def __init__(self, fetcher=None) -> None:
            self.fetcher = fetcher

        def collect(self, entries):
            return []

    monkeypatch.setattr(hermes_pulse.cli, "FeedRegistryConnector", FakeFeedRegistryConnector)
    monkeypatch.setattr(hermes_pulse.cli, "KnownSourceSearchConnector", EmptyKnownSourceSearchConnector)
    output_path = tmp_path / "deliveries" / "morning-digest.md"

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--chatgpt-history",
                str(CHATGPT_HISTORY_PATH),
                "--grok-history",
                str(GROK_HISTORY_PATH),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    assert fetchers == [None]
    assert "Live fetch item" in output_path.read_text()
    assert codex_calls[0]["raw_items"][0]["title"] == "Live fetch item"


def test_morning_digest_skips_optional_local_context_when_paths_are_omitted(
    monkeypatch, tmp_path: Path
) -> None:
    fetchers: list[object] = []
    _install_stub_codex_summarizer(monkeypatch)

    class FakeFeedRegistryConnector:
        def __init__(self, fetcher=None) -> None:
            fetchers.append(fetcher)

        def collect(self, entries):
            assert [entry.id for entry in entries if entry.rss_url] == [
                "official-blog",
                "trusted-secondary-blog",
            ]
            return [
                CollectedItem(
                    id="official-blog:live-only-item",
                    source="official-blog",
                    source_kind="feed_item",
                    title="Live only item",
                    excerpt="Fetched without local context.",
                    url="https://example.com/posts/live-only-item",
                    timestamps=ItemTimestamps(created_at="Mon, 20 Apr 2026 08:00:00 GMT"),
                    provenance=Provenance(
                        provider="example.com",
                        acquisition_mode="rss_poll",
                        authority_tier="primary",
                        primary_source_url="https://example.com/posts/live-only-item",
                        raw_record_id="live-only-item",
                    ),
                    citation_chain=[
                        CitationLink(
                            label="Live only item",
                            url="https://example.com/posts/live-only-item",
                            relation="primary",
                        )
                    ],
                )
            ]

    class UnexpectedHermesHistoryConnector:
        def collect(self, path):
            raise AssertionError("hermes history connector should not be used")

    class UnexpectedNotesConnector:
        def collect(self, path):
            raise AssertionError("notes connector should not be used")

    class EmptyKnownSourceSearchConnector:
        def __init__(self, fetcher=None) -> None:
            self.fetcher = fetcher

        def collect(self, entries):
            return []

    monkeypatch.setattr(hermes_pulse.cli, "FeedRegistryConnector", FakeFeedRegistryConnector)
    monkeypatch.setattr(hermes_pulse.cli, "KnownSourceSearchConnector", EmptyKnownSourceSearchConnector)
    monkeypatch.setattr(hermes_pulse.cli, "HermesHistoryConnector", UnexpectedHermesHistoryConnector)
    monkeypatch.setattr(hermes_pulse.cli, "NotesConnector", UnexpectedNotesConnector)
    output_path = tmp_path / "deliveries" / "morning-digest.md"

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    assert fetchers == [None]
    assert "Live only item" in output_path.read_text()


def test_morning_digest_archives_raw_items_before_invoking_codex_and_delivers_canonical_digest(
    monkeypatch, tmp_path: Path
) -> None:
    codex_calls = _install_stub_codex_summarizer(monkeypatch, template="# Codex Digest\n\n- Canonical summary\n")
    output_path = tmp_path / "deliveries" / "morning-digest.md"
    archive_root = tmp_path / "archive-root"
    archive_date = date.today().isoformat()

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--chatgpt-history",
                str(CHATGPT_HISTORY_PATH),
                "--grok-history",
                str(GROK_HISTORY_PATH),
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

    assert codex_calls == [
        {
            "archive_directory": archive_root / archive_date,
            "raw_items": json.loads(raw_items_path.read_text()),
            "content": "# Codex Digest\n\n- Canonical summary\n",
        }
    ]
    assert summary_path.exists()
    assert summary_path.read_text() == output_path.read_text()
    assert not (archive_root / archive_date / "summary" / "morning-digest.md").exists()
    raw_items = json.loads(raw_items_path.read_text())
    assert {item["source"] for item in raw_items} == {"chatgpt_history", "grok_history"}
    assert len(raw_items) == 2


def test_morning_digest_defaults_archive_root_to_home_pulse_directory(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(hermes_pulse.cli.Path, "home", classmethod(lambda cls: tmp_path))
    _install_stub_codex_summarizer(monkeypatch)
    archive_date = date.today().isoformat()

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--chatgpt-history",
                str(CHATGPT_HISTORY_PATH),
                "--grok-history",
                str(GROK_HISTORY_PATH),
            ]
        )
        == 0
    )

    summary_path = tmp_path / "Pulse" / archive_date / "summary" / "codex-digest.md"
    raw_items_path = tmp_path / "Pulse" / archive_date / "raw" / "collected-items.json"

    assert summary_path.exists()
    assert summary_path.read_text().startswith("# Codex Digest\n")
    raw_items = json.loads(raw_items_path.read_text())
    assert len(raw_items) == 2
    assert {item["source"] for item in raw_items} == {"chatgpt_history", "grok_history"}


def test_pyproject_declares_console_entrypoint() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert pyproject["project"]["name"] == "hermes-pulse"
    assert pyproject["project"]["scripts"]["hermes-pulse"] == "hermes_pulse.cli:main"
    assert pyproject["project"]["scripts"]["hermes-pulse-direct-delivery"] == "hermes_pulse.direct_delivery:main"


def test_cli_parser_uses_hermes_pulse_program_name() -> None:
    parser = hermes_pulse.cli.build_parser()

    assert parser.prog == "hermes-pulse"
    assert parser.parse_args(["morning-digest", "--x-signals", "bookmarks,likes"]).x_signals == "bookmarks,likes"
    assert parser.parse_args(["refresh-grok-history", "--output-dir", str(tmp_path := ROOT)]).command == "refresh-grok-history"
    assert parser.parse_args(["refresh-chatgpt-history", "--input-dir", str(tmp_path)]).command == "refresh-chatgpt-history"
    assert parser.parse_args(["morning-digest", "--chatgpt-history", str(CHATGPT_HISTORY_PATH)]).chatgpt_history == CHATGPT_HISTORY_PATH
    assert parser.parse_args(["morning-digest", "--grok-history", str(GROK_HISTORY_PATH)]).grok_history == GROK_HISTORY_PATH


def test_morning_digest_collects_configured_grok_history(monkeypatch, tmp_path: Path) -> None:
    _install_stub_codex_summarizer(monkeypatch)
    output_path = tmp_path / "deliveries" / "morning-digest.md"

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--grok-history",
                str(GROK_HISTORY_PATH),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    assert "定期券の経路相談" in output_path.read_text()


def test_morning_digest_collects_configured_chatgpt_history(monkeypatch, tmp_path: Path) -> None:
    _install_stub_codex_summarizer(monkeypatch)
    output_path = tmp_path / "deliveries" / "morning-digest.md"

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--chatgpt-history",
                str(CHATGPT_HISTORY_PATH),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    assert "旅行計画の相談" in output_path.read_text()


def test_morning_digest_collects_configured_x_signals(monkeypatch, tmp_path: Path) -> None:
    xurl_calls: list[dict[str, object]] = []
    _install_stub_codex_summarizer(monkeypatch)

    class FakeXConnector:
        def collect(self, signal_types: list[str]):
            xurl_calls.append({"signal_types": signal_types})
            return [
                CollectedItem(
                    id="x-bookmarks:tweet-1",
                    source="x_bookmarks",
                    source_kind="post",
                    title="Saved launch thread",
                    excerpt="A saved X post.",
                    url="https://x.com/example/status/1",
                    timestamps=ItemTimestamps(created_at="2026-04-20T08:00:00Z"),
                    provenance=Provenance(
                        provider="x.com",
                        acquisition_mode="official_api",
                        authority_tier="primary",
                        primary_source_url="https://x.com/example/status/1",
                        raw_record_id="1",
                    ),
                    citation_chain=[
                        CitationLink(
                            label="Saved launch thread",
                            url="https://x.com/example/status/1",
                            relation="primary",
                        )
                    ],
                )
            ]

    monkeypatch.setattr(hermes_pulse.cli, "XUrlConnector", FakeXConnector)
    output_path = tmp_path / "deliveries" / "morning-digest.md"

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--x-signals",
                "bookmarks,likes",
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    assert xurl_calls == [{"signal_types": ["bookmarks"]}, {"signal_types": ["likes"]}]
    assert "Saved launch thread" in output_path.read_text()


def test_morning_digest_passes_saved_home_cursor_and_expected_identity_to_x_connector(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    _install_stub_codex_summarizer(monkeypatch)
    state_db = tmp_path / "state" / "hermes-pulse.db"
    upsert_connector_cursor(
        state_db,
        connector_id="x_home_timeline_reverse_chronological",
        cursor="2027277539262235108",
        last_poll_at="2026-07-13T22:00:00Z",
        last_success_at="2026-07-13T22:00:00Z",
    )

    class FakeXConnector:
        def __init__(self, *, expected_username: str | None = None) -> None:
            calls.append({"expected_username": expected_username})

        def collect(self, signal_types: list[str], *, since_ids: dict[str, str] | None = None):
            calls.append({"signal_types": signal_types, "since_ids": since_ids})
            return [
                CollectedItem(
                    id="x-home:2027277539262235109",
                    source="x_home_timeline_reverse_chronological",
                    source_kind="post",
                    title="New timeline post",
                    excerpt="Only the increment is collected.",
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
            ]

    monkeypatch.setattr(hermes_pulse.cli, "XUrlConnector", FakeXConnector)

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--x-signals",
                "likes,home_timeline_reverse_chronological",
                "--x-expected-username",
                "Y20010920T",
                "--state-db",
                str(state_db),
                "--archive-root",
                str(tmp_path / "Pulse"),
                "--output",
                str(tmp_path / "digest.md"),
            ]
        )
        == 0
    )

    assert calls == [
        {"expected_username": "Y20010920T"},
        {"signal_types": ["likes"], "since_ids": None},
        {
            "signal_types": ["home_timeline_reverse_chronological"],
            "since_ids": {"home_timeline_reverse_chronological": "2027277539262235108"},
        },
    ]


def test_morning_digest_collects_x_activity_likes_without_browser_state(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    _install_stub_codex_summarizer(monkeypatch)
    activity_log = tmp_path / "x-activity-likes.jsonl"
    activity_log.write_text("")

    class FakeActivityLikesConnector:
        def __init__(self, *, event_log, expected_user_id, error_handler=None) -> None:
            calls.append(
                {
                    "event_log": event_log,
                    "expected_user_id": expected_user_id,
                    "has_error_handler": callable(error_handler),
                }
            )

        def collect(self):
            return [
                CollectedItem(
                    id="x_likes:9001",
                    source="x_likes",
                    source_kind="post",
                    title="New activity like",
                    excerpt="A new like from XAA.",
                    url="https://x.com/example/status/2027277539262235108",
                    timestamps=ItemTimestamps(created_at="2026-07-14T00:00:00Z"),
                    provenance=Provenance(
                        provider="x.com",
                        acquisition_mode="official_api",
                        authority_tier="primary",
                        primary_source_url="https://x.com/example/status/2027277539262235108",
                        raw_record_id="9001",
                    ),
                )
            ]

    monkeypatch.setattr(hermes_pulse.cli, "XActivityLikesConnector", FakeActivityLikesConnector)

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--x-activity-likes-file",
                str(activity_log),
                "--x-expected-user-id",
                "3102332970",
                "--archive-root",
                str(tmp_path / "Pulse"),
                "--output",
                str(tmp_path / "digest.md"),
            ]
        )
        == 0
    )

    assert calls == [
        {
            "event_log": activity_log,
            "expected_user_id": "3102332970",
            "has_error_handler": True,
        }
    ]


def test_morning_digest_skips_full_likes_reconcile_until_24_hours_elapsed(monkeypatch, tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    upsert_connector_cursor(
        state_db,
        connector_id="x_likes_reconcile",
        cursor=None,
        last_poll_at="2026-07-14T00:00:00Z",
        last_success_at="2026-07-14T00:00:00Z",
        last_error=None,
    )
    calls: list[list[str]] = []
    _install_stub_codex_summarizer(monkeypatch)

    class FakeXConnector:
        def __init__(self, *, expected_username=None) -> None:
            pass

        def collect(self, signal_types, *, since_ids=None):
            calls.append(list(signal_types))
            return []

    monkeypatch.setattr(hermes_pulse.cli, "XUrlConnector", FakeXConnector)

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--state-db",
                str(state_db),
                "--x-signals",
                "likes,home_timeline_reverse_chronological",
                "--x-likes-reconcile-interval-hours",
                "24",
                "--x-expected-username",
                "Y20010920T",
                "--now",
                "2026-07-14T06:00:00Z",
                "--archive-root",
                str(tmp_path / "Pulse"),
                "--output",
                str(tmp_path / "digest.md"),
            ]
        )
        == 0
    )

    assert calls == [["home_timeline_reverse_chronological"]]


def test_x_signal_failure_is_isolated_per_signal(monkeypatch) -> None:
    timeline_item = CollectedItem(
        id="x_home_timeline_reverse_chronological:9002",
        source="x_home_timeline_reverse_chronological",
        source_kind="post",
        title="Timeline update",
        excerpt="Timeline body",
        url="https://x.com/example/status/9002",
        timestamps=ItemTimestamps(created_at="2026-07-14T08:00:00Z"),
        provenance=Provenance(
            provider="x.com",
            acquisition_mode="official_api",
            authority_tier="primary",
            primary_source_url="https://x.com/example/status/9002",
            raw_record_id="9002",
        ),
    )

    class FakeXUrlConnector:
        def __init__(self, expected_username=None) -> None:
            assert expected_username == "Y20010920T"

        def collect(self, signal_types, *, since_ids=None):
            if signal_types == ["likes"]:
                raise RuntimeError("likes unavailable")
            assert signal_types == ["home_timeline_reverse_chronological"]
            assert since_ids == {"home_timeline_reverse_chronological": "9001"}
            return [timeline_item]

    monkeypatch.setattr(hermes_pulse.cli, "XUrlConnector", FakeXUrlConnector)
    source_errors: dict[str, str] = {}
    successful_sources: set[str] = set()

    items = hermes_pulse.cli._collect_x_signals_with_error_capture(
        ["likes", "home_timeline_reverse_chronological"],
        expected_username="Y20010920T",
        since_ids={"home_timeline_reverse_chronological": "9001"},
        source_errors=source_errors,
        successful_sources=successful_sources,
    )

    assert items == [timeline_item]
    assert source_errors == {"x_likes": "likes unavailable"}
    assert successful_sources == {"x_home_timeline_reverse_chronological"}
