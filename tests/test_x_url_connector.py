import subprocess

from hermes_pulse.connectors.x_url import XUrlConnector, _run_xurl_json


def test_xurl_connector_collects_bookmarks_likes_and_reverse_chronological_home_timeline() -> None:
    responses = {
        "/2/users/me": {
            "data": {"id": "42", "username": "akita"},
        },
        "/2/users/42/bookmarks?max_results=100&tweet.fields=created_at,author_id,text,entities": {
            "data": [
                {
                    "id": "100",
                    "text": "Saved launch thread",
                    "created_at": "2026-04-20T08:00:00Z",
                    "author_id": "7",
                    "entities": {
                        "urls": [
                            {
                                "expanded_url": "https://example.com/launch",
                            }
                        ]
                    },
                }
            ],
            "includes": {"users": [{"id": "7", "username": "openai"}]},
        },
        "/2/users/42/liked_tweets?max_results=100&tweet.fields=created_at,author_id,text,entities": {
            "data": [
                {
                    "id": "101",
                    "text": "Liked benchmark result",
                    "created_at": "2026-04-20T09:00:00Z",
                    "author_id": "8",
                }
            ],
            "includes": {"users": [{"id": "8", "username": "anthropic"}]},
        },
        "/2/users/42/timelines/reverse_chronological?max_results=100&tweet.fields=created_at,author_id,text,entities": {
            "data": [
                {
                    "id": "102",
                    "text": "Timeline post worth scanning",
                    "created_at": "2026-04-20T10:00:00Z",
                    "author_id": "9",
                }
            ],
            "includes": {"users": [{"id": "9", "username": "xdev"}]},
        },
    }
    requested_paths: list[str] = []

    def runner(path: str, auth_type: str) -> dict:
        requested_paths.append(path)
        return responses[path]

    items = XUrlConnector(
        runner=runner,
        title_fetcher=lambda url: "Launch article" if url == "https://example.com/launch" else None,
    ).collect(["bookmarks", "likes", "home_timeline_reverse_chronological"])

    assert requested_paths == [
        "/2/users/me",
        "/2/users/42/bookmarks?max_results=100&tweet.fields=created_at,author_id,text,entities",
        "/2/users/42/liked_tweets?max_results=100&tweet.fields=created_at,author_id,text,entities",
        "/2/users/42/timelines/reverse_chronological?max_results=100&tweet.fields=created_at,author_id,text,entities",
    ]
    assert [item.source for item in items] == ["x_bookmarks", "x_likes", "x_home_timeline_reverse_chronological"]
    assert [item.url for item in items] == [
        "https://example.com/launch",
        "https://x.com/anthropic/status/101",
        "https://x.com/xdev/status/102",
    ]
    assert items[0].title == "Launch article"
    assert items[0].metadata["tweet_url"] == "https://x.com/openai/status/100"
    assert items[0].intent_signals is not None and items[0].intent_signals.saved is True
    assert items[1].intent_signals is not None and items[1].intent_signals.liked is True
    assert items[2].metadata["x_signal"] == "home_timeline_reverse_chronological"
    assert items[2].provenance is not None and items[2].provenance.acquisition_mode == "official_api"


def test_xurl_connector_rejects_unknown_signal_type() -> None:
    connector = XUrlConnector(runner=lambda path, auth_type: {"data": {"id": "42", "username": "akita"}})

    try:
        connector.collect(["for_you"])
    except ValueError as exc:
        assert "Unsupported X signal type" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_xurl_connector_falls_back_to_oauth1_when_oauth2_fails() -> None:
    requests: list[tuple[str, str]] = []

    def runner(path: str, auth_type: str) -> dict:
        requests.append((auth_type, path))
        if auth_type == "oauth2":
            raise RuntimeError("oauth2 missing")
        responses = {
            "/2/users/me": {"data": {"id": "42", "username": "akita"}},
            "/2/users/42/bookmarks?max_results=100&tweet.fields=created_at,author_id,text,entities": {
                "data": [
                    {
                        "id": "100",
                        "text": "Saved launch thread",
                        "created_at": "2026-04-20T08:00:00Z",
                        "author_id": "7",
                    }
                ],
                "includes": {"users": [{"id": "7", "username": "openai"}]},
            },
        }
        return responses[path]

    items = XUrlConnector(runner=runner).collect(["bookmarks"])

    assert requests == [
        ("oauth2", "/2/users/me"),
        ("oauth1", "/2/users/me"),
        ("oauth1", "/2/users/42/bookmarks?max_results=100&tweet.fields=created_at,author_id,text,entities"),
    ]
    assert len(items) == 1
    assert items[0].source == "x_bookmarks"


def test_xurl_connector_uses_codex_spark_title_when_external_url_title_is_missing() -> None:
    responses = {
        "/2/users/me": {"data": {"id": "42", "username": "akita"}},
        "/2/users/42/bookmarks?max_results=100&tweet.fields=created_at,author_id,text,entities": {
            "data": [
                {
                    "id": "100",
                    "text": "A long tweet about a launch with extra commentary",
                    "created_at": "2026-04-20T08:00:00Z",
                    "author_id": "7",
                    "entities": {
                        "urls": [
                            {
                                "expanded_url": "https://example.com/launch",
                            }
                        ]
                    },
                }
            ],
            "includes": {"users": [{"id": "7", "username": "openai"}]},
        },
    }
    synth_calls: list[tuple[str, str]] = []

    def synthesizer(text: str, url: str) -> str:
        synth_calls.append((text, url))
        return "Compressed launch title"

    items = XUrlConnector(
        runner=lambda path, auth_type: responses[path],
        title_fetcher=lambda url: None,
        title_synthesizer=synthesizer,
        enable_title_synthesis=True,
    ).collect(["bookmarks"])

    assert items[0].url == "https://example.com/launch"
    assert items[0].title == "Compressed launch title"
    assert synth_calls == [("A long tweet about a launch with extra commentary", "https://example.com/launch")]


def test_xurl_connector_limits_external_title_resolution_budget() -> None:
    responses = {
        "/2/users/me": {"data": {"id": "42", "username": "akita"}},
        "/2/users/42/bookmarks?max_results=100&tweet.fields=created_at,author_id,text,entities": {
            "data": [
                {
                    "id": "100",
                    "text": "First external link",
                    "created_at": "2026-04-20T08:00:00Z",
                    "author_id": "7",
                    "entities": {"urls": [{"expanded_url": "https://example.com/1"}]},
                },
                {
                    "id": "101",
                    "text": "Second external link",
                    "created_at": "2026-04-20T08:01:00Z",
                    "author_id": "7",
                    "entities": {"urls": [{"expanded_url": "https://example.com/2"}]},
                },
            ],
            "includes": {"users": [{"id": "7", "username": "openai"}]},
        },
    }
    fetch_calls: list[str] = []
    synth_calls: list[tuple[str, str]] = []

    items = XUrlConnector(
        runner=lambda path, auth_type: responses[path],
        title_fetcher=lambda url: fetch_calls.append(url) or None,
        title_synthesizer=lambda text, url: synth_calls.append((text, url)) or "Synthesized title",
        max_external_title_resolutions=1,
        enable_title_synthesis=True,
    ).collect(["bookmarks"])

    assert fetch_calls == ["https://example.com/1"]
    assert synth_calls == [("First external link", "https://example.com/1")]
    assert items[0].title == "Synthesized title"
    assert items[1].title == "Second external link"


def test_run_xurl_json_surfaces_credits_depleted_detail_without_account_id(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            output="",
            stderr=(
                '{\n'
                '  "account_id":1243608864848629761,\n'
                '  "title":"CreditsDepleted",\n'
                '  "detail":"Your enrolled account [1243608864848629761] does not have any credits to fulfill this request.",\n'
                '  "type":"https://api.twitter.com/2/problems/credits"\n'
                '}\n'
                'Error: request failed\n'
            ),
        )

    monkeypatch.setattr("hermes_pulse.connectors.x_url.subprocess.run", fake_run)

    try:
        _run_xurl_json("/2/users/42/bookmarks", "oauth2")
    except RuntimeError as exc:
        message = str(exc)
        assert "CreditsDepleted: X API spend cap reached" in message
        assert "does not have any credits" not in message
        assert "1243608864848629761" not in message
        assert "/2/users/42/bookmarks" in message
    else:
        raise AssertionError("expected RuntimeError")


def test_xurl_connector_does_not_fall_back_to_oauth1_for_spend_cap() -> None:
    requests: list[tuple[str, str]] = []

    def runner(path: str, auth_type: str) -> dict:
        requests.append((auth_type, path))
        raise RuntimeError("SpendCapReached: X API spend cap reached; blocked until 2026-06-18")

    try:
        XUrlConnector(runner=runner).collect(["bookmarks"])
    except RuntimeError as exc:
        assert "SpendCapReached" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert requests == [("oauth2", "/2/users/me")]
