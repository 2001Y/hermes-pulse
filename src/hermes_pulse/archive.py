import hashlib
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timezone
from pathlib import Path

from hermes_pulse.models import CitationLink, CollectedItem, IntentSignals, ItemTimestamps, Provenance
from hermes_pulse.summarization.base import RAW_ITEMS_RELATIVE_PATH


DEFAULT_ARCHIVE_ROOT = Path.home() / "Pulse"
logger = logging.getLogger(__name__)


def write_morning_digest_archive(
    items: list[CollectedItem],
    archive_root: str | Path,
    archive_date: str,
    *,
    retrieved_at: str | None = None,
) -> Path:
    archive_root = Path(archive_root)
    archive_directory = archive_root / archive_date
    raw_items_path = archive_directory / RAW_ITEMS_RELATIVE_PATH
    raw_items_path.parent.mkdir(parents=True, exist_ok=True)

    retrieved_at = retrieved_at or _utc_now_isoformat()
    if _use_source_ledgers(archive_root):
        diff_items = _append_source_ledgers(archive_root, items=items, retrieved_at=retrieved_at)
    else:
        diff_items = list(items)
    write_archive_raw_items(archive_directory, diff_items)
    return archive_directory


def write_archive_raw_items(archive_directory: str | Path, items: list[CollectedItem]) -> Path:
    archive_directory = Path(archive_directory)
    raw_items_path = archive_directory / RAW_ITEMS_RELATIVE_PATH
    raw_items_path.parent.mkdir(parents=True, exist_ok=True)
    raw_items_path.write_text(json.dumps([asdict(item) for item in items], indent=2) + "\n")
    return raw_items_path


def load_items_from_source_ledgers(
    archive_root: str | Path,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
) -> list[CollectedItem]:
    archive_root = Path(archive_root)
    source_directory = archive_root / "sources"
    if not source_directory.exists():
        return []
    lower_bound = _parse_window_boundary(window_start, is_end=False)
    upper_bound = _parse_window_boundary(window_end, is_end=True)
    loaded: list[tuple[str, CollectedItem]] = []
    for ledger_path in sorted(source_directory.glob("*.jsonl")):
        for line_number, line in enumerate(ledger_path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("Skipping malformed source ledger line %s:%s: %s", ledger_path, line_number, exc)
                continue
            retrieved_at = payload.get("retrieved_at")
            if not isinstance(retrieved_at, str):
                continue
            retrieved_at_dt = _parse_timestamp(retrieved_at)
            if lower_bound is not None and retrieved_at_dt < lower_bound:
                continue
            if upper_bound is not None and retrieved_at_dt >= upper_bound:
                continue
            loaded.append((retrieved_at, _collected_item_from_payload(payload)))
    loaded.sort(key=lambda entry: (entry[0], entry[1].source, entry[1].id))
    return [item for _, item in loaded]


def _use_source_ledgers(archive_root: Path) -> bool:
    return archive_root != DEFAULT_ARCHIVE_ROOT


def _append_source_ledgers(
    archive_root: Path,
    *,
    items: list[CollectedItem],
    retrieved_at: str,
) -> list[CollectedItem]:
    diff_items: list[CollectedItem] = []
    source_directory = archive_root / "sources"
    source_directory.mkdir(parents=True, exist_ok=True)
    existing_fingerprints_by_source: dict[str, dict[str, str]] = {}

    for item in items:
        ledger_path = source_directory / f"{item.source}.jsonl"
        seen_fingerprints = existing_fingerprints_by_source.setdefault(item.source, _load_existing_fingerprints(ledger_path))
        identity = _item_identity(item)
        fingerprint = _item_fingerprint(item)
        if identity in seen_fingerprints:
            continue
        seen_fingerprints[identity] = fingerprint
        record = asdict(item)
        record["retrieved_at"] = retrieved_at
        record["identity"] = identity
        record["fingerprint"] = fingerprint
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(_serialize_jsonl_record(record) + "\n")
        diff_items.append(item)
    return diff_items


def _load_existing_fingerprints(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    fingerprints: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Skipping malformed source ledger line %s:%s: %s", path, line_number, exc)
            continue
        identity = payload.get("identity") or payload.get("url") or payload.get("id")
        fingerprint = payload.get("fingerprint")
        if isinstance(identity, str) and identity:
            fingerprints[identity] = fingerprint if isinstance(fingerprint, str) else ""
    return fingerprints


def _collected_item_from_payload(payload: dict[str, object]) -> CollectedItem:
    timestamps_payload = payload.get("timestamps")
    timestamps = None
    if isinstance(timestamps_payload, dict):
        timestamps = ItemTimestamps(
            created_at=timestamps_payload.get("created_at"),
            updated_at=timestamps_payload.get("updated_at"),
            start_at=timestamps_payload.get("start_at"),
            end_at=timestamps_payload.get("end_at"),
        )

    intent_payload = payload.get("intent_signals")
    intent_signals = None
    if isinstance(intent_payload, dict):
        intent_signals = IntentSignals(
            saved=bool(intent_payload.get("saved", False)),
            liked=bool(intent_payload.get("liked", False)),
            unread=bool(intent_payload.get("unread", False)),
            unresolved=bool(intent_payload.get("unresolved", False)),
        )

    provenance_payload = payload.get("provenance")
    provenance = None
    if isinstance(provenance_payload, dict):
        provenance = Provenance(
            provider=provenance_payload["provider"],
            acquisition_mode=provenance_payload["acquisition_mode"],
            authority_tier=provenance_payload.get("authority_tier"),
            primary_source_url=provenance_payload.get("primary_source_url"),
            artifact_id=provenance_payload.get("artifact_id"),
            raw_record_id=provenance_payload.get("raw_record_id"),
        )

    citation_chain_payload = payload.get("citation_chain")
    citation_chain: list[CitationLink] = []
    if isinstance(citation_chain_payload, list):
        for entry in citation_chain_payload:
            if not isinstance(entry, dict):
                continue
            citation_chain.append(
                CitationLink(
                    label=entry["label"],
                    url=entry["url"],
                    relation=entry["relation"],
                )
            )

    return CollectedItem(
        id=payload["id"],
        source=payload["source"],
        source_kind=payload["source_kind"],
        title=payload.get("title"),
        excerpt=payload.get("excerpt"),
        body=payload.get("body"),
        url=payload.get("url"),
        people=list(payload.get("people", [])),
        topics=list(payload.get("topics", [])),
        place_refs=list(payload.get("place_refs", [])),
        timestamps=timestamps,
        intent_signals=intent_signals,
        provenance=provenance,
        citation_chain=citation_chain,
        metadata=dict(payload.get("metadata", {})),
    )


def _parse_window_boundary(value: str | None, *, is_end: bool) -> datetime | None:
    if value is None:
        return None
    if len(value) == 10:
        parsed = datetime.fromisoformat(value).replace(tzinfo=UTC)
        return parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    return _parse_timestamp(value)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _item_identity(item: CollectedItem) -> str:
    if item.url:
        return item.url
    if item.provenance is not None and item.provenance.primary_source_url:
        return item.provenance.primary_source_url
    return item.id


def _item_fingerprint(item: CollectedItem) -> str:
    payload = {
        "id": item.id,
        "title": item.title,
        "excerpt": item.excerpt,
        "body": item.body,
        "url": item.url,
        "timestamps": None if item.timestamps is None else asdict(item.timestamps),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _serialize_jsonl_record(record: dict) -> str:
    return (
        json.dumps(record, ensure_ascii=False)
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _utc_now_isoformat() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
