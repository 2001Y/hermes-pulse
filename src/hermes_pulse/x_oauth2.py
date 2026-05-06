import base64
import inspect
import json
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_SHARED_ENV_PATH = Path("~/.config/env/shared.env").expanduser()
DEFAULT_XURL_PATH = Path("~/.xurl").expanduser()
DEFAULT_XURL_APP_NAME = "default"
DEFAULT_MIN_VALID_SECONDS = 300
X_OAUTH2_TOKEN_ENDPOINT = "https://api.x.com/2/oauth2/token"


class XOAuth2ReauthRequiredError(RuntimeError):
    pass


@dataclass
class XOAuth2Credentials:
    client_id: str
    client_secret: str
    username: str
    access_token: str
    refresh_token: str
    expiration_time: int
    app_name: str = DEFAULT_XURL_APP_NAME


def refresh_x_oauth2_token(
    *,
    shared_env_path: Path = DEFAULT_SHARED_ENV_PATH,
    xurl_path: Path = DEFAULT_XURL_PATH,
    xurl_app_name: str | None = None,
    min_valid_seconds: int = DEFAULT_MIN_VALID_SECONDS,
    force: bool = False,
    allow_interactive_reauth: bool = False,
    validate_runner=None,
    refresh_runner=None,
    interactive_reauth_runner=None,
) -> dict[str, object]:
    credentials = load_x_oauth2_credentials(
        shared_env_path=shared_env_path,
        xurl_path=xurl_path,
        xurl_app_name=xurl_app_name,
    )
    refresh_runner = refresh_runner or _refresh_token_via_x_api
    interactive_reauth_runner = interactive_reauth_runner or (lambda: _run_xurl_interactive_reauth(credentials))

    if not force and _is_token_valid(credentials.expiration_time, min_valid_seconds=min_valid_seconds):
        _invoke_validate_runner(validate_runner, credentials)
        return {"status": "valid", "changed": False}

    try:
        refreshed = refresh_runner(credentials)
        updated = XOAuth2Credentials(
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            username=credentials.username,
            access_token=refreshed["access_token"],
            refresh_token=refreshed.get("refresh_token") or credentials.refresh_token,
            expiration_time=int(time.time()) + int(refreshed.get("expires_in") or 0),
            app_name=credentials.app_name,
        )
        _write_x_oauth2_credentials(shared_env_path=shared_env_path, xurl_path=xurl_path, credentials=updated)
        _invoke_validate_runner(validate_runner, updated)
        return {"status": "refreshed", "changed": True}
    except Exception as refresh_error:
        if not allow_interactive_reauth:
            raise XOAuth2ReauthRequiredError(
                "X OAuth2 refresh failed; interactive reauth required"
            ) from refresh_error

    interactive_reauth_runner()
    updated = load_x_oauth2_credentials(
        shared_env_path=shared_env_path,
        xurl_path=xurl_path,
        xurl_app_name=xurl_app_name,
        prefer_env_tokens=False,
    )
    _write_shared_env_app_config(shared_env_path, updated)
    _invoke_validate_runner(validate_runner, updated)
    return {"status": "interactive_reauth", "changed": True}


def load_x_oauth2_credentials(
    *,
    shared_env_path: Path,
    xurl_path: Path,
    xurl_app_name: str | None = None,
    prefer_env_tokens: bool = True,
) -> XOAuth2Credentials:
    env = _load_exported_env(shared_env_path)
    xurl_payload = _load_xurl_payload(xurl_path)
    app_name = xurl_app_name or xurl_payload.get("default_app") or DEFAULT_XURL_APP_NAME
    app = ((xurl_payload.get("apps") or {}).get(app_name) or {})
    username = env.get("X_OAUTH2_USERNAME") or app.get("default_user")
    if not username:
        raise ValueError("X_OAUTH2_USERNAME is missing")
    token_payload = (((app.get("oauth2_tokens") or {}).get(username) or {}).get("oauth2") or {})
    client_id = env.get("X_CLIENT_ID") or app.get("client_id")
    client_secret = env.get("X_CLIENT_SECRET") or app.get("client_secret")
    access_token = token_payload.get("access_token")
    refresh_token = token_payload.get("refresh_token")
    expiration_time = int(token_payload.get("expiration_time") or 0)
    if not client_id or not client_secret or not access_token or not refresh_token:
        raise ValueError("X OAuth2 credentials are incomplete")
    return XOAuth2Credentials(
        client_id=client_id,
        client_secret=client_secret,
        username=username,
        access_token=access_token,
        refresh_token=refresh_token,
        expiration_time=int(expiration_time),
        app_name=app_name,
    )


def _is_token_valid(expiration_time: int, *, min_valid_seconds: int) -> bool:
    return expiration_time > int(time.time()) + min_valid_seconds


def _invoke_validate_runner(validate_runner, credentials: XOAuth2Credentials) -> str:
    if validate_runner is None:
        return _run_xurl_whoami_oauth2(credentials)
    parameter_count = len(inspect.signature(validate_runner).parameters)
    if parameter_count == 0:
        return validate_runner()
    return validate_runner(credentials)


def _run_xurl_whoami_oauth2(credentials: XOAuth2Credentials) -> str:
    completed = subprocess.run(
        ["xurl", "--app", _shell_safe_app_name(credentials), "whoami", "--auth", "oauth2", "--username", credentials.username],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _refresh_token_via_x_api(credentials: XOAuth2Credentials) -> dict[str, object]:
    payload = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": credentials.refresh_token,
        }
    ).encode()
    authorization = base64.b64encode(f"{credentials.client_id}:{credentials.client_secret}".encode()).decode()
    request = urllib.request.Request(
        X_OAUTH2_TOKEN_ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Basic {authorization}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(body or str(exc)) from exc


def _run_xurl_interactive_reauth(credentials: XOAuth2Credentials) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", 8080))
        except OSError as exc:
            raise RuntimeError("127.0.0.1:8080 is already in use; cannot start xurl auth oauth2") from exc
    subprocess.run(["xurl", "--app", _shell_safe_app_name(credentials), "auth", "oauth2"], check=True)


def _write_x_oauth2_credentials(*, shared_env_path: Path, xurl_path: Path, credentials: XOAuth2Credentials) -> None:
    _write_shared_env_app_config(shared_env_path, credentials)
    payload = _load_xurl_payload(xurl_path)
    apps = payload.setdefault("apps", {})
    app_name = credentials.app_name or payload.get("default_app") or DEFAULT_XURL_APP_NAME
    app = apps.setdefault(app_name, {})
    app["client_id"] = credentials.client_id
    app["client_secret"] = credentials.client_secret
    app["default_user"] = credentials.username
    tokens = app.setdefault("oauth2_tokens", {})
    tokens[credentials.username] = {
        "type": "oauth2",
        "oauth2": {
            "access_token": credentials.access_token,
            "refresh_token": credentials.refresh_token,
            "expiration_time": credentials.expiration_time,
        },
    }
    xurl_path.write_text(yaml.safe_dump(payload, sort_keys=False))


def _write_shared_env_app_config(shared_env_path: Path, credentials: XOAuth2Credentials) -> None:
    env = _load_exported_env(shared_env_path)
    env["X_CLIENT_ID"] = credentials.client_id
    env["X_CLIENT_SECRET"] = credentials.client_secret
    env["X_OAUTH2_USERNAME"] = credentials.username
    lines = shared_env_path.read_text().splitlines() if shared_env_path.exists() else []
    for key, value in env.items():
        replacement = f'export {key}="{value}"'
        for index, line in enumerate(lines):
            if line.startswith(f"export {key}="):
                lines[index] = replacement
                break
        else:
            lines.append(replacement)
    shared_env_path.parent.mkdir(parents=True, exist_ok=True)
    shared_env_path.write_text("\n".join(lines) + "\n")


def _load_exported_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith("export ") or "=" not in stripped:
            continue
        key, value = stripped.removeprefix("export ").split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def _load_xurl_payload(path: Path) -> dict:
    if not path.exists():
        return {"apps": {}, "default_app": DEFAULT_XURL_APP_NAME}
    return yaml.safe_load(path.read_text()) or {"apps": {}, "default_app": DEFAULT_XURL_APP_NAME}


def _shell_safe_app_name(credentials: XOAuth2Credentials) -> str:
    return getattr(credentials, "app_name", DEFAULT_XURL_APP_NAME)
