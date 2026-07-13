import sqlite3
from pathlib import Path

import hermes_pulse.connectors.x_browser as x_browser
from hermes_pulse.connectors.x_browser import XBrowserConnector, refresh_x_browser_profile


def test_x_browser_connector_collects_likes_and_home_timeline_from_authenticated_pages(tmp_path: Path) -> None:
    requested: list[tuple[str, int]] = []
    snapshots = {
        "https://x.com/Y20010920T/likes": {
            "active_handle": "Y20010920T",
            "posts": [
                {
                    "id": "101",
                    "username": "anthropic",
                    "text": "Liked benchmark result",
                    "created_at": "2026-07-14T00:10:00.000Z",
                    "tweet_url": "https://x.com/anthropic/status/101",
                }
            ],
        },
        "https://x.com/home": {
            "active_handle": "Y20010920T",
            "posts": [
                {
                    "id": "102",
                    "username": "xdev",
                    "text": "Timeline post worth scanning",
                    "created_at": "2026-07-14T00:20:00.000Z",
                    "tweet_url": "https://x.com/xdev/status/102",
                }
            ],
        },
    }

    def page_reader(url: str, limit: int) -> dict[str, object]:
        requested.append((url, limit))
        return snapshots[url]

    items = XBrowserConnector(
        profile_root=tmp_path / "x-profile",
        profile_directory="Profile 4",
        expected_handle="Y20010920T",
        limit=20,
        page_reader=page_reader,
    ).collect(["likes", "home_timeline_reverse_chronological"])

    assert requested == [
        ("https://x.com/Y20010920T/likes", 20),
        ("https://x.com/home", 20),
    ]
    assert [item.source for item in items] == ["x_likes", "x_home_timeline_reverse_chronological"]
    assert [item.url for item in items] == [
        "https://x.com/anthropic/status/101",
        "https://x.com/xdev/status/102",
    ]
    assert items[0].intent_signals is not None and items[0].intent_signals.liked is True
    assert items[1].intent_signals is not None and items[1].intent_signals.liked is False
    assert items[0].provenance is not None
    assert items[0].provenance.acquisition_mode == "browser_automation_experimental"
    assert items[0].metadata["x_signal"] == "likes"
    assert items[1].metadata["x_signal"] == "home_timeline_reverse_chronological"


def test_x_browser_connector_fails_closed_on_account_identity_mismatch(tmp_path: Path) -> None:
    connector = XBrowserConnector(
        profile_root=tmp_path / "x-profile",
        profile_directory="Profile 4",
        expected_handle="Y20010920T",
        page_reader=lambda url, limit: {"active_handle": "different_account", "posts": []},
    )

    try:
        connector.collect(["likes"])
    except RuntimeError as exc:
        assert "identity mismatch" in str(exc)
        assert "Y20010920T" in str(exc)
        assert "different_account" in str(exc)
    else:
        raise AssertionError("expected identity mismatch")


def test_x_browser_connector_uses_bounded_default_browser_reader(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def fake_reader(**kwargs):
        calls.append(kwargs)
        return {"active_handle": "Y20010920T", "posts": []}

    monkeypatch.setattr(x_browser, "_read_authenticated_x_page", fake_reader)

    connector = XBrowserConnector(
        profile_root=tmp_path / "x-profile",
        profile_directory="Profile 4",
        expected_handle="Y20010920T",
        limit=12,
        chrome_path=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    )

    assert connector.collect(["likes"]) == []
    assert calls == [
        {
            "profile_root": tmp_path / "x-profile",
            "profile_directory": "Profile 4",
            "expected_handle": "Y20010920T",
            "url": "https://x.com/Y20010920T/likes",
            "limit": 12,
            "chrome_path": Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        }
    ]


def test_refresh_x_browser_profile_copies_only_required_metadata_and_cookie_database(tmp_path: Path) -> None:
    source_root = tmp_path / "Chrome"
    source_profile = source_root / "Profile 4"
    source_profile.mkdir(parents=True)
    (source_root / "Local State").write_text('{"profile": {}}')
    (source_profile / "Preferences").write_text('{"translate": {"enabled": false}}')
    (source_profile / "Secure Preferences").write_text("{}")
    with sqlite3.connect(source_profile / "Cookies") as connection:
        connection.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
        connection.execute("INSERT INTO cookies VALUES (?, ?, ?)", (".x.com", "session", b"encrypted"))
        connection.execute("INSERT INTO cookies VALUES (?, ?, ?)", (".example.com", "unrelated", b"other-secret"))

    destination_root = tmp_path / "x-pulse-profile"
    result = refresh_x_browser_profile(
        source_user_data_dir=source_root,
        source_profile_directory="Profile 4",
        destination_user_data_dir=destination_root,
        destination_profile_directory="Profile 4",
    )

    destination_profile = destination_root / "Profile 4"
    assert result == destination_profile
    assert (destination_root / "Local State").read_text() == '{"profile": {}}'
    assert (destination_profile / "Preferences").read_text() == '{"translate": {"enabled": false}}'
    assert (destination_profile / "Secure Preferences").read_text() == "{}"
    with sqlite3.connect(destination_profile / "Cookies") as connection:
        assert connection.execute(
            "SELECT host_key, name, encrypted_value FROM cookies ORDER BY host_key"
        ).fetchall() == [
            (
                ".x.com",
                "session",
                b"encrypted",
            )
        ]
    assert destination_root.stat().st_mode & 0o777 == 0o700
    assert (destination_profile / "Cookies").stat().st_mode & 0o777 == 0o600
