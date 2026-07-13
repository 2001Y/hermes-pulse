import plistlib
import shlex
from pathlib import Path

from hermes_pulse.launchd import (
    DirectDeliveryWrapperSpec,
    GeneratedLaunchdArtifacts,
    LaunchdPlistSpec,
    LocationWalkWrapperSpec,
    generate_launchd_artifacts,
    render_direct_delivery_wrapper,
    render_launchd_plist,
    render_location_walk_wrapper,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_render_direct_delivery_wrapper_targets_module_with_channel_thread_and_archive_args() -> None:
    spec = DirectDeliveryWrapperSpec(
        python_executable=Path("/opt/homebrew/bin/python3"),
        repo_root=REPO_ROOT,
        channel="C123456",
        thread_ts="1712345.6789",
        archive_root=Path("/Users/akitani/Pulse Archive"),
        chatgpt_history=Path("/Users/akitani/Pulse/Imports/chatgpt"),
        chatgpt_export_dir=Path("/Users/akitani/Downloads"),
        grok_history=Path("/Users/akitani/Pulse/Imports/grok/browser-export"),
        grok_history_fallback_db=Path("/Users/akitani/Library/Application Support/Google/Chrome/Profile 4/History"),
        x_signals="bookmarks,likes,home_timeline_reverse_chronological",
        codex_model="gpt-5.4",
        summary_format="briefing-v1",
        digest_command="evening-digest",
    )

    wrapper = render_direct_delivery_wrapper(spec)

    assert wrapper.startswith("#!/bin/zsh\nset -euo pipefail\n")
    assert f"cd {shlex.quote(str(REPO_ROOT))}" in wrapper
    assert str(REPO_ROOT / "src") in wrapper
    assert "source ~/.config/env/shared.env" in wrapper
    assert 'xurl auth apps add default --client-id "$X_CLIENT_ID" --client-secret "$X_CLIENT_SECRET"' in wrapper
    assert 'xurl auth apps update default --client-id "$X_CLIENT_ID" --client-secret "$X_CLIENT_SECRET"' in wrapper
    assert 'xurl auth default default' in wrapper
    assert 'xurl auth default default "$X_OAUTH2_USERNAME"' in wrapper
    assert 'X_OAUTH2_ACCESS_TOKEN' not in wrapper
    assert 'X_OAUTH2_REFRESH_TOKEN' not in wrapper
    assert 'X_OAUTH2_EXPIRATION_TIME' not in wrapper
    assert 'oauth2_tokens' not in wrapper
    assert 'default_user' not in wrapper
    assert 'if ! /opt/homebrew/bin/python3 -m hermes_pulse.cli refresh-x-oauth2 --shared-env-path /Users/akitani/.config/env/shared.env --xurl-app-name default --min-valid-seconds 900 >/dev/null 2>&1; then' in wrapper
    assert 'echo "warning: X OAuth2 refresh failed; digest will continue without X signals" >&2' in wrapper
    assert 'if ! /opt/homebrew/bin/python3 -m hermes_pulse.cli refresh-chatgpt-history --input-dir /Users/akitani/Downloads --output-dir /Users/akitani/Pulse/Imports/chatgpt >/dev/null 2>&1; then' in wrapper
    assert 'echo "warning: chatgpt history refresh failed; continuing with existing import" >&2' in wrapper
    assert 'if ! /opt/homebrew/bin/python3 -m hermes_pulse.cli refresh-grok-history --output-dir /Users/akitani/Pulse/Imports/grok/browser-export --cdp-port 9223 --page-size 100 >/dev/null 2>&1; then' in wrapper
    assert 'echo "warning: grok history refresh failed; trying Chrome History fallback" >&2' in wrapper
    fallback_line = next(line for line in wrapper.splitlines() if "refresh-grok-history-fallback" in line)
    assert shlex.split(fallback_line.strip().removeprefix("if ! ").removesuffix(" >/dev/null 2>&1; then")) == [
        "/opt/homebrew/bin/python3",
        "-m",
        "hermes_pulse.cli",
        "refresh-grok-history-fallback",
        "--history-db",
        "/Users/akitani/Library/Application Support/Google/Chrome/Profile 4/History",
        "--output-dir",
        "/Users/akitani/Pulse/Imports/grok/browser-export",
    ]
    assert 'echo "warning: grok history fallback also failed; continuing with existing import" >&2' in wrapper
    assert "/opt/homebrew/bin/python3" in wrapper

    exec_line = next(line for line in wrapper.splitlines() if line.startswith("exec "))
    assert shlex.split(exec_line.removeprefix("exec ")) == [
        "/opt/homebrew/bin/python3",
        "-m",
        "hermes_pulse.direct_delivery",
        "--command",
        "evening-digest",
        "--channel",
        "C123456",
        "--thread-ts",
        "1712345.6789",
        "--archive-root",
        "/Users/akitani/Pulse Archive",
        "--chatgpt-history",
        "/Users/akitani/Pulse/Imports/chatgpt",
        "--grok-history",
        "/Users/akitani/Pulse/Imports/grok/browser-export",
        "--x-signals",
        "bookmarks,likes,home_timeline_reverse_chronological",
        "--codex-model",
        "gpt-5.4",
        "--summary-format",
        "briefing-v1",
    ]


def test_render_direct_delivery_wrapper_uses_authenticated_browser_x_signals_without_xurl() -> None:
    spec = DirectDeliveryWrapperSpec(
        python_executable=Path("/Users/akitani/.hermes/hermes-agent/venv/bin/python"),
        repo_root=REPO_ROOT,
        channel="D0AT8A3RB9A",
        x_browser_signals="likes,home_timeline_reverse_chronological",
        x_browser_profile_root=Path("/Users/akitani/.hermes/browser/x-pulse-profile"),
        x_browser_profile_directory="Profile 4",
        x_browser_handle="Y20010920T",
        x_browser_limit=20,
        chrome_user_data_dir=Path("/Users/akitani/Library/Application Support/Google/Chrome"),
        chrome_profile_directory="Profile 4",
    )

    wrapper = render_direct_delivery_wrapper(spec)

    assert "xurl auth" not in wrapper
    assert "refresh-x-oauth2" not in wrapper
    refresh_line = next(line for line in wrapper.splitlines() if "refresh-x-browser-profile" in line)
    assert shlex.split(refresh_line.strip().removeprefix("if ! ").removesuffix(" >/dev/null 2>&1; then")) == [
        "/Users/akitani/.hermes/hermes-agent/venv/bin/python",
        "-m",
        "hermes_pulse.cli",
        "refresh-x-browser-profile",
        "--chrome-user-data-dir",
        "/Users/akitani/Library/Application Support/Google/Chrome",
        "--chrome-profile-directory",
        "Profile 4",
        "--x-browser-profile-root",
        "/Users/akitani/.hermes/browser/x-pulse-profile",
        "--x-browser-profile-directory",
        "Profile 4",
    ]
    assert 'echo "warning: X browser profile refresh failed; digest will continue with existing browser profile" >&2' in wrapper
    exec_line = next(line for line in wrapper.splitlines() if line.startswith("exec "))
    exec_args = shlex.split(exec_line.removeprefix("exec "))
    browser_start = exec_args.index("--x-browser-signals")
    assert exec_args[browser_start : browser_start + 10] == [
        "--x-browser-signals",
        "likes,home_timeline_reverse_chronological",
        "--x-browser-profile-root",
        "/Users/akitani/.hermes/browser/x-pulse-profile",
        "--x-browser-profile-directory",
        "Profile 4",
        "--x-browser-handle",
        "Y20010920T",
        "--x-browser-limit",
        "20",
    ]


def test_render_launchd_plist_serializes_label_schedule_and_program_arguments() -> None:
    plist = render_launchd_plist(
        LaunchdPlistSpec(
            label="ai.hermes.pulse.direct-delivery",
            program_arguments=["/Users/akitani/bin/run-hermes-pulse.sh"],
            hour=8,
            minute=15,
            working_directory=REPO_ROOT,
            standard_out_path=Path("/tmp/hermes-pulse.out.log"),
            standard_error_path=Path("/tmp/hermes-pulse.err.log"),
        )
    )

    payload = plistlib.loads(plist.encode())

    assert payload["Label"] == "ai.hermes.pulse.direct-delivery"
    assert payload["ProgramArguments"] == ["/Users/akitani/bin/run-hermes-pulse.sh"]
    assert payload["StartCalendarInterval"] == {"Hour": 8, "Minute": 15}
    assert payload["WorkingDirectory"] == str(REPO_ROOT)
    assert payload["StandardOutPath"] == "/tmp/hermes-pulse.out.log"
    assert payload["StandardErrorPath"] == "/tmp/hermes-pulse.err.log"
    assert payload["RunAtLoad"] is False


def test_render_launchd_plist_serializes_start_interval_when_requested() -> None:
    plist = render_launchd_plist(
        LaunchdPlistSpec(
            label="ai.hermes.pulse.location-walk",
            program_arguments=["/Users/akitani/bin/run-hermes-pulse-location-walk.sh"],
            interval_seconds=300,
            working_directory=REPO_ROOT,
            standard_out_path=Path("/tmp/hermes-pulse-location-walk.out.log"),
            standard_error_path=Path("/tmp/hermes-pulse-location-walk.err.log"),
        )
    )

    payload = plistlib.loads(plist.encode())

    assert payload["Label"] == "ai.hermes.pulse.location-walk"
    assert payload["ProgramArguments"] == ["/Users/akitani/bin/run-hermes-pulse-location-walk.sh"]
    assert payload["StartInterval"] == 300
    assert "StartCalendarInterval" not in payload
    assert payload["WorkingDirectory"] == str(REPO_ROOT)
    assert payload["StandardOutPath"] == "/tmp/hermes-pulse-location-walk.out.log"
    assert payload["StandardErrorPath"] == "/tmp/hermes-pulse-location-walk.err.log"


def test_render_location_walk_wrapper_runs_cli_then_posts_generated_markdown() -> None:
    output_path = Path("/Users/akitani/.hermes/tmp/location-walk.md")
    state_db = Path("/Users/akitani/.hermes/state/hermes-pulse.db")
    source_registry = REPO_ROOT / "fixtures/source_registry/sample_sources.yaml"
    spec = LocationWalkWrapperSpec(
        python_executable=Path("/opt/homebrew/bin/python3"),
        repo_root=REPO_ROOT,
        channel="D123456",
        thread_ts="1712345.6789",
        source_registry=source_registry,
        state_db=state_db,
        output_path=output_path,
    )

    wrapper = render_location_walk_wrapper(spec)

    assert wrapper.startswith("#!/bin/zsh\nset -euo pipefail\n")
    assert f"rm -f {shlex.quote(str(output_path))}" in wrapper
    assert f"if [ -f {shlex.quote(str(output_path))} ]; then" in wrapper

    cli_line = next(line for line in wrapper.splitlines() if " -m hermes_pulse.cli location-walk " in line)
    assert shlex.split(cli_line) == [
        "/opt/homebrew/bin/python3",
        "-m",
        "hermes_pulse.cli",
        "location-walk",
        "--source-registry",
        str(source_registry),
        "--state-db",
        str(state_db),
        "--output",
        str(output_path),
    ]

    slack_line = next(line for line in wrapper.splitlines() if " -m hermes_pulse.slack_direct " in line)
    assert shlex.split(slack_line) == [
        "/opt/homebrew/bin/python3",
        "-m",
        "hermes_pulse.slack_direct",
        "--input-file",
        str(output_path),
        "--channel",
        "D123456",
        "--thread-ts",
        "1712345.6789",
    ]


def test_generate_launchd_artifacts_writes_wrapper_and_plist_to_output_directory(tmp_path: Path) -> None:
    output_directory = tmp_path / "generated"

    artifacts = generate_launchd_artifacts(
        output_directory,
        wrapper_spec=DirectDeliveryWrapperSpec(
            python_executable=Path("/opt/homebrew/bin/python3"),
            repo_root=REPO_ROOT,
            channel="C123456",
            archive_root=Path("/Users/akitani/Pulse"),
            chatgpt_history=Path("/Users/akitani/Pulse/Imports/chatgpt"),
            chatgpt_export_dir=Path("/Users/akitani/Downloads"),
            grok_history=Path("/Users/akitani/Pulse/Imports/grok/browser-export"),
            grok_history_fallback_db=Path("/Users/akitani/Library/Application Support/Google/Chrome/Profile 4/History"),
            x_signals="bookmarks,likes",
            codex_model="gpt-5.4",
            summary_format="briefing-v1",
            digest_command="evening-digest",
        ),
        plist_spec=LaunchdPlistSpec(
            label="ai.hermes.pulse.direct-delivery",
            program_arguments=[],
            hour=9,
            minute=5,
        ),
    )

    assert artifacts == GeneratedLaunchdArtifacts(
        wrapper_path=output_directory / "run-hermes-pulse-direct-delivery.sh",
        plist_path=output_directory / "ai.hermes.pulse.direct-delivery.plist",
    )
    assert artifacts.wrapper_path.exists()
    assert artifacts.plist_path.exists()
    assert artifacts.wrapper_path.stat().st_mode & 0o111

    wrapper = artifacts.wrapper_path.read_text()
    refresh_chatgpt_line = next(line for line in wrapper.splitlines() if "refresh-chatgpt-history" in line)
    assert shlex.split(refresh_chatgpt_line.removeprefix("if ! ").removesuffix(" >/dev/null 2>&1; then")) == [
        "/opt/homebrew/bin/python3",
        "-m",
        "hermes_pulse.cli",
        "refresh-chatgpt-history",
        "--input-dir",
        "/Users/akitani/Downloads",
        "--output-dir",
        "/Users/akitani/Pulse/Imports/chatgpt",
    ]
    exec_line = next(line for line in wrapper.splitlines() if line.startswith("exec "))
    assert shlex.split(exec_line.removeprefix("exec ")) == [
        "/opt/homebrew/bin/python3",
        "-m",
        "hermes_pulse.direct_delivery",
        "--command",
        "evening-digest",
        "--channel",
        "C123456",
        "--archive-root",
        "/Users/akitani/Pulse",
        "--chatgpt-history",
        "/Users/akitani/Pulse/Imports/chatgpt",
        "--grok-history",
        "/Users/akitani/Pulse/Imports/grok/browser-export",
        "--x-signals",
        "bookmarks,likes",
        "--codex-model",
        "gpt-5.4",
        "--summary-format",
        "briefing-v1",
    ]

    plist_payload = plistlib.loads(artifacts.plist_path.read_bytes())
    assert plist_payload["ProgramArguments"] == [str(artifacts.wrapper_path)]
    assert plist_payload["StartCalendarInterval"] == {"Hour": 9, "Minute": 5}
