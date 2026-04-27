import json

from hermes_pulse.archive import load_items_from_source_ledgers, write_morning_digest_archive
from hermes_pulse.models import CollectedItem, ItemTimestamps, Provenance


def _item(source: str, item_id: str, url: str) -> CollectedItem:
    return CollectedItem(
        id=f"{source}:{item_id}",
        source=source,
        source_kind="document",
        title=f"Title {item_id}",
        excerpt=f"Excerpt {item_id}",
        url=url,
        timestamps=ItemTimestamps(created_at="2026-04-23T08:00:00Z"),
        provenance=Provenance(
            provider="example.com",
            acquisition_mode="rss_poll",
            authority_tier="primary",
            primary_source_url=url,
            raw_record_id=item_id,
        ),
    )


def test_write_morning_digest_archive_appends_per_source_ledger_and_writes_only_new_diff_items(tmp_path) -> None:
    archive_root = tmp_path / "Pulse"

    write_morning_digest_archive(
        items=[_item("apple-newsroom", "1", "https://example.com/1")],
        archive_root=archive_root,
        archive_date="2026-04-23",
        retrieved_at="2026-04-23T08:00:00Z",
    )
    archive_directory = write_morning_digest_archive(
        items=[
            _item("apple-newsroom", "1", "https://example.com/1"),
            _item("apple-newsroom", "2", "https://example.com/2"),
        ],
        archive_root=archive_root,
        archive_date="2026-04-24",
        retrieved_at="2026-04-24T08:00:00Z",
    )

    raw_items = json.loads((archive_directory / "raw" / "collected-items.json").read_text())
    ledger_path = archive_root / "sources" / "apple-newsroom.jsonl"
    ledger_lines = [json.loads(line) for line in ledger_path.read_text().splitlines()]

    assert [item["url"] for item in raw_items] == ["https://example.com/2"]
    assert [entry["url"] for entry in ledger_lines] == ["https://example.com/1", "https://example.com/2"]
    assert [entry["retrieved_at"] for entry in ledger_lines] == ["2026-04-23T08:00:00Z", "2026-04-24T08:00:00Z"]


def test_write_morning_digest_archive_skips_malformed_existing_ledger_lines(tmp_path) -> None:
    archive_root = tmp_path / "Pulse"
    ledger_path = archive_root / "sources" / "grok_history.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        '{"identity":"https://example.com/good","fingerprint":"abc"}\n'
        '{"identity":"broken"\n'
    )

    archive_directory = write_morning_digest_archive(
        items=[_item("grok_history", "2", "https://example.com/2")],
        archive_root=archive_root,
        archive_date="2026-04-24",
        retrieved_at="2026-04-24T08:00:00Z",
    )

    raw_items = json.loads((archive_directory / "raw" / "collected-items.json").read_text())
    ledger_lines = ledger_path.read_text().splitlines()

    assert [item["url"] for item in raw_items] == ["https://example.com/2"]
    assert len(ledger_lines) == 3
    assert json.loads(ledger_lines[-1])["url"] == "https://example.com/2"


def test_write_morning_digest_archive_escapes_unicode_line_separators_in_jsonl(tmp_path) -> None:
    archive_root = tmp_path / "Pulse"
    item = _item("grok_history", "3", "https://example.com/3")
    item.body = "before\u2028after\u2029done"

    write_morning_digest_archive(
        items=[item],
        archive_root=archive_root,
        archive_date="2026-04-24",
        retrieved_at="2026-04-24T08:00:00Z",
    )

    ledger_path = archive_root / "sources" / "grok_history.jsonl"
    ledger_text = ledger_path.read_text()
    ledger_lines = ledger_text.splitlines()

    assert len(ledger_lines) == 1
    assert "\\u2028" in ledger_text
    assert "\\u2029" in ledger_text
    assert json.loads(ledger_lines[0])["body"] == "before\u2028after\u2029done"


def test_load_items_from_source_ledgers_filters_by_retrieved_at_window(tmp_path) -> None:
    archive_root = tmp_path / "Pulse"

    write_morning_digest_archive(
        items=[_item("apple-newsroom", "1", "https://example.com/1")],
        archive_root=archive_root,
        archive_date="2026-04-21",
        retrieved_at="2026-04-21T08:00:00Z",
    )
    write_morning_digest_archive(
        items=[_item("apple-newsroom", "2", "https://example.com/2")],
        archive_root=archive_root,
        archive_date="2026-04-24",
        retrieved_at="2026-04-24T08:00:00Z",
    )
    write_morning_digest_archive(
        items=[_item("openai-news", "3", "https://example.com/3")],
        archive_root=archive_root,
        archive_date="2026-04-23",
        retrieved_at="2026-04-23T08:00:00Z",
    )

    items = load_items_from_source_ledgers(
        archive_root,
        window_start="2026-04-22",
        window_end="2026-04-24",
    )

    assert [(item.source, item.url) for item in items] == [
        ("openai-news", "https://example.com/3"),
    ]
