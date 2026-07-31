from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from lifevault.agent.graph_agent import GraphAgent
from lifevault.agent.service import LifeVaultAgent
from lifevault.agent.update_graph_agent import RecordUpdateGraphAgent
from lifevault.config import get_settings
from lifevault.mcp_server.client import InProcessPersonalVaultMcpClient
from lifevault.models.schemas import (
    GraphTurn,
    RecordStatus,
    RecordUpdateTurn,
    ReminderStatus,
)
from lifevault.storage.repository import VaultRepository
from lifevault.tools.idempotency import stable_key
from lifevault.worker.reminder_worker import ReminderWorker


def main() -> None:
    parser = argparse.ArgumentParser(description="LifeVault local reminder assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Initialize the SQLite database")

    add = sub.add_parser("add", help="Create a record draft from natural language")
    add.add_argument("text", help="Natural language input")
    add.add_argument("--yes", action="store_true", help="Confirm record and reminder creation")
    add.add_argument("--no-reminder", action="store_true", help="Skip reminder creation")
    add.add_argument("--thread-id", default=None, help="Optional LangGraph thread id")

    resume = sub.add_parser("resume", help="Resume an interrupted create-record graph")
    resume.add_argument("thread_id")
    resume_input = resume.add_mutually_exclusive_group()
    resume_input.add_argument(
        "--text",
        default=None,
        help="Natural language supplement for missing fields",
    )
    resume_input.add_argument(
        "--corrections-json",
        default=None,
        help="JSON object with structured record-review corrections",
    )
    resume_input.add_argument(
        "--action",
        choices=["confirm", "continue", "cancel", "skip"],
        default=None,
        help="Action for duplicate, record, or reminder interrupts",
    )

    state = sub.add_parser("state", help="Show a graph thread state")
    state.add_argument("thread_id")

    natural_edit = sub.add_parser("edit", help="Update a saved record from natural language")
    natural_edit.add_argument("text")
    natural_edit.add_argument("--record-id", default=None, help="Preselect a record from its own UI/context")
    natural_edit.add_argument("--thread-id", default=None)
    natural_edit.add_argument("--yes", action="store_true", help="Confirm after target selection and preview")

    edit_resume = sub.add_parser("edit-resume", help="Resume an interrupted natural update")
    edit_resume.add_argument("thread_id")
    edit_resume.add_argument("--text", default=None)
    edit_resume.add_argument("--record-id", default=None)
    edit_resume.add_argument("--changes-json", default=None)
    edit_resume.add_argument("--target-status", default=None)
    edit_resume.add_argument(
        "--action",
        choices=["confirm", "cancel"],
        default=None,
    )

    edit_state = sub.add_parser("edit-state", help="Show a natural-update graph state")
    edit_state.add_argument("thread_id")

    search = sub.add_parser("search", help="Search saved records")
    search.add_argument("text", help="Search query")

    list_records = sub.add_parser("list", help="List records")
    list_records.add_argument("--query", default=None)

    subscriptions = sub.add_parser("subscriptions", help="List upcoming subscription renewals")
    subscriptions.add_argument("--days", type=int, default=30, help="Renewal window in days")
    subscriptions.add_argument("--exclude-auto-renew", action="store_true", help="Hide auto-renewing subscriptions")
    subscriptions.add_argument("--limit", type=int, default=20)

    reminders = sub.add_parser("reminders", help="List reminders")
    reminders.add_argument("--status", choices=[status.value for status in ReminderStatus], default=None)

    audit = sub.add_parser("audit", help="List immutable audit events")
    audit.add_argument("--actor", default=None)
    audit.add_argument("--action", default=None)
    audit.add_argument("--result", choices=["ok", "rejected", "failed"], default=None)
    audit.add_argument("--before-id", type=int, default=None)
    audit.add_argument("--limit", type=int, default=50)
    audit.add_argument("--json", action="store_true", help="Print the MCP response as JSON")

    preferences = sub.add_parser("preferences", help="View or update reminder preferences")
    preferences_sub = preferences.add_subparsers(dest="preferences_action", required=True)
    preferences_sub.add_parser("show", help="Show current preferences")
    preferences_set = preferences_sub.add_parser("set", help="Update selected preferences")
    preferences_set.add_argument("--default-time", default=None)
    preferences_set.add_argument("--advance-days", type=int, default=None)
    preferences_set.add_argument("--quiet-start", default=None)
    preferences_set.add_argument("--quiet-end", default=None)
    preferences_set.add_argument("--clear-quiet-hours", action="store_true")
    preferences_set.add_argument("--yes", action="store_true", help="Confirm the preference update")

    snooze = sub.add_parser("snooze-reminder", help="Snooze a reminder")
    snooze.add_argument("reminder_id")
    snooze_group = snooze.add_mutually_exclusive_group()
    snooze_group.add_argument("--minutes", type=int, default=None, help="Minutes from now. Defaults to 60.")
    snooze_group.add_argument("--at", default=None, help="New scheduled datetime in ISO format")

    update = sub.add_parser("status", help="Update a record status")
    update.add_argument("record_id")
    update.add_argument("new_status", choices=[status.value for status in RecordStatus])
    update.add_argument("expected_version", type=int)
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--yes", action="store_true")
    update.add_argument("--idempotency-key", default=None)

    edit = sub.add_parser("update", help="Preview or apply a typed partial record update")
    edit.add_argument("record_id")
    edit.add_argument("expected_version", type=int)
    edit.add_argument(
        "--changes-json",
        required=True,
        help="JSON object containing only fields to update; null clears optional fields",
    )
    edit.add_argument("--dry-run", action="store_true", help="Preview without writing")
    edit.add_argument("--yes", action="store_true", help="Confirm the record update")
    edit.add_argument(
        "--confirm-duplicate",
        action="store_true",
        help="Confirm update even when possible duplicates are found",
    )
    edit.add_argument(
        "--idempotency-key",
        default=None,
        help="Optional stable retry key; derived from the request by default",
    )

    worker = sub.add_parser("worker", help="Run reminder worker")
    worker.add_argument("--once", action="store_true", help="Scan once and exit")
    worker.add_argument("--interval", type=int, default=60)

    sub.add_parser("mcp-server", help="Run the Personal Vault MCP server over stdio")

    mcp_smoke = sub.add_parser("mcp-smoke", help="Run a stdio MCP smoke test")
    mcp_smoke.add_argument("--db", type=Path, default=None, help="Optional SQLite database path")

    eval_cmd = sub.add_parser("eval", help="Run extraction eval cases")
    eval_cmd.add_argument("--examples", type=Path, default=None, help="JSONL eval examples path")
    eval_cmd.add_argument("--use-qwen", action="store_true", help="Use configured local Qwen instead of fallback")
    eval_cmd.add_argument("--json-out", type=Path, default=None, help="Optional JSON report output path")

    eval_updates = sub.add_parser("eval-updates", help="Run natural record-update eval cases")
    eval_updates.add_argument("--examples", type=Path, default=None)
    eval_updates.add_argument("--use-qwen", action="store_true")
    eval_updates.add_argument("--json-out", type=Path, default=None)

    args = parser.parse_args()
    settings = get_settings()

    if args.command == "mcp-server":
        from lifevault.mcp_server.server import main as run_mcp_server

        run_mcp_server()
        return

    if args.command == "mcp-smoke":
        from lifevault.mcp_server.smoke import run_smoke

        result = asyncio.run(run_smoke(args.db, cwd=Path.cwd()))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "eval":
        from lifevault.eval.runner import DEFAULT_EXAMPLES_PATH, format_summary, run_eval, write_json_report

        report = run_eval(
            settings,
            examples_path=args.examples or DEFAULT_EXAMPLES_PATH,
            use_qwen=args.use_qwen,
        )
        print(format_summary(report))
        if args.json_out:
            write_json_report(report, args.json_out)
            print(f"Wrote JSON report: {args.json_out}")
        return

    if args.command == "eval-updates":
        from lifevault.eval.update_runner import (
            DEFAULT_UPDATE_EXAMPLES_PATH,
            format_update_summary,
            run_update_eval,
            write_update_json_report,
        )

        report = run_update_eval(
            settings,
            examples_path=args.examples or DEFAULT_UPDATE_EXAMPLES_PATH,
            use_qwen=args.use_qwen,
        )
        print(format_update_summary(report))
        if args.json_out:
            write_update_json_report(report, args.json_out)
            print(f"Wrote JSON report: {args.json_out}")
        return

    repository = VaultRepository(settings.database_path)
    mcp_client = InProcessPersonalVaultMcpClient(settings, repository)
    agent = LifeVaultAgent(settings, repository, mcp_client=mcp_client)
    graph_agent = GraphAgent(settings, repository, mcp_client=mcp_client)
    update_graph_agent = RecordUpdateGraphAgent(
        settings,
        repository,
        mcp_client=mcp_client,
    )

    if args.command == "init-db":
        print(f"Initialized database: {settings.database_path}")
        return

    if args.command == "add":
        turn = graph_agent.start_create_record(args.text, thread_id=args.thread_id)
        turn = drive_graph_interactively(turn, graph_agent, yes=args.yes, no_reminder=args.no_reminder)
        print_graph_turn(turn)
        return

    if args.command == "resume":
        payload: dict[str, Any] | str
        if args.text is not None:
            payload = {"text": args.text}
        elif args.corrections_json is not None:
            try:
                corrections = json.loads(args.corrections_json)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid --corrections-json: {exc.msg}") from exc
            if not isinstance(corrections, dict):
                raise SystemExit("--corrections-json must contain a JSON object")
            payload = {"action": "apply", "corrections": corrections}
        elif args.action is not None:
            payload = {"action": args.action}
        else:
            existing = graph_agent.get_state(args.thread_id)
            if existing is None:
                print(f"No graph state found for thread: {args.thread_id}")
                raise SystemExit(2)
            print_graph_turn(existing)
            payload = prompt_payload(existing)
        turn = graph_agent.resume(args.thread_id, payload)
        turn = drive_graph_interactively(turn, graph_agent)
        print_graph_turn(turn)
        return

    if args.command == "state":
        turn = graph_agent.get_state(args.thread_id)
        if turn is None:
            print(f"No graph state found for thread: {args.thread_id}")
            raise SystemExit(2)
        print_graph_turn(turn)
        return

    if args.command == "edit":
        turn = update_graph_agent.start(
            args.text,
            preselected_record_id=args.record_id,
            thread_id=args.thread_id,
        )
        turn = drive_update_graph_interactively(
            turn,
            update_graph_agent,
            yes=args.yes,
        )
        print_update_turn(turn)
        return

    if args.command == "edit-resume":
        supplied = sum(
            value is not None
            for value in [
                args.text,
                args.record_id,
                args.changes_json,
                args.target_status,
                args.action,
            ]
        )
        if supplied > 1:
            raise SystemExit("Provide only one resume payload.")
        if args.text is not None:
            payload: dict[str, Any] = {"text": args.text}
        elif args.record_id is not None:
            payload = {"record_id": args.record_id}
        elif args.changes_json is not None:
            payload = {
                "action": "apply",
                "changes": parse_json_object(args.changes_json, "--changes-json"),
            }
        elif args.target_status is not None:
            payload = {"action": "apply", "target_status": args.target_status}
        elif args.action is not None:
            payload = {"action": args.action}
        else:
            existing = update_graph_agent.get_state(args.thread_id)
            if existing is None:
                print(f"No update graph state found for thread: {args.thread_id}")
                raise SystemExit(2)
            print_update_turn(existing)
            payload = prompt_update_payload(existing)
        turn = update_graph_agent.resume(args.thread_id, payload)
        turn = drive_update_graph_interactively(turn, update_graph_agent)
        print_update_turn(turn)
        return

    if args.command == "edit-state":
        turn = update_graph_agent.get_state(args.thread_id)
        if turn is None:
            print(f"No update graph state found for thread: {args.thread_id}")
            raise SystemExit(2)
        print_update_turn(turn)
        return

    if args.command == "search":
        _records, answer = agent.search(args.text)
        print(answer)
        return

    if args.command == "list":
        result = mcp_client.search_records(query=args.query)
        records = require_mcp_ok("search_records", result).get("records", [])
        for record in records:
            deadline = record.get("deadline") or "-"
            amount = record.get("amount")
            amount_text = f"{amount:g}" if amount is not None else "-"
            print(
                f"{record['id']} | v{record['version']} | {record['record_type']} | "
                f"{record['title']} | {amount_text} | {record['status']} | {deadline}"
            )
        return

    if args.command == "subscriptions":
        result = mcp_client.list_upcoming_subscriptions(
            days=args.days,
            include_auto_renew=not args.exclude_auto_renew,
            limit=args.limit,
        )
        require_mcp_ok("list_upcoming_subscriptions", result)
        print(f"Upcoming subscriptions: {result['date_from']} -> {result['date_to']}")
        for record in result["records"]:
            deadline = record.get("deadline") or "-"
            amount = record.get("amount")
            amount_text = f"{amount:g} {record.get('currency', 'CNY')}" if amount is not None else "金额未知"
            details = record.get("details") or {}
            cycle = details.get("billing_cycle") or "-"
            auto_renew = details.get("auto_renew")
            auto_renew_text = "-" if auto_renew is None else ("auto" if auto_renew else "manual")
            print(f"{record['id']} | {record['title']} | {amount_text} | {cycle} | {auto_renew_text} | {deadline}")
        return

    if args.command == "reminders":
        result = mcp_client.list_reminders(status=args.status)
        reminders_list = require_mcp_ok("list_reminders", result).get("reminders", [])
        for reminder in reminders_list:
            print(
                f"{reminder['id']} | {reminder['record_id']} | {reminder['status']} | "
                f"{reminder['scheduled_at']} | {reminder['message']}"
            )
        return

    if args.command == "audit":
        result = mcp_client.list_audit_logs(
            actor=args.actor,
            action=args.action,
            result=args.result,
            before_id=args.before_id,
            limit=args.limit,
        )
        audit_logs = require_mcp_ok("list_audit_logs", result).get("audit_logs", [])
        if args.json:
            print_json(result)
            return
        for log in audit_logs:
            print(
                f"{log['id']} | {log['created_at']} | {log['actor']} | "
                f"{log['action']} | {log['result']} | {log.get('target_id') or '-'} | "
                f"{log.get('params_summary') or '-'}"
            )
        return

    if args.command == "preferences":
        if args.preferences_action == "show":
            result = mcp_client.get_preferences()
            preference = require_mcp_ok("get_preferences", result)["preference"]
            print_preferences(preference)
            return

        patch: dict[str, Any] = {}
        if args.default_time is not None:
            patch["default_time"] = args.default_time
        if args.advance_days is not None:
            patch["default_advance_days"] = args.advance_days
        quiet_values_supplied = args.quiet_start is not None or args.quiet_end is not None
        if args.clear_quiet_hours and quiet_values_supplied:
            raise SystemExit("--clear-quiet-hours cannot be combined with --quiet-start or --quiet-end")
        if quiet_values_supplied and (args.quiet_start is None or args.quiet_end is None):
            raise SystemExit("--quiet-start and --quiet-end must be provided together")
        if args.clear_quiet_hours:
            patch["quiet_hours_start"] = None
            patch["quiet_hours_end"] = None
        elif quiet_values_supplied:
            patch["quiet_hours_start"] = args.quiet_start
            patch["quiet_hours_end"] = args.quiet_end
        if not patch:
            raise SystemExit("No preference fields were provided.")
        if not args.yes and not ask_yes_no("更新这些偏好？"):
            print("Preference update cancelled.")
            return

        result = mcp_client.update_preferences(patch, user_confirmed=True)
        outcome = require_mcp_ok("update_preferences", result)
        print("Preferences updated." if outcome["changed"] else "Preferences unchanged.")
        print_preferences(outcome["preference"])
        return

    if args.command == "snooze-reminder":
        scheduled_at = parse_snooze_scheduled_at(args.at, args.minutes, settings.default_timezone)
        result = mcp_client.snooze_reminder(args.reminder_id, scheduled_at.isoformat())
        reminder = require_mcp_ok("snooze_reminder", result)["reminder"]
        print(f"Snoozed {args.reminder_id} to {reminder['scheduled_at']} as {reminder['id']}")
        return

    if args.command == "status":
        preview = mcp_client.preview_record_status_update(
            args.record_id,
            args.new_status,
            args.expected_version,
        )
        if not preview.get("ok") and (preview.get("error") or {}).get("code") == "no_changes":
            print("Record status is already unchanged.")
            return
        preview = require_mcp_ok("preview_record_status_update", preview)
        print_json(
            {
                "record": preview["record"],
                "reminders_to_cancel": preview.get("reminders_to_cancel") or [],
                "warnings": preview.get("warnings") or [],
            }
        )
        if args.dry_run:
            return
        if not args.yes and not ask_yes_no("提交这次状态更新？"):
            print("Status update cancelled.")
            return
        key = args.idempotency_key or stable_key(
            "cli-record-status-update",
            args.record_id,
            args.expected_version,
            args.new_status,
        )
        result = mcp_client.update_record_status(
            args.record_id,
            args.new_status,
            args.expected_version,
            key,
            user_confirmed=True,
        )
        record = require_mcp_ok("update_record_status", result)["record"]
        print(f"Updated {record['id']} to {record['status']}, version={record['version']}")
        return

    if args.command == "update":
        changes = parse_json_object(args.changes_json, "--changes-json")
        preview_result = mcp_client.preview_record_update(
            args.record_id,
            changes,
            args.expected_version,
        )
        preview = require_mcp_ok("preview_record_update", preview_result)
        print_record_update_preview(preview)
        if args.dry_run:
            return

        duplicates = preview.get("duplicate_candidates") or []
        duplicate_confirmed = args.confirm_duplicate
        if duplicates and not duplicate_confirmed:
            if args.yes:
                raise SystemExit(
                    "Possible duplicates found; pass --confirm-duplicate to confirm explicitly."
                )
            duplicate_confirmed = ask_yes_no("仍然更新这条疑似重复记录？")
            if not duplicate_confirmed:
                print("Record update cancelled.")
                return
        if not args.yes and not ask_yes_no("提交这次记录更新？"):
            print("Record update cancelled.")
            return

        idempotency_key = args.idempotency_key or stable_key(
            "cli-record-update",
            args.record_id,
            args.expected_version,
            json.dumps(changes, ensure_ascii=False, sort_keys=True),
            duplicate_confirmed,
        )
        update_result = mcp_client.update_record(
            args.record_id,
            changes,
            args.expected_version,
            idempotency_key,
            user_confirmed=True,
            duplicate_confirmed=duplicate_confirmed,
        )
        outcome = require_mcp_ok("update_record", update_result)
        record = outcome["record"]
        print(
            f"Updated {record['id']} to version={record['version']}; "
            f"cancelled_reminders={len(outcome.get('cancelled_reminders') or [])}; "
            f"created_reminders={len(outcome.get('created_reminders') or [])}"
        )
        return

    if args.command == "worker":
        worker_service = ReminderWorker(settings, repository)
        if args.once:
            count = worker_service.run_once(datetime.now().astimezone())
            print(f"Processed reminders: {count}")
        else:
            worker_service.run_forever(interval_seconds=args.interval)
        return


def drive_graph_interactively(
    turn: GraphTurn,
    graph_agent: GraphAgent,
    yes: bool = False,
    no_reminder: bool = False,
) -> GraphTurn:
    while turn.status == "interrupted":
        print_graph_turn(turn)
        if yes:
            payload = auto_payload(turn, no_reminder=no_reminder)
            if payload is None:
                return turn
        else:
            payload = prompt_payload(turn)
        turn = graph_agent.resume(turn.thread_id, payload)
    return turn


def drive_update_graph_interactively(
    turn: RecordUpdateTurn,
    graph_agent: RecordUpdateGraphAgent,
    yes: bool = False,
) -> RecordUpdateTurn:
    while turn.status == "interrupted":
        print_update_turn(turn)
        if yes and turn.interrupt_type == "update_confirmation":
            payload = {"action": "confirm"}
        else:
            payload = prompt_update_payload(turn)
        turn = graph_agent.resume(turn.thread_id, payload)
    return turn


def print_update_turn(turn: RecordUpdateTurn) -> None:
    print(f"Thread: {turn.thread_id}")
    print(f"Status: {turn.status}")
    if turn.interrupt_type:
        print(f"Interrupt: {turn.interrupt_type}")
    if turn.prompt:
        print(turn.prompt)
    if turn.target_query:
        print("Target query:")
        print_json(turn.target_query)
    if turn.candidates:
        print("Candidates:")
        for index, candidate in enumerate(turn.candidates, start=1):
            print(
                f"{index}. {candidate.get('id')} | {candidate.get('record_type')} | "
                f"{candidate.get('title')} | {candidate.get('status')} | "
                f"v{candidate.get('version')}"
            )
    if turn.record:
        print("Selected record:")
        print_json(turn.record)
    if turn.changes:
        print("Changes:")
        print_json(turn.changes)
    if turn.target_status:
        print(f"Target status: {turn.target_status}")
    if turn.preview:
        print("Preview:")
        print_json(turn.preview)
    if turn.no_changes:
        print("No changes were needed.")
    if turn.updated_record_id:
        print(f"Updated record: {turn.updated_record_id}")
    if turn.field_errors:
        for field, messages in turn.field_errors.items():
            for message in messages:
                print(f"- {field}: {message}")
    if turn.warnings:
        print("Warnings: " + "; ".join(turn.warnings))
    if turn.errors:
        print("Errors: " + "; ".join(turn.errors))


def prompt_update_payload(turn: RecordUpdateTurn) -> dict[str, Any]:
    if turn.interrupt_type in {"missing_target", "target_not_found", "missing_update_details"}:
        return {"text": input("补充：").strip()}
    if turn.interrupt_type == "target_selection":
        selected = input("记录序号或 ID：").strip()
        if selected.isdigit() and 1 <= int(selected) <= len(turn.candidates):
            selected = str(turn.candidates[int(selected) - 1]["id"])
        return {"record_id": selected}
    if turn.interrupt_type == "update_confirmation":
        return {"action": "confirm" if ask_yes_no("提交这次记录更新？") else "cancel"}
    return {"action": "cancel"}


def print_graph_turn(turn: GraphTurn) -> None:
    print(f"Thread: {turn.thread_id}")
    print(f"Status: {turn.status}")
    if turn.interrupt_type:
        print(f"Interrupt: {turn.interrupt_type}")
    if turn.prompt:
        print(turn.prompt)
    if turn.missing_fields:
        print("Missing fields: " + ", ".join(turn.missing_fields))
    if turn.candidate:
        print("Candidate:")
        print_json(turn.candidate)
    if turn.duplicate_candidates:
        print("Possible duplicates:")
        for duplicate in turn.duplicate_candidates:
            print(
                f"- {duplicate.get('record_id')} {duplicate.get('title')} "
                f"{duplicate.get('score', 0):.2f}: {duplicate.get('reason')}"
            )
    if turn.record:
        print("Record:")
        print_json(turn.record)
    if turn.reminders:
        print("Reminders:")
        print(json.dumps(turn.reminders, ensure_ascii=False, indent=2))
    if turn.saved_record_id:
        print(f"Saved record: {turn.saved_record_id}")
    if turn.reminder_ids:
        print("Created reminders: " + ", ".join(turn.reminder_ids))
    if turn.field_errors:
        print("Field errors:")
        for field, messages in turn.field_errors.items():
            for message in messages:
                print(f"- {field}: {message}")
    if turn.warnings:
        print("Warnings: " + "; ".join(turn.warnings))
    if turn.errors:
        print("Errors: " + "; ".join(turn.errors))


def print_json(data: dict[str, Any]) -> None:
    import json

    print(json.dumps(data, ensure_ascii=False, indent=2))


def print_preferences(preference: dict[str, Any]) -> None:
    print(f"default_time: {preference['default_time']}")
    print(f"default_advance_days: {preference['default_advance_days']}")
    print(f"quiet_hours_start: {preference.get('quiet_hours_start') or '-'}")
    print(f"quiet_hours_end: {preference.get('quiet_hours_end') or '-'}")


def parse_json_object(raw_value: str, option_name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid {option_name}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{option_name} must contain a JSON object")
    return value


def print_record_update_preview(preview: dict[str, Any]) -> None:
    print("Record update preview:")
    print_json(
        {
            "record": preview.get("record"),
            "changed_fields": preview.get("changed_fields") or [],
            "reminders_to_cancel": preview.get("reminders_to_cancel") or [],
            "reminders_to_create": preview.get("reminders_to_create") or [],
            "warnings": preview.get("warnings") or [],
            "duplicate_candidates": preview.get("duplicate_candidates") or [],
        }
    )


def require_mcp_ok(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok"):
        return result
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        print(f"MCP {tool_name} failed: {error.get('code', 'unknown_error')}: {error.get('message', '')}")
    else:
        print(f"MCP {tool_name} failed.")
    raise SystemExit(2)


def parse_snooze_scheduled_at(at: str | None, minutes: int | None, timezone_name: str) -> datetime:
    if at:
        try:
            parsed = datetime.fromisoformat(at)
        except ValueError as exc:
            raise SystemExit(f"Invalid --at datetime: {at}") from exc
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=ZoneInfo(timezone_name))
        return parsed

    delay_minutes = 60 if minutes is None else minutes
    if delay_minutes <= 0:
        raise SystemExit("--minutes must be positive")
    return datetime.now(ZoneInfo(timezone_name)) + timedelta(minutes=delay_minutes)


def auto_payload(turn: GraphTurn, no_reminder: bool) -> dict[str, str] | None:
    if turn.interrupt_type == "missing_fields":
        return None
    if turn.interrupt_type == "duplicate_review":
        return {"action": "continue"}
    if turn.interrupt_type == "record_confirmation":
        return {"action": "confirm"}
    if turn.interrupt_type == "reminder_confirmation":
        return {"action": "skip" if no_reminder else "confirm"}
    return None


def prompt_payload(turn: GraphTurn) -> dict[str, str]:
    if turn.interrupt_type == "missing_fields":
        return {"text": input("补充：").strip()}
    if turn.interrupt_type == "duplicate_review":
        return {"action": "continue" if ask_yes_no("继续保存新记录？") else "cancel"}
    if turn.interrupt_type == "record_confirmation":
        return {"action": "confirm" if ask_yes_no("保存这条记录？") else "cancel"}
    if turn.interrupt_type == "reminder_confirmation":
        return {"action": "confirm" if ask_yes_no("创建这些提醒？") else "skip"}
    return {"action": "cancel"}


def ask_yes_no(prompt: str) -> bool:
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes", "是", "确认"}


if __name__ == "__main__":
    main()
