from __future__ import annotations

import plistlib
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from hermes_pulse.model_policy import require_pulse_codex_model
from hermes_pulse.summarization.codex_cli import DEFAULT_CODEX_MODEL, DEFAULT_SUMMARY_FORMAT

DEFAULT_LAUNCHD_PATH = "/Users/akitani/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DEFAULT_WRAPPER_FILENAME = "run-hermes-pulse-direct-delivery.sh"
DEFAULT_SHARED_ENV_PATH = Path("~/.config/env/shared.env").expanduser()
DEFAULT_XURL_APP_NAME = "default"


@dataclass(frozen=True)
class DirectDeliveryWrapperSpec:
    python_executable: Path
    repo_root: Path
    channel: str
    digest_command: str = "morning-digest"
    thread_ts: str | None = None
    archive_root: Path | None = None
    source_registry: Path | None = None
    feed_fixture: Path | None = None
    search_fixture: Path | None = None
    chatgpt_history: Path | None = None
    chatgpt_export_dir: Path | None = None
    grok_history: Path | None = None
    grok_history_fallback_db: Path | None = None
    hermes_history: Path | None = None
    notes: Path | None = None
    x_signals: str | None = None
    x_browser_signals: str | None = None
    x_browser_profile_root: Path | None = None
    x_browser_profile_directory: str = "Profile 4"
    x_browser_handle: str | None = None
    x_browser_limit: int = 20
    chrome_user_data_dir: Path | None = None
    chrome_profile_directory: str = "Profile 4"
    codex_model: str = DEFAULT_CODEX_MODEL
    summary_format: str = DEFAULT_SUMMARY_FORMAT
    working_directory: Path | None = None
    shared_env_path: Path = DEFAULT_SHARED_ENV_PATH
    xurl_app_name: str = DEFAULT_XURL_APP_NAME

    def __post_init__(self) -> None:
        require_pulse_codex_model(self.codex_model)


@dataclass(frozen=True)
class LaunchdPlistSpec:
    label: str
    program_arguments: Sequence[str]
    hour: int | None = None
    minute: int | None = None
    interval_seconds: int | None = None
    working_directory: Path | None = None
    standard_out_path: Path | None = None
    standard_error_path: Path | None = None
    run_at_load: bool = False

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("launchd label must not be empty")
        uses_calendar = self.hour is not None or self.minute is not None
        uses_interval = self.interval_seconds is not None
        if uses_calendar == uses_interval:
            raise ValueError("launchd spec must use either calendar schedule or interval schedule")
        if uses_calendar:
            if self.hour is None or self.minute is None:
                raise ValueError("launchd calendar schedule requires both hour and minute")
            if not 0 <= self.hour <= 23:
                raise ValueError("launchd hour must be between 0 and 23")
            if not 0 <= self.minute <= 59:
                raise ValueError("launchd minute must be between 0 and 59")
        if uses_interval:
            assert self.interval_seconds is not None
            if self.interval_seconds <= 0:
                raise ValueError("launchd interval must be greater than 0")


@dataclass(frozen=True)
class LocationWalkWrapperSpec:
    python_executable: Path
    repo_root: Path
    channel: str
    state_db: Path
    output_path: Path
    thread_ts: str | None = None
    source_registry: Path | None = None
    working_directory: Path | None = None
    shared_env_path: Path = DEFAULT_SHARED_ENV_PATH


@dataclass(frozen=True)
class GeneratedLaunchdArtifacts:
    wrapper_path: Path
    plist_path: Path


def build_direct_delivery_program_arguments(spec: DirectDeliveryWrapperSpec) -> list[str]:
    args = [
        str(spec.python_executable),
        "-m",
        "hermes_pulse.direct_delivery",
        "--command",
        spec.digest_command,
        "--channel",
        spec.channel,
    ]
    if spec.thread_ts is not None:
        args.extend(["--thread-ts", spec.thread_ts])
    if spec.archive_root is not None:
        args.extend(["--archive-root", str(spec.archive_root)])
    if spec.source_registry is not None:
        args.extend(["--source-registry", str(spec.source_registry)])
    if spec.feed_fixture is not None:
        args.extend(["--feed-fixture", str(spec.feed_fixture)])
    if spec.search_fixture is not None:
        args.extend(["--search-fixture", str(spec.search_fixture)])
    if spec.chatgpt_history is not None:
        args.extend(["--chatgpt-history", str(spec.chatgpt_history)])
    if spec.grok_history is not None:
        args.extend(["--grok-history", str(spec.grok_history)])
    if spec.hermes_history is not None:
        args.extend(["--hermes-history", str(spec.hermes_history)])
    if spec.notes is not None:
        args.extend(["--notes", str(spec.notes)])
    if spec.x_signals is not None and spec.x_browser_signals is not None:
        raise ValueError("Use either x_signals or x_browser_signals, not both")
    if spec.x_signals is not None:
        args.extend(["--x-signals", spec.x_signals])
    if spec.x_browser_signals is not None:
        if spec.x_browser_profile_root is None or spec.x_browser_handle is None:
            raise ValueError("x_browser_signals requires x_browser_profile_root and x_browser_handle")
        args.extend(
            [
                "--x-browser-signals",
                spec.x_browser_signals,
                "--x-browser-profile-root",
                str(spec.x_browser_profile_root),
                "--x-browser-profile-directory",
                spec.x_browser_profile_directory,
                "--x-browser-handle",
                spec.x_browser_handle,
                "--x-browser-limit",
                str(spec.x_browser_limit),
            ]
        )
    args.extend(["--codex-model", spec.codex_model, "--summary-format", spec.summary_format])
    return args


def _render_optional_refresh_command(command: str, *, warning_message: str) -> str:
    return "\n".join(
        [
            f"if ! {command} >/dev/null 2>&1; then",
            f'  echo "{warning_message}" >&2',
            "fi",
        ]
    )


def render_direct_delivery_wrapper(spec: DirectDeliveryWrapperSpec) -> str:
    repo_root = Path(spec.repo_root)
    working_directory = Path(spec.working_directory or repo_root)
    python_path_root = repo_root / "src"
    command = " ".join(shlex.quote(argument) for argument in build_direct_delivery_program_arguments(spec))
    refresh_commands: list[str] = []
    if spec.chatgpt_export_dir is not None and spec.chatgpt_history is not None:
        refresh_chatgpt_args = [
            str(spec.python_executable),
            "-m",
            "hermes_pulse.cli",
            "refresh-chatgpt-history",
            "--input-dir",
            str(spec.chatgpt_export_dir),
            "--output-dir",
            str(spec.chatgpt_history),
        ]
        refresh_chatgpt_command = " ".join(shlex.quote(argument) for argument in refresh_chatgpt_args)
        refresh_commands.append(
            _render_optional_refresh_command(
                refresh_chatgpt_command,
                warning_message="warning: chatgpt history refresh failed; continuing with existing import",
            )
        )
    if spec.grok_history is not None:
        refresh_args = [
            str(spec.python_executable),
            "-m",
            "hermes_pulse.cli",
            "refresh-grok-history",
            "--output-dir",
            str(spec.grok_history),
            "--cdp-port",
            "9223",
            "--page-size",
            "100",
        ]
        refresh_command = " ".join(shlex.quote(argument) for argument in refresh_args)
        if spec.grok_history_fallback_db is not None:
            fallback_args = [
                str(spec.python_executable),
                "-m",
                "hermes_pulse.cli",
                "refresh-grok-history-fallback",
                "--history-db",
                str(spec.grok_history_fallback_db),
                "--output-dir",
                str(spec.grok_history),
            ]
            fallback_command = " ".join(shlex.quote(argument) for argument in fallback_args)
            refresh_commands.append(
                "\n".join(
                    [
                        f"if ! {refresh_command} >/dev/null 2>&1; then",
                        '  echo "warning: grok history refresh failed; trying Chrome History fallback" >&2',
                        f"  if ! {fallback_command} >/dev/null 2>&1; then",
                        '    echo "warning: grok history fallback also failed; continuing with existing import" >&2',
                        "  fi",
                        "fi",
                    ]
                )
            )
        else:
            refresh_commands.append(
                _render_optional_refresh_command(
                    refresh_command,
                    warning_message="warning: grok history refresh failed; continuing with existing import",
                )
            )
    if spec.x_signals is not None:
        refresh_x_oauth2_args = [
            str(spec.python_executable),
            "-m",
            "hermes_pulse.cli",
            "refresh-x-oauth2",
            "--shared-env-path",
            str(spec.shared_env_path),
            "--xurl-app-name",
            spec.xurl_app_name,
            "--min-valid-seconds",
            "900",
        ]
        refresh_commands.append(
            _render_optional_refresh_command(
                " ".join(shlex.quote(argument) for argument in refresh_x_oauth2_args),
                warning_message="warning: X OAuth2 refresh failed; digest will continue without X signals",
            )
        )
    if spec.x_browser_signals is not None:
        if spec.chrome_user_data_dir is None or spec.x_browser_profile_root is None:
            raise ValueError("x_browser_signals requires chrome_user_data_dir and x_browser_profile_root")
        refresh_x_browser_args = [
            str(spec.python_executable),
            "-m",
            "hermes_pulse.cli",
            "refresh-x-browser-profile",
            "--chrome-user-data-dir",
            str(spec.chrome_user_data_dir),
            "--chrome-profile-directory",
            spec.chrome_profile_directory,
            "--x-browser-profile-root",
            str(spec.x_browser_profile_root),
            "--x-browser-profile-directory",
            spec.x_browser_profile_directory,
        ]
        refresh_commands.append(
            _render_optional_refresh_command(
                " ".join(shlex.quote(argument) for argument in refresh_x_browser_args),
                warning_message=(
                    "warning: X browser profile refresh failed; "
                    "digest will continue with existing browser profile"
                ),
            )
        )
    shared_env_path = spec.shared_env_path
    shared_env_reference = (
        f"~/{shared_env_path.relative_to(Path.home())}"
        if shared_env_path.is_absolute() and Path.home() in shared_env_path.parents
        else str(shared_env_path)
    )
    shared_env_shell = shared_env_reference if shared_env_reference.startswith("~/") else shlex.quote(shared_env_reference)
    xurl_app_name = shlex.quote(spec.xurl_app_name)
    xurl_bootstrap_lines = (
        [
            'if [ -n "${X_CLIENT_ID:-}" ] && [ -n "${X_CLIENT_SECRET:-}" ]; then',
            f'  if ! xurl auth apps add {xurl_app_name} --client-id "$X_CLIENT_ID" --client-secret "$X_CLIENT_SECRET" >/dev/null 2>&1; then',
            f'    xurl auth apps update {xurl_app_name} --client-id "$X_CLIENT_ID" --client-secret "$X_CLIENT_SECRET" >/dev/null 2>&1',
            "  fi",
            '  if [ -n "${X_OAUTH2_USERNAME:-}" ]; then',
            f'    xurl auth default {xurl_app_name} "$X_OAUTH2_USERNAME" >/dev/null 2>&1 || true',
            "  else",
            f'    xurl auth default {xurl_app_name} >/dev/null 2>&1 || true',
            "  fi",
            "fi",
            "",
        ]
        if spec.x_signals is not None
        else []
    )
    return "\n".join(
        [
            "#!/bin/zsh",
            "set -euo pipefail",
            "",
            f"export PATH={DEFAULT_LAUNCHD_PATH}",
            f"export PYTHONPATH={shlex.quote(str(python_path_root))}\"${{PYTHONPATH:+:$PYTHONPATH}}\"",
            "",
            f'if [ -f {shared_env_shell} ]; then',
            f'  source {shared_env_shell}',
            "fi",
            *xurl_bootstrap_lines,
            f"cd {shlex.quote(str(working_directory))}",
            *refresh_commands,
            f"exec {command}",
            "",
        ]
    )


def build_location_walk_program_arguments(spec: LocationWalkWrapperSpec) -> list[str]:
    args = [
        str(spec.python_executable),
        "-m",
        "hermes_pulse.cli",
        "location-walk",
    ]
    if spec.source_registry is not None:
        args.extend(["--source-registry", str(spec.source_registry)])
    args.extend(["--state-db", str(spec.state_db), "--output", str(spec.output_path)])
    return args


def build_location_walk_slack_post_arguments(spec: LocationWalkWrapperSpec) -> list[str]:
    args = [
        str(spec.python_executable),
        "-m",
        "hermes_pulse.slack_direct",
        "--input-file",
        str(spec.output_path),
        "--channel",
        spec.channel,
    ]
    if spec.thread_ts is not None:
        args.extend(["--thread-ts", spec.thread_ts])
    return args


def render_location_walk_wrapper(spec: LocationWalkWrapperSpec) -> str:
    repo_root = Path(spec.repo_root)
    working_directory = Path(spec.working_directory or repo_root)
    python_path_root = repo_root / "src"
    shared_env_path = spec.shared_env_path
    shared_env_reference = (
        f"~/{shared_env_path.relative_to(Path.home())}"
        if shared_env_path.is_absolute() and Path.home() in shared_env_path.parents
        else str(shared_env_path)
    )
    shared_env_shell = shared_env_reference if shared_env_reference.startswith("~/") else shlex.quote(shared_env_reference)
    cli_command = " ".join(shlex.quote(argument) for argument in build_location_walk_program_arguments(spec))
    slack_command = " ".join(shlex.quote(argument) for argument in build_location_walk_slack_post_arguments(spec))
    output_path = shlex.quote(str(spec.output_path))
    return "\n".join(
        [
            "#!/bin/zsh",
            "set -euo pipefail",
            "",
            f"export PATH={DEFAULT_LAUNCHD_PATH}",
            f"export PYTHONPATH={shlex.quote(str(python_path_root))}\"${{PYTHONPATH:+:$PYTHONPATH}}\"",
            "",
            f'if [ -f {shared_env_shell} ]; then',
            f'  source {shared_env_shell}',
            "fi",
            "",
            f"cd {shlex.quote(str(working_directory))}",
            f"rm -f {output_path}",
            cli_command,
            f"if [ -f {output_path} ]; then",
            f"  {slack_command}",
            "fi",
            "",
        ]
    )


def render_launchd_plist(spec: LaunchdPlistSpec) -> str:
    payload: dict[str, object] = {
        "Label": spec.label,
        "ProgramArguments": [str(argument) for argument in spec.program_arguments],
        "RunAtLoad": spec.run_at_load,
    }
    if spec.interval_seconds is not None:
        payload["StartInterval"] = spec.interval_seconds
    else:
        payload["StartCalendarInterval"] = {"Hour": spec.hour, "Minute": spec.minute}
    if spec.working_directory is not None:
        payload["WorkingDirectory"] = str(spec.working_directory)
    if spec.standard_out_path is not None:
        payload["StandardOutPath"] = str(spec.standard_out_path)
    if spec.standard_error_path is not None:
        payload["StandardErrorPath"] = str(spec.standard_error_path)
    return plistlib.dumps(payload, sort_keys=False).decode()


def generate_launchd_artifacts(
    output_directory: str | Path,
    *,
    wrapper_spec: DirectDeliveryWrapperSpec,
    plist_spec: LaunchdPlistSpec,
    wrapper_filename: str = DEFAULT_WRAPPER_FILENAME,
    plist_filename: str | None = None,
) -> GeneratedLaunchdArtifacts:
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)

    wrapper_path = output_directory / wrapper_filename
    wrapper_path.write_text(render_direct_delivery_wrapper(wrapper_spec))
    wrapper_path.chmod(0o755)

    plist_path = output_directory / (plist_filename or f"{plist_spec.label}.plist")
    program_arguments = [str(argument) for argument in plist_spec.program_arguments] or [str(wrapper_path)]
    plist_path.write_text(
        render_launchd_plist(
            LaunchdPlistSpec(
                label=plist_spec.label,
                program_arguments=program_arguments,
                hour=plist_spec.hour,
                minute=plist_spec.minute,
                working_directory=plist_spec.working_directory,
                standard_out_path=plist_spec.standard_out_path,
                standard_error_path=plist_spec.standard_error_path,
                run_at_load=plist_spec.run_at_load,
            )
        )
    )

    return GeneratedLaunchdArtifacts(wrapper_path=wrapper_path, plist_path=plist_path)
