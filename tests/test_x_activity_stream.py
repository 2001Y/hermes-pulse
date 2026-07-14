import io
import json

from hermes_pulse.x_activity_stream import append_activity_events, run_activity_stream


def test_append_activity_events_keeps_only_new_expected_outbound_likes(tmp_path) -> None:
    output_path = tmp_path / "x-activity-likes.jsonl"
    existing = {
        "data": {
            "event_type": "like.create",
            "event_uuid": "9001",
            "filter": {"user_id": "3102332970", "direction": "outbound"},
            "payload": {"liked_tweet_id": "101"},
        }
    }
    output_path.write_text(json.dumps(existing) + "\n")
    incoming = [
        json.dumps(existing),
        json.dumps(
            {
                "data": {
                    "event_type": "like.create",
                    "event_uuid": "9002",
                    "filter": {"user_id": "3102332970", "direction": "inbound"},
                    "payload": {"liked_tweet_id": "102"},
                }
            }
        ),
        json.dumps(
            {
                "data": {
                    "event_type": "like.create",
                    "event_uuid": "9003",
                    "filter": {"user_id": "999", "direction": "outbound"},
                    "payload": {"liked_tweet_id": "103"},
                }
            }
        ),
        json.dumps(
            {
                "data": {
                    "event_type": "like.create",
                    "event_uuid": "9004",
                    "filter": {"user_id": "3102332970", "direction": "outbound"},
                    "payload": {"liked_tweet_id": "104"},
                }
            }
        ),
    ]

    appended = append_activity_events(
        incoming,
        output_path=output_path,
        expected_user_id="3102332970",
    )

    stored = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert appended == 1
    assert [entry["data"]["event_uuid"] for entry in stored] == ["9001", "9004"]


def test_run_activity_stream_uses_xurl_app_only_authentication(tmp_path) -> None:
    calls: list[dict[str, object]] = []

    class FakeProcess:
        stdout = io.StringIO("")

        def wait(self) -> int:
            return 0

    def process_factory(arguments, **kwargs):
        calls.append({"arguments": arguments, "kwargs": kwargs})
        return FakeProcess()

    assert (
        run_activity_stream(
            output_path=tmp_path / "x-activity-likes.jsonl",
            expected_user_id="3102332970",
            process_factory=process_factory,
        )
        == 0
    )
    assert calls[0]["arguments"] == [
        "xurl",
        "--auth",
        "app",
        "--stream",
        "https://api.x.com/2/activity/stream",
    ]
