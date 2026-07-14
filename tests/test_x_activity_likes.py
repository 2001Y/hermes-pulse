import json

from hermes_pulse.connectors.x_activity_likes import XActivityLikesConnector


def test_x_activity_likes_collects_only_expected_users_outbound_new_likes(tmp_path) -> None:
    event_log = tmp_path / "x-activity-likes.jsonl"
    events = [
        {
            "data": {
                "event_type": "like.create",
                "event_uuid": "9001",
                "filter": {"user_id": "3102332970", "direction": "outbound"},
                "payload": {
                    "liked_tweet_id": "2027277539262235108",
                    "tweet_author_id": "42",
                    "created_at": "2026-07-14T00:00:00Z",
                },
                "includes": {"users": [{"id": "42", "username": "example"}]},
            }
        },
        {
            "data": {
                "event_type": "like.create",
                "event_uuid": "9002",
                "filter": {"user_id": "3102332970", "direction": "inbound"},
                "payload": {"liked_tweet_id": "2027277539262235109"},
            }
        },
        {
            "data": {
                "event_type": "like.create",
                "event_uuid": "9003",
                "filter": {"user_id": "999", "direction": "outbound"},
                "payload": {"liked_tweet_id": "2027277539262235110"},
            }
        },
    ]
    event_log.write_text("\n".join(json.dumps(event) for event in events) + "\nnot-json\n")
    fetched_urls: list[str] = []

    def fetch_oembed(tweet_url: str) -> dict:
        fetched_urls.append(tweet_url)
        return {
            "author_name": "Example Author",
            "author_url": "https://x.com/example",
            "html": '<blockquote class="twitter-tweet"><p lang="ja">追加でライクした投稿 <a href="https://t.co/a">https://t.co/a</a></p>&mdash; Example Author (@example) <a href="https://x.com/example/status/2027277539262235108">July 14, 2026</a></blockquote>',
        }

    items = XActivityLikesConnector(
        event_log=event_log,
        expected_user_id="3102332970",
        oembed_fetcher=fetch_oembed,
    ).collect()

    assert fetched_urls == ["https://x.com/example/status/2027277539262235108"]
    assert len(items) == 1
    item = items[0]
    assert item.source == "x_likes"
    assert item.url == "https://x.com/example/status/2027277539262235108"
    assert item.title == "追加でライクした投稿 https://t.co/a"
    assert item.excerpt == "追加でライクした投稿 https://t.co/a"
    assert item.intent_signals is not None and item.intent_signals.liked is True
    assert item.provenance is not None and item.provenance.acquisition_mode == "official_api"
    assert item.provenance.raw_record_id == "9001"
    assert item.metadata["liked_tweet_id"] == "2027277539262235108"
