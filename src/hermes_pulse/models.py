from dataclasses import dataclass, field
from typing import Literal


TriggerFamily = Literal["scheduled", "event", "review"]
OutputMode = Literal[
    "digest",
    "mini_digest",
    "warning",
    "nudge",
    "action_prep",
    "deep_brief",
    "source_audit",
]
SourceAcquisitionMode = Literal[
    "official_api",
    "official_export",
    "rss_poll",
    "atom_poll",
    "known_source_search",
    "manual_import",
    "browser_automation_experimental",
]
CollectedItemAcquisitionMode = Literal[
    "local_store",
    "official_api",
    "official_export",
    "share_link_import",
    "manual_import",
    "browser_automation_experimental",
    "rss_poll",
    "atom_poll",
    "known_source_search",
]
AuthorityTier = Literal["primary", "trusted_secondary", "discovery_only"]
SourceKind = Literal[
    "event",
    "email",
    "conversation",
    "note",
    "post",
    "place",
    "artifact",
    "feed_item",
    "document",
]
CitationRelation = Literal["primary", "secondary", "discussion", "derived"]
CandidateKind = Literal[
    "today",
    "people",
    "incoming",
    "resurface",
    "followup",
    "tomorrow",
    "tonight",
    "warning",
    "action_prep",
    "deep_brief",
    "source_audit",
]
Actionability = Literal["none", "info", "prep", "approval_needed"]


@dataclass(slots=True)
class TimeWindow:
    start: str
    end: str


@dataclass(slots=True)
class PlaceWindow:
    lat: float
    lon: float
    radius_m: float | None = None


@dataclass(slots=True)
class TriggerScope:
    time_window: TimeWindow | None = None
    place_window: PlaceWindow | None = None
    entities: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    source_registry_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TriggerEvent:
    id: str
    type: str
    profile_id: str
    occurred_at: str
    scope: TriggerScope
    evidence_refs: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class TriggerProfile:
    id: str
    family: TriggerFamily
    event_type: str
    collection_preset: str
    output_mode: OutputMode
    action_ceiling: int
    cooldown_minutes: int | None = None
    ranking_weights: dict[str, float] = field(default_factory=dict)
    quotas: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class SourceRegistryEntry:
    id: str
    source_family: str
    domain: str
    title: str
    acquisition_mode: SourceAcquisitionMode
    authority_tier: AuthorityTier
    rss_url: str | None = None
    search_hints: list[str] = field(default_factory=list)
    topical_scopes: list[str] = field(default_factory=list)
    category_hint: str | None = None
    language: str | None = None
    requires_primary_confirmation: bool = False


@dataclass(slots=True)
class ItemTimestamps:
    created_at: str | None = None
    updated_at: str | None = None
    start_at: str | None = None
    end_at: str | None = None


@dataclass(slots=True)
class IntentSignals:
    saved: bool = False
    liked: bool = False
    unread: bool = False
    unresolved: bool = False


@dataclass(slots=True)
class Provenance:
    provider: str
    acquisition_mode: CollectedItemAcquisitionMode
    authority_tier: AuthorityTier | None = None
    primary_source_url: str | None = None
    artifact_id: str | None = None
    raw_record_id: str | None = None


@dataclass(slots=True)
class CitationLink:
    label: str
    url: str
    relation: CitationRelation


@dataclass(slots=True)
class CollectedItem:
    id: str
    source: str
    source_kind: SourceKind
    title: str | None = None
    excerpt: str | None = None
    body: str | None = None
    url: str | None = None
    people: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    place_refs: list[str] = field(default_factory=list)
    timestamps: ItemTimestamps | None = None
    intent_signals: IntentSignals | None = None
    provenance: Provenance | None = None
    citation_chain: list[CitationLink] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class Candidate:
    id: str
    kind: CandidateKind
    item_ids: list[str]
    trigger_relevance: float
    actionability: Actionability
    score: float
    reasons: list[str] = field(default_factory=list)
    suppression_scope: list[str] = field(default_factory=list)
