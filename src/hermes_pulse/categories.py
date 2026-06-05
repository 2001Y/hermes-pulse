from collections.abc import Mapping, Sequence
from typing import Literal

DigestCategory = Literal["ai", "it", "finance", "camera", "car", "schedule"]

DIGEST_CATEGORY_ORDER: tuple[DigestCategory, ...] = ("ai", "it", "finance", "camera", "car", "schedule")
DIGEST_CATEGORY_LABELS: dict[DigestCategory, str] = {
    "ai": "AI",
    "it": "IT",
    "finance": "金融",
    "camera": "カメラ",
    "car": "車",
    "schedule": "スケジュール",
}

_CATEGORY_ALIASES: dict[str, DigestCategory] = {
    "ai": "ai",
    "人工知能": "ai",
    "llm": "ai",
    "it": "it",
    "tech": "it",
    "technology": "it",
    "developer": "it",
    "developer-tools": "it",
    "security": "it",
    "finance": "finance",
    "financial": "finance",
    "fintech": "finance",
    "markets": "finance",
    "market": "finance",
    "金融": "finance",
    "camera": "camera",
    "cameras": "camera",
    "photography": "camera",
    "photo": "camera",
    "lenses": "camera",
    "lens": "camera",
    "cine": "camera",
    "cinema": "camera",
    "car": "car",
    "cars": "car",
    "automotive": "car",
    "ev": "car",
    "vehicle": "car",
    "vehicles": "car",
    "schedule": "schedule",
    "calendar": "schedule",
    "event": "schedule",
    "events": "schedule",
    "スケジュール": "schedule",
}

_KEYWORDS: tuple[tuple[DigestCategory, tuple[str, ...]], ...] = (
    ("ai", ("openai", "anthropic", "claude", "gemini", "deepmind", "meta ai", "xai", "x.ai", "gpt", "llm", "langchain", "codex")),
    ("finance", ("nikkei", "bloomberg", "reuters", "market", "markets", "finance", "bank", "boj", "日銀", "金利", "金融", "株", "為替", "円", "ドル")),
    ("camera", ("camera", "cameras", "dpreview", "petapixel", "デジカメ", "lens", "lenses", "leica", "canon", "nikon", "sony alpha", "fujifilm", "sigma", "tamron", "viltrox", "laowa", "ttartisan", "7artisans", "blackmagic", "cine", "cinema")),
    ("car", ("tesla", "electrek", "insideevs", "car-watch", "car watch", "mini", "bmw", "byd", "hyundai", "kia", "nio", "polestar", "automotive", "vehicle", "vehicles", "ev", "自動車", "車", "電気自動車")),
    ("it", ("apple", "microsoft", "google", "software", "cloud", "security", "vulnerability", "cve", "redis", "oracle", "pan-os", "iphone", "ipad", "mac", "ios", "android", "developer")),
)


def category_label(category: str) -> str:
    normalized = normalize_category(category)
    return DIGEST_CATEGORY_LABELS[normalized] if normalized is not None else category


def normalize_category(value: object) -> DigestCategory | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("_", "-")
    if not normalized:
        return None
    if normalized in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[normalized]
    return None


def classify_raw_item(item: Mapping[str, object]) -> DigestCategory:
    metadata = item.get("metadata")
    metadata_mapping = metadata if isinstance(metadata, Mapping) else {}

    explicit = _first_category(
        metadata_mapping.get("item_category"),
        metadata_mapping.get("digest_category"),
        metadata_mapping.get("category"),
    )
    if explicit is not None:
        return explicit

    hinted = _first_category(metadata_mapping.get("category_hint"), item.get("category_hint"))
    if hinted is not None:
        return hinted

    if _has_schedule_timestamp(item):
        return "schedule"

    scoped = _first_category(
        *_as_sequence(metadata_mapping.get("topical_scopes")),
        *_as_sequence(item.get("topics")),
    )
    if scoped is not None:
        return scoped

    haystack = " ".join(
        str(value)
        for value in (
            item.get("source"),
            item.get("source_kind"),
            item.get("title"),
            item.get("excerpt"),
            item.get("body"),
            item.get("url"),
        )
        if value is not None
    ).lower()
    for category, keywords in _KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return category
    return "it"


def group_raw_items_by_category(items: Sequence[Mapping[str, object]]) -> dict[DigestCategory, list[Mapping[str, object]]]:
    grouped: dict[DigestCategory, list[Mapping[str, object]]] = {category: [] for category in DIGEST_CATEGORY_ORDER}
    for item in items:
        grouped[classify_raw_item(item)].append(item)
    return {category: grouped[category] for category in DIGEST_CATEGORY_ORDER if grouped[category]}


def _first_category(*values: object) -> DigestCategory | None:
    for value in values:
        category = normalize_category(value)
        if category is not None:
            return category
    return None


def _as_sequence(value: object) -> list[object]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    return []


def _has_schedule_timestamp(item: Mapping[str, object]) -> bool:
    timestamps = item.get("timestamps")
    if not isinstance(timestamps, Mapping):
        return False
    return bool(timestamps.get("start_at") or timestamps.get("end_at"))
