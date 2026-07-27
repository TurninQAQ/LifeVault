from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

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
    resume.add_argument("--text", default=None, help="Natural language supplement for missing fields")
    resume.add_argument(
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
    if turn.reminder:
        print("Reminder:")
        print_json(turn.reminder)
    if turn.saved_record_id:
        print(f"Saved record: {turn.saved_record_id}")
    if turn.reminder_id:
        print(f"Created reminder: {turn.reminder_id}")
    if turn.errors:
        print("Errors: " + "; ".join(turn.errors))


def print_json(data: dict[str, Any]) -> None:
    import json

    print(json.dumps(data, ensure_ascii=False, indent=2))


def require_mcp_ok(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok"):
        return result
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        print(f"MCP {tool_name} failed: {error.get('code', 'unknown_error')}: {error.get('message', '')}")
    else:
        print(f"MCP {tool_name} failed.")
    raise SystemExit(2)


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
        return {"action": "confirm" if ask_yes_no("创建这条提醒？") else "skip"}
    return {"action": "cancel"}


def ask_yes_no(prompt: str) -> bool:
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes", "是", "确认"}


if __name__ == "__main__":
    main()
