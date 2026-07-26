from __future__ import annotations

import argparse
from datetime import datetime

from lifevault.agent.service import ConfirmationRequired, LifeVaultAgent
from lifevault.config import get_settings
from lifevault.models.schemas import DraftResult, RecordStatus, ReminderStatus
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

    search = sub.add_parser("search", help="Search saved records")
    search.add_argument("text", help="Search query")

    list_records = sub.add_parser("list", help="List records")
    list_records.add_argument("--query", default=None)

    reminders = sub.add_parser("reminders", help="List reminders")
    reminders.add_argument("--status", choices=[status.value for status in ReminderStatus], default=None)

    update = sub.add_parser("status", help="Update a record status")
    update.add_argument("record_id")
    update.add_argument("new_status", choices=[status.value for status in RecordStatus])
    update.add_argument("expected_version", type=int)

    worker = sub.add_parser("worker", help="Run reminder worker")
    worker.add_argument("--once", action="store_true", help="Scan once and exit")
    worker.add_argument("--interval", type=int, default=60)

    args = parser.parse_args()
    settings = get_settings()
    repository = VaultRepository(settings.database_path)
    agent = LifeVaultAgent(settings, repository)

    if args.command == "init-db":
        print(f"Initialized database: {settings.database_path}")
        return

    if args.command == "add":
        draft = agent.create_draft(args.text)
        print_draft(draft)
        if not draft.is_ready_to_save:
            raise SystemExit(2)
        confirmed = args.yes or ask_yes_no("保存这条记录？")
        reminder_confirmed = False if args.no_reminder else (args.yes or ask_yes_no("创建这条提醒？"))
        try:
            result = agent.save_draft(draft, confirmed, reminder_confirmed)
        except ConfirmationRequired as exc:
            print(str(exc))
            raise SystemExit(2) from exc
        print(f"Saved record: {result.record.id}")
        if result.reminder:
            print(f"Created reminder: {result.reminder.id} at {result.reminder.scheduled_at.isoformat()}")
        return

    if args.command == "search":
        _records, answer = agent.search(args.text)
        print(answer)
        return

    if args.command == "list":
        records = repository.search_records(settings.default_user_id, query=args.query)
        for record in records:
            deadline = record.deadline.isoformat() if record.deadline else "-"
            amount = f"{record.amount:g}" if record.amount is not None else "-"
            print(f"{record.id} | v{record.version} | {record.record_type.value} | {record.title} | {amount} | {record.status.value} | {deadline}")
        return

    if args.command == "reminders":
        status = ReminderStatus(args.status) if args.status else None
        reminders_list = repository.list_reminders(settings.default_user_id, status=status)
        for reminder in reminders_list:
            print(
                f"{reminder.id} | {reminder.record_id} | {reminder.status.value} | "
                f"{reminder.scheduled_at.isoformat()} | {reminder.message}"
            )
        return

    if args.command == "status":
        record = repository.update_record_status(
            settings.default_user_id,
            args.record_id,
            RecordStatus(args.new_status),
            args.expected_version,
        )
        print(f"Updated {record.id} to {record.status.value}, version={record.version}")
        return

    if args.command == "worker":
        worker_service = ReminderWorker(settings, repository)
        if args.once:
            count = worker_service.run_once(datetime.now().astimezone())
            print(f"Processed reminders: {count}")
        else:
            worker_service.run_forever(interval_seconds=args.interval)


def print_draft(draft: DraftResult) -> None:
    print(f"Thread: {draft.thread_id}")
    if draft.warnings:
        for warning in draft.warnings:
            print(f"Warning: {warning}")
    print(f"Intent: {draft.candidate.intent}")
    print(f"Record type: {draft.candidate.record_type}")
    if draft.missing_fields:
        print("Missing fields: " + ", ".join(draft.missing_fields))
    if draft.record:
        print("Record preview:")
        print(draft.record.model_dump_json(indent=2))
    if draft.duplicate_candidates:
        print("Possible duplicates:")
        for duplicate in draft.duplicate_candidates:
            print(f"- {duplicate.record_id} {duplicate.title} {duplicate.score:.2f}: {duplicate.reason}")
    if draft.reminder:
        print("Reminder preview:")
        print(draft.reminder.model_dump_json(indent=2))


def ask_yes_no(prompt: str) -> bool:
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes", "是", "确认"}


if __name__ == "__main__":
    main()
