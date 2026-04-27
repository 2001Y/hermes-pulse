from pathlib import Path

import yaml

from hermes_pulse.models import SourceRegistryEntry


def load_source_registry(path: str | Path) -> list[SourceRegistryEntry]:
    path = Path(path)
    payload = yaml.safe_load(path.read_text()) or {}
    raw_entries = list(_load_included_entries(path, payload))
    raw_entries.extend(payload.get("sources", []))

    only_ids = payload.get("only_ids") or []
    if only_ids:
        allowed = list(only_ids)
        raw_entries = [entry for entry in raw_entries if entry.get("id") in allowed]
        raw_entries.sort(key=lambda entry: allowed.index(entry["id"]))

    deduped_entries: dict[str, dict] = {}
    for entry in raw_entries:
        deduped_entries[entry["id"]] = entry
    ordered_entries = list(deduped_entries.values())
    return [SourceRegistryEntry(**entry) for entry in ordered_entries]


def _load_included_entries(path: Path, payload: dict) -> list[dict]:
    include_value = payload.get("include")
    if include_value is None:
        return []
    include_paths = include_value if isinstance(include_value, list) else [include_value]
    entries: list[dict] = []
    for include_path in include_paths:
        included_payload = yaml.safe_load((path.parent / include_path).read_text()) or {}
        entries.extend(included_payload.get("sources", []))
    return entries
