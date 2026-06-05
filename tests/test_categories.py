from hermes_pulse.categories import classify_raw_item, group_raw_items_by_category


def test_classify_raw_item_prefers_explicit_item_category_over_source_hint() -> None:
    item = {
        "source": "generic-google-news",
        "title": "Sony announces new full-frame camera",
        "metadata": {
            "category_hint": "it",
            "item_category": "camera",
        },
    }

    assert classify_raw_item(item) == "camera"


def test_group_raw_items_by_category_uses_source_hints_topics_timestamps_and_keywords() -> None:
    items = [
        {
            "source": "google-news-ai",
            "title": "OpenAI launches agent runtime",
            "metadata": {"category_hint": "ai"},
        },
        {
            "source": "nikkei-markets",
            "title": "日銀の金利見通し",
            "metadata": {"topical_scopes": ["markets"]},
        },
        {
            "source": "calendar",
            "title": "WWDC keynote",
            "timestamps": {"start_at": "2026-06-08T10:00:00Z"},
        },
        {
            "source": "car-watch",
            "title": "MINI EV software update",
        },
    ]

    grouped = group_raw_items_by_category(items)

    assert [item["title"] for item in grouped["ai"]] == ["OpenAI launches agent runtime"]
    assert [item["title"] for item in grouped["finance"]] == ["日銀の金利見通し"]
    assert [item["title"] for item in grouped["schedule"]] == ["WWDC keynote"]
    assert [item["title"] for item in grouped["car"]] == ["MINI EV software update"]
