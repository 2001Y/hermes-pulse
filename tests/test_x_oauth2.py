from pathlib import Path

import pytest

import hermes_pulse.cli
import hermes_pulse.x_oauth2
from hermes_pulse.x_oauth2 import X_OAUTH2_TOKEN_ENDPOINT, load_x_oauth2_credentials, refresh_x_oauth2_token


def _write_shared_env(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                'export X_CLIENT_ID="client-id"',
                'export X_CLIENT_SECRET="client-secret"',
                'export X_OAUTH2_USERNAME="akita"',
            ]
        )
        + "\n"
    )


def _append_shared_env_token_exports(
    path: Path,
    *,
    access_value: str = "env-access",
    refresh_value: str = "env-refresh",
    expiration_time: int = 0,
) -> None:
    with path.open("a") as handle:
        handle.write(
            "\n".join(
                [
                    f'export X_OAUTH2_ACCESS_TOKEN="{access_value}"',
                    f'export X_OAUTH2_REFRESH_TOKEN="{refresh_value}"',
                    f'export X_OAUTH2_EXPIRATION_TIME="{expiration_time}"',
                ]
            )
            + "\n"
        )


def _write_xurl(
    path: Path,
    *,
    expiration_time: int = 0,
    access_token: str = "old-access",
    refresh_token: str = "old-refresh",
) -> None:
    path.write_text(
        (
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
          access_token: {access_token}
          refresh_token: {refresh_token}
          expiration_time: {expiration_time}
default_app: default
""".strip()
            + "\n"
        ).format(
            access_token=access_token,
            refresh_token=refresh_token,
            expiration_time=expiration_time,
        )
    )


def test_x_oauth2_refresh_uses_expected_token_endpoint() -> None:
    assert X_OAUTH2_TOKEN_ENDPOINT == "https://api.x.com/2/oauth2/token"


def test_refresh_x_oauth2_token_noops_when_token_is_still_valid(tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"
    xurl_path = tmp_path / ".xurl"
    _write_shared_env(shared_env)
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


def test_refresh_x_oauth2_token_refreshes_and_updates_xurl_without_writing_tokens_to_shared_env(tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"
    xurl_path = tmp_path / ".xurl"
    _write_shared_env(shared_env)
    _append_shared_env_token_exports(shared_env, access_value="stale-env-access", refresh_value="stale-env-refresh")
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
    assert 'export X_CLIENT_ID="client-id"' in shared_env_text
    assert 'export X_CLIENT_SECRET="client-secret"' in shared_env_text
    assert 'export X_OAUTH2_USERNAME="akita"' in shared_env_text
    assert "new-access" not in shared_env_text
    assert "new-refresh" not in shared_env_text
    assert "X_OAUTH2_ACCESS_TOKEN" not in shared_env_text
    assert "X_OAUTH2_REFRESH_TOKEN" not in shared_env_text
    assert "X_OAUTH2_EXPIRATION_TIME" not in shared_env_text
    xurl_text = xurl_path.read_text()
    assert "custom-app:" in xurl_text
    assert "access_token: new-access" in xurl_text
    assert "refresh_token: new-refresh" in xurl_text


def test_refresh_x_oauth2_token_falls_back_to_interactive_reauth_when_allowed_without_writing_tokens_to_shared_env(tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"
    xurl_path = tmp_path / ".xurl"
    _write_shared_env(shared_env)
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
    assert 'export X_CLIENT_ID="client-id"' in shared_env_text
    assert 'export X_CLIENT_SECRET="client-secret"' in shared_env_text
    assert 'export X_OAUTH2_USERNAME="akita"' in shared_env_text
    assert "interactive-access" not in shared_env_text
    assert "interactive-refresh" not in shared_env_text
    xurl_text = xurl_path.read_text()
    assert "access_token: interactive-access" in xurl_text
    assert "refresh_token: interactive-refresh" in xurl_text


def test_load_x_oauth2_credentials_ignores_shared_env_token_exports_and_uses_xurl_tokens(tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"
    xurl_path = tmp_path / ".xurl"
    _write_shared_env(shared_env)
    _append_shared_env_token_exports(
        shared_env,
        expiration_time=4102444800,
        access_value="env-fresh-access",
        refresh_value="env-fresh-refresh",
    )
    _write_xurl(
        xurl_path,
        expiration_time=0,
        access_token="xurl-access",
        refresh_token="xurl-refresh",
    )

    credentials = load_x_oauth2_credentials(shared_env_path=shared_env, xurl_path=xurl_path)

    assert credentials.access_token == "xurl-access"
    assert credentials.refresh_token == "xurl-refresh"
    assert credentials.expiration_time == 0


def test_load_x_oauth2_credentials_requires_xurl_token_payload_even_if_shared_env_has_token_exports(tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"
    xurl_path = tmp_path / ".xurl"
    _write_shared_env(shared_env)
    _append_shared_env_token_exports(
        shared_env,
        expiration_time=4102444800,
        access_value="env-fresh-access",
        refresh_value="env-fresh-refresh",
    )
    xurl_path.write_text(
        """
apps:
  default:
    client_id: client-id
    client_secret: client-secret
    default_user: akita
default_app: default
""".strip()
        + "\n"
    )

    with pytest.raises(ValueError, match="X OAuth2 credentials are incomplete"):
        load_x_oauth2_credentials(shared_env_path=shared_env, xurl_path=xurl_path)


def test_refresh_x_oauth2_token_validates_using_refreshed_credentials(monkeypatch, tmp_path: Path) -> None:
    shared_env = tmp_path / "shared.env"
    xurl_path = tmp_path / ".xurl"
    _write_shared_env(shared_env)
    _write_xurl(
        xurl_path,
        expiration_time=0,
        access_token="xurl-stale-access",
        refresh_token="xurl-stale-refresh",
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
