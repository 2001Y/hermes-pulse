from pathlib import Path

import hermes_pulse.cli
import hermes_pulse.x_oauth2
from hermes_pulse.x_oauth2 import X_OAUTH2_TOKEN_ENDPOINT, load_x_oauth2_credentials, refresh_x_oauth2_token


def _write_shared_env(
    path: Path,
    *,
    expiration_time: int = 0,
    access_value: str = "env-access",
    refresh_value: str = "env-refresh",
) -> None:
    path.write_text(
        "\n".join(
            [
                'export X_CLIENT_ID="client-id"',
                'export X_CLIENT_SECRET="client-secret"',
                'export X_OAUTH2_USERNAME="akita"',
                f'export X_OAUTH2_ACCESS_TOKEN="{access_value}"',
                f'export X_OAUTH2_REFRESH_TOKEN="{refresh_value}"',
                f'export X_OAUTH2_EXPIRATION_TIME="{expiration_time}"',
            ]
        )
        + "\n"
    )


def _write_xurl(path: Path, *, expiration_time: int = 0) -> None:
    path.write_text(
        """
apps:
  default:
    client_id: client-id
    client_secret: client-secret
    default_user: akita
    oauth2_tokens:
      akita:
        type: oauth2
        oauth2:
          access_token: old-access
          refresh_token: old-refresh
          expiration_time: %d
default_app: default
""".strip()
        % expiration_time
        + "\n"
    )


def test_x_oauth2_refresh_uses_expected_token_endpoint() -> None:
    assert X_OAUTH2_TOKEN_ENDPOINT == "https://api.x.com/2/oauth2/token"


def test_refresh_x_oauth2_token_noops_when_token_is_still_valid(tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"
    xurl_path = tmp_path / ".xurl"
    _write_shared_env(shared_env, expiration_time=4102444800)
    _write_xurl(xurl_path, expiration_time=4102444800)
    whoami_calls: list[str] = []

    result = refresh_x_oauth2_token(
        shared_env_path=shared_env,
        xurl_path=xurl_path,
        min_valid_seconds=300,
        validate_runner=lambda: whoami_calls.append("whoami") or '{"data":{"id":"42"}}',
        refresh_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("refresh should not run")),
        interactive_reauth_runner=lambda: (_ for _ in ()).throw(AssertionError("interactive reauth should not run")),
    )

    assert result == {"status": "valid", "changed": False}
    assert whoami_calls == ["whoami"]


def test_refresh_x_oauth2_token_refreshes_and_updates_shared_env_and_xurl(tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"
    xurl_path = tmp_path / ".xurl"
    _write_shared_env(shared_env, expiration_time=0)
    _write_xurl(xurl_path, expiration_time=0)
    xurl_path.write_text(xurl_path.read_text().replace("default:\n", "custom-app:\n", 1).replace("default_app: default", "default_app: custom-app"))
    validation_calls: list[str] = []

    result = refresh_x_oauth2_token(
        shared_env_path=shared_env,
        xurl_path=xurl_path,
        xurl_app_name="custom-app",
        min_valid_seconds=300,
        validate_runner=lambda: validation_calls.append("whoami") or '{"data":{"id":"42"}}',
        refresh_runner=lambda *_args, **_kwargs: {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 7200,
        },
        interactive_reauth_runner=lambda: (_ for _ in ()).throw(AssertionError("interactive reauth should not run")),
    )

    assert result["status"] == "refreshed"
    assert result["changed"] is True
    assert validation_calls == ["whoami"]
    shared_env_text = shared_env.read_text()
    assert 'export X_OAUTH2_ACCESS_TOKEN="new-access"' in shared_env_text
    assert 'export X_OAUTH2_REFRESH_TOKEN="new-refresh"' in shared_env_text
    xurl_text = xurl_path.read_text()
    assert "custom-app:" in xurl_text
    assert "access_token: new-access" in xurl_text
    assert "refresh_token: new-refresh" in xurl_text


def test_refresh_x_oauth2_token_falls_back_to_interactive_reauth_when_allowed(tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"
    xurl_path = tmp_path / ".xurl"
    _write_shared_env(shared_env, expiration_time=0)
    _write_xurl(xurl_path, expiration_time=0)
    xurl_path.write_text(
        xurl_path.read_text()
        + """
apps:
  custom-app:
    client_id: client-id
    client_secret: client-secret
    default_user: akita
    oauth2_tokens:
      akita:
        type: oauth2
        oauth2:
          access_token: custom-old-access
          refresh_token: custom-old-refresh
          expiration_time: 0
"""
    )
    calls: list[str] = []

    def interactive_reauth() -> None:
        calls.append("interactive")
        xurl_path.write_text(
            xurl_path.read_text().replace("access_token: custom-old-access", "access_token: interactive-access").replace(
                "refresh_token: custom-old-refresh", "refresh_token: interactive-refresh"
            ).replace("expiration_time: 0", "expiration_time: 4102444800", 1)
        )

    result = refresh_x_oauth2_token(
        shared_env_path=shared_env,
        xurl_path=xurl_path,
        xurl_app_name="custom-app",
        min_valid_seconds=300,
        allow_interactive_reauth=True,
        validate_runner=lambda: '{"data":{"id":"42"}}',
        refresh_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("refresh token invalid")),
        interactive_reauth_runner=interactive_reauth,
    )

    assert result == {"status": "interactive_reauth", "changed": True}
    assert calls == ["interactive"]
    shared_env_text = shared_env.read_text()
    assert 'export X_OAUTH2_ACCESS_TOKEN=' in shared_env_text
    assert 'interactive-access' in shared_env_text
    assert 'export X_OAUTH2_REFRESH_TOKEN=' in shared_env_text
    assert 'interactive-refresh' in shared_env_text


def test_load_x_oauth2_credentials_prefers_shared_env_tokens_when_env_is_fresher(tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"
    xurl_path = tmp_path / ".xurl"
    _write_shared_env(shared_env, expiration_time=4102444800)
    _write_xurl(xurl_path, expiration_time=0)

    credentials = load_x_oauth2_credentials(shared_env_path=shared_env, xurl_path=xurl_path)

    assert credentials.access_token == "env-access"
    assert credentials.refresh_token == "env-refresh"
    assert credentials.expiration_time == 4102444800


def test_load_x_oauth2_credentials_prefers_xurl_tokens_when_env_is_stale(tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"
    xurl_path = tmp_path / ".xurl"
    _write_shared_env(shared_env, expiration_time=0, access_value="env-stale-access", refresh_value="env-stale-refresh")
    _write_xurl(xurl_path, expiration_time=4102444800)

    credentials = load_x_oauth2_credentials(shared_env_path=shared_env, xurl_path=xurl_path)

    assert credentials.access_token == "old-access"
    assert credentials.refresh_token == "old-refresh"
    assert credentials.expiration_time == 4102444800


def test_load_x_oauth2_credentials_xurl_first_uses_complete_env_fallback_instead_of_hybrid(tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"
    xurl_path = tmp_path / ".xurl"
    _write_shared_env(shared_env, expiration_time=4102444800, access_value="env-good-access", refresh_value="env-good-refresh")
    _write_xurl(xurl_path, expiration_time=4102444800)
    xurl_path.write_text(xurl_path.read_text().replace("refresh_token: old-refresh", 'refresh_token: ""'))

    credentials = load_x_oauth2_credentials(
        shared_env_path=shared_env,
        xurl_path=xurl_path,
        prefer_env_tokens=False,
    )

    assert credentials.access_token == "env-good-access"
    assert credentials.refresh_token == "env-good-refresh"
    assert credentials.expiration_time == 4102444800


def test_refresh_x_oauth2_token_validates_using_refreshed_credentials(monkeypatch, tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"
    xurl_path = tmp_path / ".xurl"
    _write_shared_env(shared_env, expiration_time=0, access_value="env-access", refresh_value="env-refresh")
    _write_xurl(xurl_path, expiration_time=0)
    xurl_path.write_text(
        xurl_path.read_text()
        .replace("access_token: old-access", "access_token: xurl-stale-access")
        .replace("refresh_token: old-refresh", "refresh_token: xurl-stale-refresh")
    )
    validated_tokens: list[str] = []

    monkeypatch.setattr(
        hermes_pulse.x_oauth2,
        "_run_xurl_whoami_oauth2",
        lambda credentials: validated_tokens.append(credentials.access_token) or '{"data":{"id":"42"}}',
    )

    result = refresh_x_oauth2_token(
        shared_env_path=shared_env,
        xurl_path=xurl_path,
        min_valid_seconds=300,
        refresh_runner=lambda *_args, **_kwargs: {
            "access_token": "refreshed-access",
            "refresh_token": "refreshed-refresh",
            "expires_in": 7200,
        },
    )

    assert result["status"] == "refreshed"
    assert validated_tokens == ["refreshed-access"]


def test_cli_refresh_x_oauth2_command_delegates_to_helper(monkeypatch, tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"

    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        hermes_pulse.cli,
        "refresh_x_oauth2_token",
        lambda **kwargs: calls.append(kwargs) or {"status": "valid", "changed": False},
    )

    assert (
        hermes_pulse.cli.main(
            [
                "refresh-x-oauth2",
                "--shared-env-path",
                str(shared_env),
                "--xurl-app-name",
                "custom-app",
                "--min-valid-seconds",
                "900",
                "--allow-interactive-reauth",
            ]
        )
        == 0
    )
    assert calls == [
        {
            "shared_env_path": shared_env,
            "xurl_app_name": "custom-app",
            "min_valid_seconds": 900,
            "force": False,
            "allow_interactive_reauth": True,
        }
    ]
