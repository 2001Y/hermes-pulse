import json
from pathlib import Path

import pytest

from hermes_pulse.summarization.base import SummaryArtifact
from hermes_pulse.summarization.codex_cli import CodexCliSummarizer, _chunk_items


class StubCodexInvocation:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def run(self, prompt: str, *, cwd: Path) -> str:
        self.calls.append({
            "prompt": prompt,
            "cwd": cwd,
            "raw_items_exists": (cwd / "raw" / "collected-items.json").exists(),
        })
        return self.response


def test_codex_cli_summarizer_builds_grounded_prompt_and_writes_canonical_digest(tmp_path: Path) -> None:
    archive_directory = tmp_path / "2026-04-20"
    raw_directory = archive_directory / "raw"
    summary_directory = archive_directory / "summary"
    raw_directory.mkdir(parents=True)
    summary_directory.mkdir(parents=True)

    raw_items = [
        {
            "id": "official-blog:launch",
            "source": "official-blog",
            "title": "Launch update",
            "excerpt": "Version 1.0 ships today.",
            "url": "https://example.com/posts/launch-update",
        }
    ]
    (raw_directory / "collected-items.json").write_text(json.dumps(raw_items, indent=2) + "\n")
    (summary_directory / "morning-digest.md").write_text(
        "# Morning Digest\n\n- [Launch update](https://example.com/posts/launch-update)\n"
    )

    invocation = StubCodexInvocation("# Codex Digest\n\n- [日本語の要約](https://example.com/posts/launch-update)\n")
    summarizer = CodexCliSummarizer(invocation=invocation)

    artifact = summarizer.summarize_archive(archive_directory)

    assert artifact == SummaryArtifact(
        path=archive_directory / "summary" / "codex-digest.md",
        content="# Codex Digest\n\n- [日本語の要約](https://example.com/posts/launch-update)\n",
    )
    assert artifact.path.exists()
    assert artifact.path.read_text() == artifact.content
    assert len(invocation.calls) == 2
    assert invocation.calls[0]["cwd"] != archive_directory
    assert invocation.calls[0]["raw_items_exists"] is False
    assert invocation.calls[1]["cwd"] != archive_directory
    assert invocation.calls[1]["raw_items_exists"] is False

    prompt = invocation.calls[0]["prompt"]
    assert isinstance(prompt, str)
    assert "日本語" in prompt
    assert "一次情報" in prompt
    assert "リンク" in prompt
    assert "https://example.com/posts/launch-update" in prompt
    assert '"title": "Launch update"' in prompt
    assert '"official-blog:launch"' not in prompt
    assert '"source"' not in prompt
    assert "raw/collected-items.json" not in prompt
    assert str(archive_directory) not in prompt
    assert "# Morning Digest" not in prompt


def test_codex_cli_summarizer_compacts_large_raw_items_before_prompting(tmp_path: Path) -> None:
    archive_directory = tmp_path / "2026-04-20"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)

    huge_excerpt = "x" * 5000
    raw_items = [
        {
            "id": "apple-newsroom:launch",
            "source": "apple-newsroom",
            "source_kind": "feed_item",
            "title": "Launch update",
            "excerpt": huge_excerpt,
            "body": huge_excerpt,
            "url": "https://example.com/posts/launch-update",
            "timestamps": {"created_at": "2026-04-20T07:00:00Z"},
            "provenance": {
                "provider": "example.com",
                "acquisition_mode": "rss_poll",
                "authority_tier": "primary",
                "primary_source_url": "https://example.com/posts/launch-update",
            },
        }
    ]
    (raw_directory / "collected-items.json").write_text(json.dumps(raw_items, indent=2) + "\n")

    invocation = StubCodexInvocation("# Codex Digest\n\n- [Launch update](https://example.com/posts/launch-update)\n")
    summarizer = CodexCliSummarizer(invocation=invocation)

    summarizer.summarize_archive(archive_directory)

    prompt = invocation.calls[0]["prompt"]
    assert isinstance(prompt, str)
    assert "https://example.com/posts/launch-update" in prompt
    assert '"title": "Launch update"' in prompt
    assert len(prompt) < 5000
    assert huge_excerpt not in prompt


def test_codex_cli_summarizer_splits_large_archives_into_chunk_prompts_and_merges_results(tmp_path: Path) -> None:
    archive_directory = tmp_path / "2026-04-20"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)

    raw_items = [
        {
            "id": f"item-{index}",
            "source": "official-blog",
            "source_kind": "feed_item",
            "title": f"Title {index}",
            "excerpt": f"Excerpt {index}",
            "url": f"https://example.com/{index}",
        }
        for index in range(250)
    ]
    (raw_directory / "collected-items.json").write_text(json.dumps(raw_items, indent=2) + "\n")

    class MultiResponseInvocation:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def run(self, prompt: str, *, cwd: Path) -> str:
            self.calls.append({"prompt": prompt, "cwd": cwd})
            if len(self.calls) == 1:
                return "chunk-one [Title 0](https://example.com/0)"
            if len(self.calls) == 2:
                return "chunk-two [Title 50](https://example.com/50)"
            if len(self.calls) == 3:
                return "chunk-three [Title 100](https://example.com/100)"
            if len(self.calls) == 4:
                return "chunk-four [Title 150](https://example.com/150)"
            if len(self.calls) == 5:
                return "chunk-five [Title 200](https://example.com/200)"
            return "merged-final [Title 0](https://example.com/0)"

    invocation = MultiResponseInvocation()
    summarizer = CodexCliSummarizer(invocation=invocation)

    artifact = summarizer.summarize_archive(archive_directory)

    assert artifact.content == "merged-final [Title 0](https://example.com/0)"
    assert len(invocation.calls) == 6
    assert "chunk-one" in invocation.calls[5]["prompt"]
    assert "chunk-two" in invocation.calls[5]["prompt"]
    assert '"title": "Title 49"' in invocation.calls[0]["prompt"]
    assert '"title": "Title 50"' not in invocation.calls[0]["prompt"]
    assert '"title": "Title 249"' not in invocation.calls[0]["prompt"]


def test_codex_cli_summarizer_uses_smaller_chunk_size_for_large_archives(tmp_path: Path) -> None:
    archive_directory = tmp_path / "2026-04-20"
    raw_directory = archive_directory / "raw"
    raw_directory.mkdir(parents=True)

    raw_items = [
        {
            "id": f"item-{index}",
            "source": "official-blog",
            "source_kind": "feed_item",
            "title": f"Title {index}",
            "excerpt": f"Excerpt {index}",
            "url": f"https://example.com/{index}",
        }
        for index in range(120)
    ]
    (raw_directory / "collected-items.json").write_text(json.dumps(raw_items, indent=2) + "\n")

    class MultiResponseInvocation:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def run(self, prompt: str, *, cwd: Path) -> str:
            self.calls.append({"prompt": prompt, "cwd": cwd})
            if len(self.calls) == 1:
                return "response-1 [Title 0](https://example.com/0)"
            if len(self.calls) == 2:
                return "response-2 [Title 40](https://example.com/40)"
            if len(self.calls) == 3:
                return "response-3 [Title 80](https://example.com/80)"
            return "response-4 [Title 0](https://example.com/0)"

    invocation = MultiResponseInvocation()
    summarizer = CodexCliSummarizer(invocation=invocation)

    artifact = summarizer.summarize_archive(archive_directory)

    assert artifact.partial_contents == [
        "response-1 [Title 0](https://example.com/0)",
        "response-2 [Title 40](https://example.com/40)",
        "response-3 [Title 80](https://example.com/80)",
    ]
    assert artifact.content == "response-4 [Title 0](https://example.com/0)"
    assert len(invocation.calls) == 4
    assert '"title": "Title 39"' in invocation.calls[0]["prompt"]
    assert '"title": "Title 40"' not in invocation.calls[0]["prompt"]
    assert '"title": "Title 40"' in invocation.calls[1]["prompt"]
    assert '"title": "Title 79"' in invocation.calls[1]["prompt"]
    assert '"title": "Title 80"' not in invocation.calls[1]["prompt"]


def test_chunk_items_balances_tail_for_evenly_sized_items() -> None:
    items = [{"id": f"item-{index}", "title": f"Title {index}", "excerpt": "x" * 20, "body": None, "url": f"https://example.com/{index}"} for index in range(101)]

    chunks = _chunk_items(items, 50)

    assert [len(chunk) for chunk in chunks] == [34, 34, 33]


def test_codex_cli_summarizer_requires_raw_collected_items_json(tmp_path: Path) -> None:
    archive_directory = tmp_path / "2026-04-20"
    archive_directory.mkdir(parents=True)

    summarizer = CodexCliSummarizer(invocation=StubCodexInvocation("unused"))

    with pytest.raises(FileNotFoundError):
        summarizer.summarize_archive(archive_directory)
