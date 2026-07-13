import json
import tomllib
from datetime import date
from pathlib import Path

import pytest

import hermes_pulse.cli
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
    browser_args = parser.parse_args(
        [
            "morning-digest",
            "--x-browser-signals",
            "likes,home_timeline_reverse_chronological",
            "--x-browser-profile-root",
            "/Users/akitani/.hermes/browser/x-pulse-profile",
            "--x-browser-profile-directory",
            "Profile 4",
            "--x-browser-handle",
            "Y20010920T",
            "--x-browser-limit",
            "20",
        ]
    )
    assert browser_args.x_browser_signals == "likes,home_timeline_reverse_chronological"
    assert browser_args.x_browser_profile_root == Path("/Users/akitani/.hermes/browser/x-pulse-profile")
    assert browser_args.x_browser_profile_directory == "Profile 4"
    assert browser_args.x_browser_handle == "Y20010920T"
    assert browser_args.x_browser_limit == 20
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

    assert xurl_calls == [{"signal_types": ["bookmarks", "likes"]}]
    assert "Saved launch thread" in output_path.read_text()


def test_morning_digest_collects_x_signals_from_authenticated_browser(monkeypatch, tmp_path: Path) -> None:
    browser_calls: list[dict[str, object]] = []
    _install_stub_codex_summarizer(monkeypatch)

    class FakeXBrowserConnector:
        def __init__(self, **kwargs):
            browser_calls.append({"constructor": kwargs})

        def collect(self, signal_types: list[str]):
            browser_calls.append({"signal_types": signal_types})
            return [
                CollectedItem(
                    id="x-likes:tweet-browser-1",
                    source="x_likes",
                    source_kind="post",
                    title="Browser-collected liked post",
                    excerpt="Collected from the authenticated X web UI.",
                    url="https://x.com/example/status/2",
                    timestamps=ItemTimestamps(created_at="2026-07-14T00:00:00Z"),
                    provenance=Provenance(
                        provider="x.com",
                        acquisition_mode="browser_automation_experimental",
                        authority_tier="primary",
                        primary_source_url="https://x.com/example/status/2",
                        raw_record_id="2",
                    ),
                    citation_chain=[
                        CitationLink(
                            label="Browser-collected liked post",
                            url="https://x.com/example/status/2",
                            relation="primary",
                        )
                    ],
                )
            ]

    monkeypatch.setattr(hermes_pulse.cli, "XBrowserConnector", FakeXBrowserConnector)
    output_path = tmp_path / "deliveries" / "morning-browser-x.md"
    profile_root = tmp_path / "x-browser-profile"

    assert (
        hermes_pulse.cli.main(
            [
                "morning-digest",
                "--source-registry",
                str(SOURCE_REGISTRY_PATH),
                "--x-browser-signals",
                "likes,home_timeline_reverse_chronological",
                "--x-browser-profile-root",
                str(profile_root),
                "--x-browser-profile-directory",
                "Profile 4",
                "--x-browser-handle",
                "Y20010920T",
                "--x-browser-limit",
                "20",
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    assert browser_calls == [
        {
            "constructor": {
                "profile_root": profile_root,
                "profile_directory": "Profile 4",
                "expected_handle": "Y20010920T",
                "limit": 20,
            }
        },
        {"signal_types": ["likes", "home_timeline_reverse_chronological"]},
    ]
    assert "Browser-collected liked post" in output_path.read_text()


def test_refresh_x_browser_profile_command_copies_logged_in_profile_metadata(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    source_root = tmp_path / "Chrome"
    destination_root = tmp_path / "x-pulse-profile"

    def fake_refresh(**kwargs):
        calls.append(kwargs)
        return destination_root / "Profile 4"

    monkeypatch.setattr(hermes_pulse.cli, "refresh_x_browser_profile", fake_refresh)

    assert (
        hermes_pulse.cli.main(
            [
                "refresh-x-browser-profile",
                "--chrome-user-data-dir",
                str(source_root),
                "--chrome-profile-directory",
                "Profile 4",
                "--x-browser-profile-root",
                str(destination_root),
                "--x-browser-profile-directory",
                "Profile 4",
            ]
        )
        == 0
    )
    assert calls == [
        {
            "source_user_data_dir": source_root,
            "source_profile_directory": "Profile 4",
            "destination_user_data_dir": destination_root,
            "destination_profile_directory": "Profile 4",
        }
    ]
