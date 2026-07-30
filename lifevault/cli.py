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
from lifevault.config import get_settings
from lifevault.mcp_server.client import InProcessPersonalVaultMcpClient
from lifevault.models.schemas import GraphTurn, RecordStatus, ReminderStatus
from lifevault.storage.repository import VaultRepository
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

    repository = VaultRepository(settings.database_path)
    mcp_client = InProcessPersonalVaultMcpClient(settings, repository)
    agent = LifeVaultAgent(settings, repository, mcp_client=mcp_client)
    graph_agent = GraphAgent(settings, repository, mcp_client=mcp_client)

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
        result = mcp_client.update_record_status(
            args.record_id,
            args.new_status,
            args.expected_version,
        )
        record = require_mcp_ok("update_record_status", result)["record"]
        print(f"Updated {record['id']} to {record['status']}, version={record['version']}")
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
