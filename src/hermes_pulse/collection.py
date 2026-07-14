from collections.abc import Mapping
from typing import Any

from hermes_pulse.models import TriggerEvent, TriggerProfile


COLLECTION_PRESETS = {
    "broad_day_start": [
        "feed_registry",
        "known_source_search",
        "x_signals",
        "x_activity_likes",
        "google_calendar",
        "gmail",
        "chatgpt_history",
        "grok_history",
    ],
    "broad_day_end": [
        "feed_registry",
        "known_source_search",
        "x_signals",
        "x_activity_likes",
        "google_calendar",
        "gmail",
        "chatgpt_history",
        "grok_history",
    ],
    "calendar_leave_now": ["google_calendar"],
    "mail_operational": ["gmail"],
    "shopping_replenishment": ["notes"],
    "known_source_delta": ["feed_registry", "known_source_search"],
    "known_source_deep_dive": ["feed_registry", "known_source_search"],
    "location_arrival": ["location_context"],
    "location_walk": ["location_context"],
    "calendar_gap_window": ["google_calendar"],
    "trigger_quality_audit": ["audit_context"],
}


def collect_for_trigger(
    trigger: TriggerEvent,
    profile: TriggerProfile,
    connectors: Mapping[str, Any],
) -> list[Any]:
    collected: list[Any] = []
    for connector_id in COLLECTION_PRESETS.get(profile.collection_preset, []):
        connector = connectors.get(connector_id)
        if connector is None:
            continue
        collected.extend(connector.collect())
    return collected
