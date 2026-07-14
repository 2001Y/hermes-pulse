import argparse
import inspect
import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from hermes_pulse.archive import (
    load_items_from_source_ledgers,
    write_archive_raw_items,
    write_morning_digest_archive,
)
from hermes_pulse.collection import collect_for_trigger
from hermes_pulse.connectors.audit_context import AuditContextConnector, load_audit_context_fixture
from hermes_pulse.connectors.feed_registry import FeedRegistryConnector
from hermes_pulse.connectors.chatgpt_history import ChatGPTHistoryConnector
from hermes_pulse.connectors.gmail import GmailConnector
from hermes_pulse.connectors.google_calendar import GoogleCalendarConnector
from hermes_pulse.connectors.grok_history import GrokHistoryConnector
from hermes_pulse.connectors.hermes_history import HermesHistoryConnector
from hermes_pulse.connectors.known_source_search import KnownSourceSearchConnector
from hermes_pulse.connectors.location_context import LocationContextConnector, load_location_context_fixture
from hermes_pulse.connectors.notes import NotesConnector
from hermes_pulse.connectors.x_activity_likes import XActivityLikesConnector
from hermes_pulse.connectors.x_url import XUrlConnector
from hermes_pulse.exporters.chatgpt_export_prep import ChatGPTExportPreparer
from hermes_pulse.exporters.grok_browser_export import GrokBrowserExporter
from hermes_pulse.exporters.grok_history_fallback import ChromeHistoryGrokExporter
from hermes_pulse.db import (
    get_approval_action_record,
    get_suppression,
    initialize_database,
    list_active_suppression_subjects,
    list_connector_cursor_records,
    list_recent_approval_actions,
    record_approval_action,
    record_delivery,
    record_feedback,
    record_suppression,
    record_trigger_run,
    summarize_feedback_signals,
    update_approval_action,
    update_suppression_status,
    update_suppression_superseded_flag,
    update_trigger_run_status,
    upsert_connector_cursor,
    upsert_source_registry_state,
)
from hermes_pulse.delivery.local_markdown import LocalMarkdownDelivery
from hermes_pulse.models import CollectedItem, SourceRegistryEntry, TriggerEvent, TriggerScope
from hermes_pulse.rendering import (
    _parse_key_value_lines,
    render_feed_update_nudge,
    render_feed_update_deep_brief,
    render_feed_update_source_audit,
    render_gap_window_mini_digest,
    render_leave_now_warning,
    render_location_arrival_mini_digest,
    render_location_walk_nudge,
    render_mail_operational_warning,
    render_shopping_replenishment_action_prep,
    render_trigger_quality_review,
)
from hermes_pulse.source_registry import load_source_registry
from hermes_pulse.summarization import CodexCliSummarizer
from hermes_pulse.trigger_registry import get_trigger_profile
from hermes_pulse.x_oauth2 import DEFAULT_SHARED_ENV_PATH, refresh_x_oauth2_token


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_REGISTRY = REPO_ROOT / "fixtures/source_registry/default_sources.yaml"


class BoundConnector:
    def __init__(self, collector: Callable[[], list[object]]) -> None:
        self._collector = collector

    def collect(self) -> list[object]:
        return self._collector()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-pulse")
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "morning-digest",
            "evening-digest",
            "leave-now-warning",
            "mail-operational",
            "shopping-replenishment",
            "feed-update",
            "location-arrival",
            "location-walk",
            "review-trigger-quality",
            "gap-window-mini-digest",
            "feed-update-deep-brief",
            "feed-update-source-audit",
            "approve-action",
            "reject-action",
            "complete-action",
            "failed-action",
            "dismiss-suppression",
            "expire-suppression",
            "supersede-suppression",
            "refresh-grok-history",
            "refresh-grok-history-fallback",
            "refresh-chatgpt-history",
            "prepare-chatgpt-history",
            "refresh-x-oauth2",
            "state-summary",
        ),
    )
    parser.add_argument("--source-registry", type=Path)
    parser.add_argument("--feed-fixture", type=Path)
    parser.add_argument("--search-fixture", type=Path)
    parser.add_argument("--calendar-fixture", type=Path)
    parser.add_argument("--gmail-fixture", type=Path)
    parser.add_argument("--location-fixture", type=Path)
    parser.add_argument("--audit-fixture", type=Path)
    parser.add_argument("--chatgpt-history", type=Path)
    parser.add_argument("--grok-history", type=Path)
    parser.add_argument("--hermes-history", type=Path)
    parser.add_argument("--notes", type=Path)
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--archive-label")
    parser.add_argument("--window-start")
    parser.add_argument("--window-end")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--input-file", type=Path)
    parser.add_argument("--history-db", type=Path)
    parser.add_argument("--cdp-port", type=int, default=9223)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--shared-env-path", type=Path, default=DEFAULT_SHARED_ENV_PATH)
    parser.add_argument("--xurl-app-name", default="default")
    parser.add_argument("--min-valid-seconds", type=int, default=300)
    parser.add_argument("--allow-interactive-reauth", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--action-id")
    parser.add_argument("--suppression-id")
    parser.add_argument("--execution-receipt")
    parser.add_argument("--execution-error")
    parser.add_argument("--execution-provider")
    parser.add_argument("--execution-store")
    parser.add_argument("--execution-order-id")
    parser.add_argument("--retryable", action="store_true")
    parser.add_argument("--now")
    parser.add_argument("--x-signals")
    parser.add_argument("--x-expected-username")
    parser.add_argument("--x-likes-reconcile-interval-hours", type=int, default=0)
    parser.add_argument("--x-activity-likes-file", type=Path)
    parser.add_argument("--x-expected-user-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command is None:
        return 0

    run_id: str | None = None
    profile = None
    occurred_at = _occurred_at_for_command(args.command, args)
    if args.command in {"approve-action", "reject-action", "complete-action", "failed-action"}:
        if args.state_db is None or not args.action_id:
            raise ValueError("action update commands require --state-db and --action-id")
        _update_approval_action_from_command(
            args.state_db,
            action_id=args.action_id,
            command=args.command,
            occurred_at=occurred_at,
            execution_receipt=getattr(args, "execution_receipt", None),
            execution_error=getattr(args, "execution_error", None),
            execution_provider=getattr(args, "execution_provider", None),
            execution_store=getattr(args, "execution_store", None),
            execution_order_id=getattr(args, "execution_order_id", None),
            retryable=getattr(args, "retryable", False),
        )
        return 0
    if args.command in {"dismiss-suppression", "expire-suppression", "supersede-suppression"}:
        if args.state_db is None or not args.suppression_id:
            raise ValueError("suppression update commands require --state-db and --suppression-id")
        _update_suppression_from_command(
            args.state_db,
            suppression_id=args.suppression_id,
            command=args.command,
        )
        return 0
    if args.command == "refresh-grok-history":
        if args.output_dir is None:
            raise ValueError("refresh-grok-history requires --output-dir")
        GrokBrowserExporter(cdp_port=args.cdp_port).export(args.output_dir, page_size=args.page_size)
        return 0
    if args.command == "refresh-grok-history-fallback":
        if args.history_db is None or args.output_dir is None:
            raise ValueError("refresh-grok-history-fallback requires --history-db and --output-dir")
        ChromeHistoryGrokExporter().export(args.history_db, args.output_dir)
        return 0
    if args.command == "refresh-chatgpt-history":
        if args.input_dir is None or args.output_dir is None:
            raise ValueError("refresh-chatgpt-history requires --input-dir and --output-dir")
        preparer = ChatGPTExportPreparer()
        preparer.refresh_latest_export(args.input_dir, args.output_dir)
        return 0
    if args.command == "prepare-chatgpt-history":
        if args.input_file is None or args.output_dir is None:
            raise ValueError("prepare-chatgpt-history requires --input-file and --output-dir")
        ChatGPTExportPreparer().prepare(args.input_file, args.output_dir)
        return 0
    if args.command == "refresh-x-oauth2":
        refresh_x_oauth2_token(
            shared_env_path=args.shared_env_path,
            xurl_app_name=args.xurl_app_name,
            min_valid_seconds=args.min_valid_seconds,
            force=args.force,
            allow_interactive_reauth=args.allow_interactive_reauth,
        )
        return 0
    if args.command == "state-summary":
        if args.state_db is None:
            raise ValueError("state-summary requires --state-db")
        markdown = _render_state_summary(args.state_db)
        if args.output is not None:
            LocalMarkdownDelivery().deliver(markdown, args.output)
        else:
            print(markdown, end="")
        return 0
    if args.state_db is not None:
        profile = _profile_for_command(args.command)
        run_id = record_trigger_run(
            args.state_db,
            event_type=profile.event_type,
            profile_id=profile.id,
            occurred_at=occurred_at,
            output_mode=profile.output_mode,
            status="started",
        )

    delivery_succeeded = False
    pending_connector_cursor_update: tuple[list[CollectedItem], list[str], list[str], str] | None = None
    pending_local_connector_health_update: tuple[dict[str, str], set[str], list[CollectedItem], str] | None = None
    pending_source_registry_state_update: tuple[list[SourceRegistryEntry], list[CollectedItem], str, dict[str, str], set[str]] | None = None
    pending_suppression_update: tuple[list[CollectedItem], str, str, str, int | None] | None = None
    pending_feedback_update: tuple[list[CollectedItem], str, str] | None = None
    pending_approval_action_update: tuple[list[CollectedItem], str, str] | None = None
    try:
        markdown: str | None = None

        if args.command in {"morning-digest", "evening-digest"}:
            items, source_errors, successful_sources = _build_digest_with_source_errors(args.command, args)
            occurred_at = _occurred_at_for_command(args.command, args)
            deliverable_items = items
            if args.state_db is not None:
                items = _filter_items_already_seen_by_connector_cursor(args.state_db, items=items)
                deliverable_items = _filter_suppressed_items(
                    args.state_db,
                    items=items,
                    trigger_family=profile.event_type,
                    occurred_at=occurred_at,
                )
                pending_connector_cursor_update = (
                    items,
                    _parse_x_signal_types(getattr(args, "x_signals", None)),
                    _requested_history_connectors(args),
                    occurred_at,
                )
                pending_source_registry_state_update = (
                    load_source_registry(args.source_registry or DEFAULT_SOURCE_REGISTRY),
                    items,
                    occurred_at,
                    source_errors,
                    successful_sources,
                )
                if run_id is not None:
                    pending_suppression_update = (
                        deliverable_items,
                        profile.event_type,
                        occurred_at,
                        run_id,
                        None,
                    )
            archive_root = args.archive_root or Path.home() / "Pulse"
            archive_directory = write_morning_digest_archive(
                items=deliverable_items,
                archive_root=archive_root,
                archive_date=_archive_label_for_args(args),
                retrieved_at=occurred_at,
            )
            _apply_replay_window_if_requested(
                archive_directory,
                archive_root=archive_root,
                args=args,
            )
            markdown = CodexCliSummarizer().summarize_archive(archive_directory).content
        elif args.command == "leave-now-warning":
            markdown = _build_leave_now_warning(args)
        elif args.command == "mail-operational":
            markdown = _build_mail_operational(args)
        elif args.command == "shopping-replenishment":
            items = _build_event_trigger_items("shopping.replenishment.default", args)
            markdown = render_shopping_replenishment_action_prep(items)
            if args.state_db is not None and run_id is not None:
                pending_approval_action_update = (items, occurred_at, run_id)
        elif args.command == "feed-update":
            markdown = _build_feed_update(args)
        elif args.command == "location-arrival":
            markdown = _build_location_arrival(args)
        elif args.command == "location-walk":
            location_errors: dict[str, str] = {}
            location_successful: set[str] = set()
            items = _collect_location_context_items(
                location_fixture=getattr(args, "location_fixture", None),
                error_handler=lambda connector_id, message: location_errors.__setitem__(connector_id, message),
                success_handler=lambda connector_id: location_successful.add(connector_id),
            )
            deliverable_items = items
            if args.state_db is not None:
                deliverable_items = _filter_suppressed_items(
                    args.state_db,
                    items=items,
                    trigger_family=profile.event_type,
                    occurred_at=occurred_at,
                )
                pending_local_connector_health_update = (location_errors, location_successful, items, occurred_at)
                if run_id is not None:
                    pending_suppression_update = (
                        deliverable_items,
                        profile.event_type,
                        occurred_at,
                        run_id,
                        profile.cooldown_minutes,
                    )
            markdown = render_location_walk_nudge(deliverable_items)
        elif args.command == "review-trigger-quality":
            items = _build_event_trigger_items("review.trigger_quality.default", args)
            markdown = render_trigger_quality_review(items)
            if args.state_db is not None and run_id is not None:
                pending_feedback_update = (items, occurred_at, run_id)
        elif args.command == "gap-window-mini-digest":
            markdown = _build_gap_window(args)
        elif args.command == "feed-update-deep-brief":
            markdown = _build_feed_update_deep_brief(args)
        elif args.command == "feed-update-source-audit":
            markdown = _build_feed_update_source_audit(args)
        else:
            return 0

        if args.output is not None and markdown is not None:
            delivered_path = LocalMarkdownDelivery().deliver(markdown, args.output)
            delivery_succeeded = True
            if args.state_db is not None and run_id is not None:
                record_delivery(args.state_db, run_id=run_id, destination=str(delivered_path), status="success")
                if pending_suppression_update is not None:
                    items, trigger_family, occurred_at, suppression_run_id, cooldown_minutes = pending_suppression_update
                    _record_suppression_history(
                        args.state_db,
                        items=items,
                        trigger_family=trigger_family,
                        occurred_at=occurred_at,
                        run_id=suppression_run_id,
                        cooldown_minutes=cooldown_minutes,
                    )

        if args.state_db is not None and pending_source_registry_state_update is not None:
            source_registry, items, occurred_at, source_errors, successful_sources = pending_source_registry_state_update
            _record_source_registry_state(
                args.state_db,
                source_registry=source_registry,
                items=items,
                occurred_at=occurred_at,
                source_errors=source_errors,
                successful_sources=successful_sources,
            )

        if args.state_db is not None and pending_feedback_update is not None:
            items, occurred_at, feedback_run_id = pending_feedback_update
            _record_feedback_from_audit_items(
                args.state_db,
                items=items,
                occurred_at=occurred_at,
                run_id=feedback_run_id,
            )

        if args.state_db is not None and pending_local_connector_health_update is not None:
            location_errors, location_successful, items, occurred_at = pending_local_connector_health_update
            _record_local_connector_health(
                args.state_db,
                error_messages=location_errors,
                successful_connectors=location_successful,
                items=items,
                occurred_at=occurred_at,
            )

        if args.state_db is not None and pending_approval_action_update is not None:
            items, occurred_at, action_run_id = pending_approval_action_update
            _record_approval_actions_from_items(
                args.state_db,
                items=items,
                occurred_at=occurred_at,
                run_id=action_run_id,
            )

        if args.state_db is not None and pending_connector_cursor_update is not None:
            items, x_signal_types, history_connectors, occurred_at = pending_connector_cursor_update
            _record_connector_cursors_from_items(
                args.state_db,
                items=items,
                occurred_at=occurred_at,
                x_signal_types=x_signal_types,
                history_connectors=history_connectors,
            )
    except Exception:
        if args.state_db is not None and run_id is not None:
            try:
                if delivery_succeeded:
                    update_trigger_run_status(args.state_db, run_id=run_id, status="delivery_state_error")
                else:
                    update_trigger_run_status(args.state_db, run_id=run_id, status="failed")
            except Exception:
                pass
        raise

    if args.state_db is not None and run_id is not None:
        try:
            update_trigger_run_status(args.state_db, run_id=run_id, status="completed")
        except Exception:
            if delivery_succeeded:
                try:
                    update_trigger_run_status(args.state_db, run_id=run_id, status="delivery_state_error")
                except Exception:
                    pass
            raise
    return 0


def _build_morning_digest(args: argparse.Namespace) -> list[CollectedItem]:
    return _build_digest("morning-digest", args)


def _build_digest(command: str, args: argparse.Namespace) -> list[CollectedItem]:
    items, _, _ = _build_digest_with_source_errors(command, args)
    return items


def _profile_for_command(command: str | None):
    if command is None:
        raise ValueError("command is required for state recording")
    profile_id = {
        "morning-digest": "digest.morning.default",
        "evening-digest": "digest.evening.default",
        "leave-now-warning": "calendar.leave_now.default",
        "mail-operational": "mail.operational.default",
        "shopping-replenishment": "shopping.replenishment.default",
        "feed-update": "feed.update.default",
        "location-arrival": "location.arrival.default",
        "location-walk": "location.walk.default",
        "review-trigger-quality": "review.trigger_quality.default",
        "gap-window-mini-digest": "calendar.gap_window.default",
        "feed-update-deep-brief": "feed.update.expert_depth",
        "feed-update-source-audit": "feed.update.source_audit",
    }[command]
    return get_trigger_profile(profile_id)


def _occurred_at_for_command(command: str | None, args: argparse.Namespace) -> str:
    if args.now:
        return args.now
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _archive_label_for_args(args: argparse.Namespace) -> str:
    return getattr(args, "archive_label", None) or date.today().isoformat()


def _apply_replay_window_if_requested(
    archive_directory: Path,
    *,
    archive_root: Path,
    args: argparse.Namespace,
    current_items: list[CollectedItem] | None = None,
    current_retrieved_at: str | None = None,
) -> list[CollectedItem] | None:
    window_start = getattr(args, "window_start", None)
    window_end = getattr(args, "window_end", None)
    if window_start is None and window_end is None:
        return None
    replay_items = load_items_from_source_ledgers(
        archive_root,
        window_start=window_start,
        window_end=window_end,
    )
    if current_items and current_retrieved_at:
        current_time = _parse_timestamp(current_retrieved_at)
        lower_bound = _parse_timestamp(window_start) if window_start else None
        upper_bound = _parse_timestamp(window_end) if window_end else None
        if (lower_bound is None or current_time >= lower_bound) and (
            upper_bound is None or current_time < upper_bound
        ):
            replay_items.extend(current_items)
    deduplicated_items: list[CollectedItem] = []
    seen: set[tuple[str, str]] = set()
    for item in replay_items:
        identity = (item.source, item.url or item.id)
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated_items.append(item)
    write_archive_raw_items(archive_directory, deduplicated_items)
    return deduplicated_items


def _parse_x_signal_types(value: str | None) -> list[str]:
    if not value:
        return []
    return [signal_type.strip() for signal_type in value.split(",") if signal_type.strip()]


def _x_source_for_signal_type(signal_type: str) -> str:
    return {
        "bookmarks": "x_bookmarks",
        "likes": "x_likes",
        "home_timeline_reverse_chronological": "x_home_timeline_reverse_chronological",
    }[signal_type]


def _requested_history_connectors(args: argparse.Namespace) -> list[str]:
    connectors: list[str] = []
    if getattr(args, "chatgpt_history", None) is not None:
        connectors.append("chatgpt_history")
    if getattr(args, "grok_history", None) is not None:
        connectors.append("grok_history")
    return connectors


def _collect_x_signals_with_error_capture(
    signal_types: list[str],
    *,
    expected_username: str | None,
    since_ids: dict[str, str],
    source_errors: dict[str, str],
    successful_sources: set[str],
) -> list[CollectedItem]:
    connector = XUrlConnector(expected_username=expected_username) if expected_username else XUrlConnector()
    items: list[CollectedItem] = []
    for signal_type in signal_types:
        source = _x_source_for_signal_type(signal_type)
        signal_since_ids = {signal_type: since_ids[signal_type]} if signal_type in since_ids else {}
        try:
            signal_items = (
                connector.collect([signal_type], since_ids=signal_since_ids)
                if signal_since_ids
                else connector.collect([signal_type])
            )
        except Exception as exc:
            source_errors[source] = str(exc)
            continue
        successful_sources.add(source)
        items.extend(signal_items)
    return items


def _x_since_ids_from_state(args: argparse.Namespace, signal_types: list[str]) -> dict[str, str]:
    if args.state_db is None or "home_timeline_reverse_chronological" not in signal_types:
        return {}
    existing = _get_connector_cursor_state(
        args.state_db,
        connector_id="x_home_timeline_reverse_chronological",
    )
    if existing is None or not existing["cursor"]:
        return {}
    return {"home_timeline_reverse_chronological": existing["cursor"]}


def _effective_x_signal_types(
    args: argparse.Namespace,
    requested_signal_types: list[str],
    *,
    occurred_at: str,
) -> list[str]:
    interval_hours = getattr(args, "x_likes_reconcile_interval_hours", 0)
    if "likes" not in requested_signal_types or interval_hours <= 0 or args.state_db is None:
        return requested_signal_types
    existing = _get_connector_cursor_state(args.state_db, connector_id="x_likes_reconcile")
    if existing is None or not existing["last_success_at"]:
        return requested_signal_types
    elapsed = _parse_timestamp(occurred_at) - _parse_timestamp(existing["last_success_at"])
    if elapsed >= timedelta(hours=interval_hours):
        return requested_signal_types
    return [signal_type for signal_type in requested_signal_types if signal_type != "likes"]


def _collect_x_activity_likes_with_error_capture(
    event_log: Path,
    expected_user_id: str | None,
    *,
    source_errors: dict[str, str],
    successful_sources: set[str],
) -> list[CollectedItem]:
    if not expected_user_id:
        source_errors["x_activity_likes"] = "X Activity likes require --x-expected-user-id"
        return []
    errors: list[str] = []
    try:
        items = XActivityLikesConnector(
            event_log=event_log,
            expected_user_id=expected_user_id,
            error_handler=errors.append,
        ).collect()
    except Exception as exc:
        source_errors["x_activity_likes"] = str(exc)
        return []
    if errors:
        source_errors["x_activity_likes"] = "; ".join(errors[:3])
    successful_sources.add("x_activity_likes")
    return items


def _cursor_sort_key(raw_record_id: str) -> tuple[int, str, int | str]:
    if raw_record_id.isdigit():
        return (2, "", int(raw_record_id))
    match = re.match(r"^(.*?)(\d+)$", raw_record_id)
    if match is not None:
        prefix, numeric_suffix = match.groups()
        return (1, prefix, int(numeric_suffix))
    return (0, raw_record_id, raw_record_id)


def _filter_items_already_seen_by_connector_cursor(path: Path, *, items: list[CollectedItem]) -> list[CollectedItem]:
    filtered: list[CollectedItem] = []
    for item in items:
        provenance = item.provenance
        if provenance is None or provenance.raw_record_id is None:
            filtered.append(item)
            continue
        existing = _get_connector_cursor_state(path, connector_id=item.source)
        if existing is None or existing["cursor"] is None:
            filtered.append(item)
            continue
        existing_cursor = existing["cursor"]
        last_success_at = existing["last_success_at"]
        item_updated_at = None if item.timestamps is None else item.timestamps.updated_at or item.timestamps.created_at
        if item_updated_at is not None and last_success_at is not None:
            if _timestamp_sort_key(item_updated_at) > _timestamp_sort_key(last_success_at):
                filtered.append(item)
                continue
        if _cursor_sort_key(provenance.raw_record_id) > _cursor_sort_key(existing_cursor):
            filtered.append(item)
    return filtered


def _timestamp_sort_key(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()


def _record_connector_cursors_from_items(
    path: Path,
    *,
    items: list[CollectedItem],
    occurred_at: str,
    x_signal_types: list[str],
    history_connectors: list[str],
) -> None:
    history_connector_set = set(history_connectors)
    top_cursors: dict[str, str | None] = {
        _x_source_for_signal_type(signal_type): None for signal_type in x_signal_types
    }
    success_markers: dict[str, str | None] = {
        connector_id: occurred_at for connector_id in top_cursors
    }
    for connector_id in history_connectors:
        top_cursors.setdefault(connector_id, None)
        success_markers.setdefault(connector_id, None)
    for item in items:
        if item.source not in top_cursors:
            continue
        raw_record_id = item.provenance.raw_record_id if item.provenance is not None else None
        if raw_record_id is None:
            continue
        current_cursor = top_cursors[item.source]
        if current_cursor is None or _cursor_sort_key(raw_record_id) > _cursor_sort_key(current_cursor):
            top_cursors[item.source] = raw_record_id
        if item.source in history_connector_set:
            item_updated_at = None if item.timestamps is None else item.timestamps.updated_at or item.timestamps.created_at
            if item_updated_at is None:
                continue
            current_marker = success_markers[item.source]
            if current_marker is None or _timestamp_sort_key(item_updated_at) > _timestamp_sort_key(current_marker):
                success_markers[item.source] = item_updated_at

    for connector_id, cursor in top_cursors.items():
        existing = _get_connector_cursor_state(path, connector_id=connector_id)
        upsert_connector_cursor(
            path,
            connector_id=connector_id,
            cursor=cursor,
            last_poll_at=occurred_at,
            last_success_at=(
                success_markers[connector_id]
                if success_markers[connector_id] is not None
                else None if existing is None else existing["last_success_at"]
            ),
        )


def _record_local_connector_health(
    path: Path,
    *,
    error_messages: dict[str, str],
    successful_connectors: set[str],
    items: list[CollectedItem],
    occurred_at: str,
) -> None:
    if "location_context" in successful_connectors:
        raw_record_id = next(
            (
                item.provenance.raw_record_id
                for item in items
                if item.source == "location_context" and item.provenance is not None
            ),
            None,
        )
        upsert_connector_cursor(
            path,
            connector_id="location_context",
            cursor=raw_record_id,
            last_poll_at=occurred_at,
            last_success_at=occurred_at,
            last_error=None,
        )
        return
    error_message = error_messages.get("location_context")
    if error_message is not None:
        existing = _get_connector_cursor_state(path, connector_id="location_context")
        upsert_connector_cursor(
            path,
            connector_id="location_context",
            cursor=None if existing is None else existing["cursor"],
            last_poll_at=occurred_at,
            last_success_at=None if existing is None else existing["last_success_at"],
            last_error=error_message,
        )


def _record_source_registry_state(
    path: Path,
    *,
    source_registry: list[SourceRegistryEntry],
    items: list[CollectedItem],
    occurred_at: str,
    source_errors: dict[str, str] | None = None,
    successful_sources: set[str] | None = None,
) -> None:
    item_ids_by_source: dict[str, list[str]] = {}
    for item in items:
        item_ids_by_source.setdefault(item.source, []).append(item.id)
    source_errors = source_errors or {}
    successful_sources = successful_sources or set()

    for entry in source_registry:
        if entry.acquisition_mode not in {"rss_poll", "atom_poll", "known_source_search"}:
            continue
        if entry.id not in item_ids_by_source and entry.id not in source_errors and entry.id not in successful_sources:
            continue
        existing_state = _get_source_registry_state(path, registry_id=entry.id)
        existing_notes = None if existing_state is None else existing_state["notes"]
        notes_payload = _build_source_registry_notes(existing_notes, last_error=source_errors.get(entry.id))
        item_ids = item_ids_by_source.get(entry.id)
        if item_ids is not None:
            seen_item_ids = json.dumps(item_ids, ensure_ascii=False)
            promoted_item_ids = json.dumps(item_ids, ensure_ascii=False)
        elif entry.id in successful_sources:
            seen_item_ids = json.dumps([], ensure_ascii=False)
            promoted_item_ids = json.dumps([], ensure_ascii=False)
        else:
            seen_item_ids = None if existing_state is None else existing_state["last_seen_item_ids"]
            promoted_item_ids = None if existing_state is None else existing_state["last_promoted_item_ids"]
        upsert_source_registry_state(
            path,
            registry_id=entry.id,
            last_poll_at=occurred_at,
            last_seen_item_ids=seen_item_ids,
            last_promoted_item_ids=promoted_item_ids,
            authority_tier=entry.authority_tier,
            notes=json.dumps(notes_payload, ensure_ascii=False) if notes_payload is not None else None,
        )


def _get_source_registry_state(path: Path, *, registry_id: str) -> dict[str, str | None] | None:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT last_seen_item_ids, last_promoted_item_ids, notes FROM source_registry_state WHERE registry_id = ?",
            (registry_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "last_seen_item_ids": row[0],
        "last_promoted_item_ids": row[1],
        "notes": row[2],
    }


def _get_connector_cursor_state(path: Path, *, connector_id: str) -> dict[str, str | None] | None:
    initialize_database(path)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT cursor, last_success_at, last_error FROM connector_cursors WHERE connector_id = ?",
            (connector_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "cursor": row[0],
        "last_success_at": row[1],
        "last_error": row[2],
    }


def _get_source_registry_notes(path: Path, *, registry_id: str) -> str | None:
    state = _get_source_registry_state(path, registry_id=registry_id)
    if state is None:
        return None
    return state["notes"]


def _render_state_summary(path: Path) -> str:
    cursor_rows = list_connector_cursor_records(path)
    action_rows = list_recent_approval_actions(path)
    feedback_rows = summarize_feedback_signals(path)

    lines = ["# Hermes Pulse state summary", ""]
    lines.append("## Connector cursors")
    if cursor_rows:
        for row in cursor_rows:
            lines.append(
                "- {connector_id}: cursor={cursor}, last_success_at={last_success_at}, last_error={last_error}".format(
                    connector_id=row["connector_id"],
                    cursor=row["cursor"] or "-",
                    last_success_at=row["last_success_at"] or "-",
                    last_error=row["last_error"] or "-",
                )
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Approval actions"])
    if action_rows:
        for row in action_rows:
            details_summary = "-"
            execution_details = row["execution_details"]
            if isinstance(execution_details, str) and execution_details:
                try:
                    parsed_details = json.loads(execution_details)
                except json.JSONDecodeError:
                    details_summary = execution_details
                else:
                    if isinstance(parsed_details, dict):
                        parts = [f"{key}={value}" for key, value in sorted(parsed_details.items())]
                        details_summary = ", ".join(parts) if parts else "-"
            lines.append(
                "- {action_id}: {action_kind} / {user_decision} / {execution_result} / details={details} / recorded_at={recorded_at}".format(
                    action_id=row["action_id"],
                    action_kind=row["action_kind"],
                    user_decision=row["user_decision"],
                    execution_result=row["execution_result"],
                    details=details_summary,
                    recorded_at=row["recorded_at"],
                )
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Feedback signal totals"])
    if feedback_rows:
        for row in feedback_rows:
            lines.append(f"- {row['category']} / {row['subject']} / {row['signal']}: {row['count']}")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _build_source_registry_notes(existing_notes: str | None, *, last_error: str | None) -> dict[str, object] | None:
    payload: dict[str, object]
    if existing_notes:
        try:
            parsed = json.loads(existing_notes)
        except json.JSONDecodeError:
            payload = {"review_note": existing_notes}
        else:
            if isinstance(parsed, dict):
                payload = dict(parsed)
            else:
                payload = {"review_note": existing_notes}
    else:
        payload = {}
    payload["last_error"] = last_error
    return payload or None



def _filter_suppressed_items(path: Path, *, items: list[CollectedItem], trigger_family: str, occurred_at: str) -> list[CollectedItem]:
    suppressed_subjects = list_active_suppression_subjects(
        path,
        trigger_family=trigger_family,
        occurred_at=occurred_at,
    )
    if not suppressed_subjects:
        return items
    return [item for item in items if json.dumps([item.source, item.id]) not in suppressed_subjects]


def _record_suppression_history(
    path: Path,
    *,
    items: list[CollectedItem],
    trigger_family: str,
    occurred_at: str,
    run_id: str,
    cooldown_minutes: int | None = None,
) -> None:
    expires_at = _parse_timestamp(occurred_at) + timedelta(
        minutes=1440 if cooldown_minutes is None else cooldown_minutes
    )
    cooldown_expires_at = expires_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for item in items:
        record_suppression(
            path,
            run_id=run_id,
            subject=json.dumps([item.source, item.id]),
            trigger_family=trigger_family,
            reason="already_delivered_in_same_trigger_family",
            cooldown_expires_at=cooldown_expires_at,
            dismissal_status="active",
            superseded_by_higher_authority=False,
        )


def _record_feedback_from_audit_items(path: Path, *, items: list[CollectedItem], occurred_at: str, run_id: str) -> None:
    for item in items:
        if item.source != "audit_context":
            continue
        metadata = item.metadata
        for signal in ("notification_rate", "ignored_rate", "delivery_failures"):
            value = metadata.get(signal)
            if value is None:
                continue
            record_feedback(
                path,
                run_id=run_id,
                category="trigger_quality",
                subject="review.trigger_quality",
                signal=signal,
                value=str(value),
                recorded_at=occurred_at,
            )
        for subject in metadata.get("late_triggers", []):
            record_feedback(
                path,
                run_id=run_id,
                category="trigger_quality",
                subject=str(subject),
                signal="late_trigger",
                value="1",
                recorded_at=occurred_at,
            )
        for subject in metadata.get("weak_sources", []):
            record_feedback(
                path,
                run_id=run_id,
                category="source_quality",
                subject=str(subject),
                signal="weak_source",
                value="1",
                recorded_at=occurred_at,
            )


def _record_approval_actions_from_items(path: Path, *, items: list[CollectedItem], occurred_at: str, run_id: str) -> None:
    item = next(iter(items), None)
    if item is None:
        return
    fields = _parse_key_value_lines(item.body or "")
    subject = json.dumps(
        {
            "buy": fields.get("buy") or item.title or item.id,
            "preferred_store": fields.get("preferred store"),
            "link": fields.get("link"),
        },
        ensure_ascii=False,
    )
    record_approval_action(
        path,
        run_id=run_id,
        action_kind="shopping.replenishment",
        subject=subject,
        approval_boundary_reached=True,
        user_decision="pending",
        execution_result="not_executed",
        recorded_at=occurred_at,
    )


def _update_suppression_from_command(path: Path, *, suppression_id: str, command: str) -> None:
    suppression = get_suppression(path, suppression_id=suppression_id)
    if suppression is None:
        raise ValueError("suppression not found")
    _, dismissal_status, superseded_by_higher_authority = suppression
    if dismissal_status != "active":
        raise ValueError("suppression is not active")
    if command == "supersede-suppression":
        if superseded_by_higher_authority:
            raise ValueError("suppression is already superseded")
        update_suppression_superseded_flag(path, suppression_id=suppression_id, superseded_by_higher_authority=True)
        return
    updated_status = "dismissed" if command == "dismiss-suppression" else "expired"
    update_suppression_status(path, suppression_id=suppression_id, dismissal_status=updated_status)


def _update_approval_action_from_command(
    path: Path,
    *,
    action_id: str,
    command: str,
    occurred_at: str,
    execution_receipt: str | None = None,
    execution_error: str | None = None,
    execution_provider: str | None = None,
    execution_store: str | None = None,
    execution_order_id: str | None = None,
    retryable: bool = False,
) -> None:
    if command == "complete-action" and (execution_error or retryable):
        raise ValueError("complete-action does not accept --execution-error or --retryable")
    if command == "failed-action" and (execution_receipt or execution_order_id):
        raise ValueError("failed-action does not accept --execution-receipt or --execution-order-id")
    if command == "approve-action" and (
        execution_receipt or execution_error or execution_provider or execution_store or execution_order_id or retryable
    ):
        raise ValueError("approve-action does not accept execution detail flags")
    if command == "reject-action" and (
        execution_receipt or execution_error or execution_provider or execution_store or execution_order_id or retryable
    ):
        raise ValueError("reject-action does not accept execution detail flags")

    action = get_approval_action_record(path, action_id=action_id)
    if action is None:
        raise ValueError("approval action not found")
    user_decision = action["user_decision"]
    execution_result = action["execution_result"]
    if command in {"complete-action", "failed-action"}:
        if user_decision != "approved" or execution_result != "approved_pending_execution":
            raise ValueError("approval action is not awaiting execution completion")
        updated_execution_result = "executed" if command == "complete-action" else "failed"
        details_payload: dict[str, object] = {}
        if command == "complete-action":
            if execution_receipt:
                details_payload["receipt"] = execution_receipt
            if execution_provider:
                details_payload["provider"] = execution_provider
            if execution_store:
                details_payload["store"] = execution_store
            if execution_order_id:
                details_payload["order_id"] = execution_order_id
        else:
            if execution_error:
                details_payload["error"] = execution_error
            if execution_provider:
                details_payload["provider"] = execution_provider
            if retryable:
                details_payload["retryable"] = True
        execution_details = json.dumps(details_payload, ensure_ascii=False) if details_payload else None
        update_approval_action(
            path,
            action_id=action_id,
            user_decision="approved",
            execution_result=updated_execution_result,
            execution_details=execution_details,
            recorded_at=occurred_at,
        )
        record_feedback(
            path,
            run_id=action["run_id"],
            category="action_execution",
            subject=action["action_kind"],
            signal=updated_execution_result,
            value="1",
            recorded_at=occurred_at,
        )
        if command == "failed-action" and retryable:
            record_feedback(
                path,
                run_id=action["run_id"],
                category="action_execution",
                subject=action["action_kind"],
                signal="retryable_failure",
                value="1",
                recorded_at=occurred_at,
            )
        return
    if user_decision != "pending":
        raise ValueError("approval action is not pending")
    if command == "approve-action":
        update_approval_action(
            path,
            action_id=action_id,
            user_decision="approved",
            execution_result="approved_pending_execution",
            execution_details=None,
            recorded_at=occurred_at,
        )
        return
    if command == "reject-action":
        update_approval_action(
            path,
            action_id=action_id,
            user_decision="rejected",
            execution_result="cancelled",
            execution_details=None,
            recorded_at=occurred_at,
        )
        return
    raise ValueError(f"unsupported action update command: {command}")


def _build_leave_now_warning(args: argparse.Namespace) -> str | None:
    items = _build_event_trigger_items("calendar.leave_now.default", args)
    return render_leave_now_warning(items, now=_parse_timestamp(args.now) if args.now else datetime.now(timezone.utc))


def _build_mail_operational(args: argparse.Namespace) -> str | None:
    items = _build_event_trigger_items("mail.operational.default", args)
    return render_mail_operational_warning(items)


def _build_shopping_replenishment(args: argparse.Namespace) -> str | None:
    items = _build_event_trigger_items("shopping.replenishment.default", args)
    return render_shopping_replenishment_action_prep(items)


def _build_feed_update(args: argparse.Namespace) -> str | None:
    items = _build_event_trigger_items("feed.update.default", args)
    return render_feed_update_nudge(items)


def _build_location_arrival(args: argparse.Namespace) -> str | None:
    items = _build_event_trigger_items("location.arrival.default", args)
    return render_location_arrival_mini_digest(items)


def _build_location_walk(args: argparse.Namespace) -> str | None:
    items = _collect_location_context_items(location_fixture=getattr(args, "location_fixture", None))
    return render_location_walk_nudge(items)


def _collect_location_context_items(
    *,
    location_fixture: Path | None,
    error_handler: Callable[[str, str], None] | None = None,
    success_handler: Callable[[str], None] | None = None,
) -> list[CollectedItem]:
    return LocationContextConnector(
        runner=(lambda: load_location_context_fixture(location_fixture)) if location_fixture is not None else None,
        error_handler=error_handler,
        success_handler=success_handler,
    ).collect()


def _build_review_trigger_quality(args: argparse.Namespace) -> str | None:
    items = _build_event_trigger_items("review.trigger_quality.default", args)
    return render_trigger_quality_review(items)


def _build_gap_window(args: argparse.Namespace) -> str | None:
    items = _build_event_trigger_items("calendar.gap_window.default", args)
    now = _parse_timestamp(args.now) if args.now else datetime.now(timezone.utc)
    return render_gap_window_mini_digest(items, now=now)


def _build_feed_update_deep_brief(args: argparse.Namespace) -> str | None:
    items = _build_event_trigger_items("feed.update.expert_depth", args)
    return render_feed_update_deep_brief(items)


def _build_feed_update_source_audit(args: argparse.Namespace) -> str | None:
    items = _build_event_trigger_items("feed.update.source_audit", args)
    return render_feed_update_source_audit(items)


def _build_event_trigger_items(profile_id: str, args: argparse.Namespace) -> list[CollectedItem]:
    profile = get_trigger_profile(profile_id)
    source_registry = load_source_registry(args.source_registry or DEFAULT_SOURCE_REGISTRY)
    feed_fetcher = _build_feed_fetcher(getattr(args, "feed_fixture", None))
    search_fetcher = _build_feed_fetcher(getattr(args, "search_fixture", None))
    calendar_fixture = getattr(args, "calendar_fixture", None)
    gmail_fixture = getattr(args, "gmail_fixture", None)
    location_fixture = getattr(args, "location_fixture", None)
    audit_fixture = getattr(args, "audit_fixture", None)
    notes_path = getattr(args, "notes", None)
    chatgpt_history_path = getattr(args, "chatgpt_history", None)
    calendar_runner = _build_json_runner(calendar_fixture)
    gmail_runner = _build_json_runner(gmail_fixture)
    occurred_at = (getattr(args, "now", None) or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    audit_runner = _build_audit_runner(
        audit_fixture=audit_fixture,
        state_db=getattr(args, "state_db", None),
        source_registry=source_registry,
        occurred_at=occurred_at,
    )
    trigger = TriggerEvent(
        id=f"event:{profile.id}",
        type=profile.event_type,
        profile_id=profile.id,
        occurred_at=occurred_at,
        scope=TriggerScope(),
    )
    connectors = {
        "feed_registry": BoundConnector(
            lambda: FeedRegistryConnector(fetcher=feed_fetcher).collect(source_registry)
        ),
        "known_source_search": BoundConnector(
            lambda: KnownSourceSearchConnector(fetcher=search_fetcher).collect(source_registry)
        ),
    }
    if location_fixture is not None or profile.id == "location.walk.default":
        connectors["location_context"] = BoundConnector(
            lambda: LocationContextConnector(
                runner=(lambda: load_location_context_fixture(location_fixture)) if location_fixture is not None else None
            ).collect()
        )
    if calendar_runner is not None:
        connectors["google_calendar"] = BoundConnector(lambda: GoogleCalendarConnector(runner=calendar_runner).collect())
    if gmail_runner is not None:
        connectors["gmail"] = BoundConnector(lambda: GmailConnector(runner=gmail_runner).collect())
    if chatgpt_history_path is not None:
        connectors["chatgpt_history"] = BoundConnector(
            lambda: ChatGPTHistoryConnector().collect(chatgpt_history_path)
        )
    if notes_path is not None:
        connectors["notes"] = BoundConnector(lambda: NotesConnector().collect(notes_path))
    if audit_runner is not None:
        connectors["audit_context"] = BoundConnector(
            lambda: AuditContextConnector(runner=audit_runner).collect()
        )
    return collect_for_trigger(trigger, profile, connectors)


def _build_digest_with_source_errors(command: str, args: argparse.Namespace) -> tuple[list[CollectedItem], dict[str, str], set[str]]:
    profile_id = {
        "morning-digest": "digest.morning.default",
        "evening-digest": "digest.evening.default",
    }[command]
    profile = get_trigger_profile(profile_id)
    source_registry = load_source_registry(args.source_registry or DEFAULT_SOURCE_REGISTRY)
    feed_fetcher = _build_feed_fetcher(args.feed_fixture)
    search_fetcher = _build_feed_fetcher(args.search_fixture)
    calendar_fixture = getattr(args, "calendar_fixture", None)
    gmail_fixture = getattr(args, "gmail_fixture", None)
    calendar_runner = _build_json_runner(calendar_fixture)
    gmail_runner = _build_json_runner(gmail_fixture)
    source_errors: dict[str, str] = {}
    successful_sources: set[str] = set()
    occurred_at = (getattr(args, "now", None) or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    trigger = TriggerEvent(
        id="scheduled:digest.morning.default",
        type=profile.event_type,
        profile_id=profile.id,
        occurred_at=occurred_at,
        scope=TriggerScope(),
    )
    connectors = {
        "feed_registry": BoundConnector(
            lambda: _build_source_registry_connector(
                FeedRegistryConnector,
                fetcher=feed_fetcher,
                error_handler=lambda entry_id, message: source_errors.__setitem__(entry_id, message),
                success_handler=lambda entry_id: successful_sources.add(entry_id),
            ).collect(source_registry)
        ),
        "known_source_search": BoundConnector(
            lambda: _build_source_registry_connector(
                KnownSourceSearchConnector,
                fetcher=search_fetcher,
                error_handler=lambda entry_id, message: source_errors.__setitem__(entry_id, message),
                success_handler=lambda entry_id: successful_sources.add(entry_id),
            ).collect(source_registry)
        ),
    }
    if calendar_runner is not None:
        connectors["google_calendar"] = BoundConnector(lambda: GoogleCalendarConnector(runner=calendar_runner).collect())
    if gmail_runner is not None:
        connectors["gmail"] = BoundConnector(lambda: GmailConnector(runner=gmail_runner).collect())
    if getattr(args, "chatgpt_history", None) is not None:
        connectors["chatgpt_history"] = BoundConnector(
            lambda: ChatGPTHistoryConnector().collect(args.chatgpt_history)
        )
    if args.x_signals:
        requested_signal_types = [value.strip() for value in args.x_signals.split(",") if value.strip()]
        signal_types = _effective_x_signal_types(
            args,
            requested_signal_types,
            occurred_at=_occurred_at_for_command(command, args),
        )
        if signal_types:
            connectors["x_signals"] = BoundConnector(
                lambda: _collect_x_signals_with_error_capture(
                    signal_types,
                    expected_username=getattr(args, "x_expected_username", None),
                    since_ids=_x_since_ids_from_state(args, signal_types),
                    source_errors=source_errors,
                    successful_sources=successful_sources,
                )
            )
    if getattr(args, "x_activity_likes_file", None) is not None:
        connectors["x_activity_likes"] = BoundConnector(
            lambda: _collect_x_activity_likes_with_error_capture(
                args.x_activity_likes_file,
                getattr(args, "x_expected_user_id", None),
                source_errors=source_errors,
                successful_sources=successful_sources,
            )
        )
    if getattr(args, "grok_history", None) is not None:
        connectors["grok_history"] = BoundConnector(
            lambda: GrokHistoryConnector().collect(args.grok_history)
        )
    if args.hermes_history is not None:
        connectors["hermes_history"] = BoundConnector(
            lambda: HermesHistoryConnector().collect(args.hermes_history)
        )
    if args.notes is not None:
        connectors["notes"] = BoundConnector(lambda: NotesConnector().collect(args.notes))
    return collect_for_trigger(trigger, profile, connectors), source_errors, successful_sources


def _build_source_registry_connector(
    connector_cls: type,
    *,
    fetcher: Callable[[str], str] | None,
    error_handler: Callable[[str, str], None],
    success_handler: Callable[[str], None],
):
    parameters = inspect.signature(connector_cls.__init__).parameters
    kwargs: dict[str, object] = {"fetcher": fetcher}
    if "error_handler" in parameters:
        kwargs["error_handler"] = error_handler
    if "success_handler" in parameters:
        kwargs["success_handler"] = success_handler
    return connector_cls(**kwargs)


def _build_feed_fetcher(feed_fixture: Path | None) -> Callable[[str], str] | None:
    if feed_fixture is None:
        return None
    payload = feed_fixture.read_text()
    return lambda url: payload


def _build_json_runner(path: Path | None) -> Callable[[], list[dict[str, object]]] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text())
    return lambda: payload


def _build_audit_runner(
    *,
    audit_fixture: Path | None,
    state_db: Path | None,
    source_registry: list[SourceRegistryEntry],
    occurred_at: str,
) -> Callable[[], dict[str, object]] | None:
    if audit_fixture is not None:
        return lambda: load_audit_context_fixture(audit_fixture)
    if state_db is not None:
        return lambda: _build_runtime_trigger_quality_audit(
            state_db,
            source_registry=source_registry,
            occurred_at=occurred_at,
        )
    return None


def _build_runtime_trigger_quality_audit(
    path: Path,
    *,
    source_registry: list[SourceRegistryEntry],
    occurred_at: str,
) -> dict[str, object]:
    observed_at = _parse_timestamp(occurred_at)
    stale_inputs: list[str] = []
    weak_sources: list[str] = []
    source_threshold = timedelta(hours=24)
    location_threshold = timedelta(hours=1)

    with sqlite3.connect(path) as connection:
        source_rows = {
            registry_id: last_poll_at
            for registry_id, last_poll_at in connection.execute(
                "SELECT registry_id, last_poll_at FROM source_registry_state"
            ).fetchall()
        }
        connector_rows = {
            connector_id: last_success_at
            for connector_id, last_success_at in connection.execute(
                "SELECT connector_id, last_success_at FROM connector_cursors"
            ).fetchall()
        }

    for entry in source_registry:
        if entry.acquisition_mode not in {"rss_poll", "atom_poll", "known_source_search"}:
            continue
        last_poll_at = source_rows.get(entry.id)
        if last_poll_at is None:
            stale_inputs.append(f"{entry.id}: never_polled")
            weak_sources.append(entry.id)
            continue
        if observed_at - _parse_timestamp(last_poll_at) > source_threshold:
            stale_inputs.append(f"{entry.id}: stale_since={last_poll_at}")
            weak_sources.append(entry.id)

    last_location_success = connector_rows.get("location_context")
    if last_location_success is None:
        stale_inputs.append("location_context: never_collected")
        weak_sources.append("location_context")
    elif observed_at - _parse_timestamp(last_location_success) > location_threshold:
        stale_inputs.append(f"location_context: stale_since={last_location_success}")
        weak_sources.append("location_context")

    payload: dict[str, object] = {}
    if stale_inputs:
        payload["stale_inputs"] = stale_inputs
        payload["weak_sources"] = weak_sources
    return payload


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
