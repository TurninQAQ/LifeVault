from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run_smoke(database_path: Path | None = None, cwd: Path | None = None) -> dict[str, Any]:
    if database_path is None:
        tempdir = tempfile.TemporaryDirectory()
        database_path = Path(tempdir.name) / "lifevault_mcp_smoke.db"
    else:
        tempdir = None

    try:
        env = dict(os.environ)
        env["LIFEVAULT_DB"] = str(database_path)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "lifevault.mcp_server.server"],
            env=env,
            cwd=cwd or Path.cwd(),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                tool_names = sorted(tool.name for tool in listed.tools)
                run_id = uuid4().hex[:8]

                default_preferences = await call_json(session, "get_preferences", {})
                require_ok("get_preferences", default_preferences)
                rejected_preferences = await call_json(
                    session,
                    "update_preferences",
                    {
                        "preferences": {"default_time": "07:30"},
                        "user_confirmed": False,
                    },
                )
                updated_preferences = await call_json(
                    session,
                    "update_preferences",
                    {
                        "preferences": {
                            "default_time": "07:30",
                            "default_advance_days": 4,
                            "quiet_hours_start": "22:00",
                            "quiet_hours_end": "08:00",
                        },
                        "user_confirmed": True,
                    },
                )
                require_ok("update_preferences", updated_preferences)
                unchanged_preferences = await call_json(
                    session,
                    "update_preferences",
                    {
                        "preferences": {"default_time": "07:30"},
                        "user_confirmed": True,
                    },
                )
                require_ok("update_preferences unchanged", unchanged_preferences)

                record_payload = {
                    "record_type": "purchase",
                    "title": f"MCP 测试耳机 {run_id}",
                    "amount": 199.0,
                    "currency": "CNY",
                    "event_date": "2026-07-25",
                    "deadline": "2026-08-01",
                    "details": {"merchant": "京东", "order_number": f"MCP-{run_id}"},
                    "notes": "stdio smoke",
                }
                rejected_save = await call_json(
                    session,
                    "save_record",
                    {
                        "record": record_payload,
                        "idempotency_key": f"mcp-smoke-record-rejected-{run_id}",
                        "user_confirmed": False,
                    },
                )
                save_result = await call_json(
                    session,
                    "save_record",
                    {
                        "record": record_payload,
                        "idempotency_key": f"mcp-smoke-record-{run_id}",
                        "user_confirmed": True,
                    },
                )
                require_ok("save_record", save_result)
                record_id = save_result["record"]["id"]

                subscription_payload = {
                    "record_type": "subscription",
                    "title": f"MCP 测试会员 {run_id}",
                    "amount": 30.0,
                    "currency": "CNY",
                    "deadline": "2099-08-15",
                    "details": {
                        "service_name": f"MCP 测试会员 {run_id}",
                        "billing_cycle": "monthly",
                        "auto_renew": True,
                        "renewal_anchor_day": 15,
                    },
                    "notes": "stdio smoke subscription",
                }
                subscription_save = await call_json(
                    session,
                    "save_record",
                    {
                        "record": subscription_payload,
                        "idempotency_key": f"mcp-smoke-subscription-{run_id}",
                        "user_confirmed": True,
                    },
                )
                require_ok("save_record subscription", subscription_save)
                upcoming_subscriptions = await call_json(
                    session,
                    "list_upcoming_subscriptions",
                    {"days": 30000, "include_auto_renew": True, "limit": 10},
                )
                require_ok("list_upcoming_subscriptions", upcoming_subscriptions)

                search_result = await call_json(session, "search_records", {"query": record_payload["title"]})
                require_ok("search_records", search_result)
                get_result = await call_json(session, "get_record", {"record_id": record_id})
                require_ok("get_record", get_result)
                duplicate_result = await call_json(
                    session,
                    "find_duplicate",
                    {
                        "record_type": "purchase",
                        "title": record_payload["title"],
                        "merchant": "京东",
                        "order_number": f"MCP-{run_id}",
                        "amount": 199.0,
                        "event_date": "2026-07-25",
                    },
                )
                require_ok("find_duplicate", duplicate_result)
                update_changes = {"notes": "stdio smoke updated"}
                update_preview = await call_json(
                    session,
                    "preview_record_update",
                    {
                        "record_id": record_id,
                        "changes": update_changes,
                        "expected_version": 1,
                    },
                )
                require_ok("preview_record_update", update_preview)
                rejected_update = await call_json(
                    session,
                    "update_record",
                    {
                        "record_id": record_id,
                        "changes": update_changes,
                        "expected_version": 1,
                        "idempotency_key": f"mcp-smoke-update-rejected-{run_id}",
                        "user_confirmed": False,
                    },
                )
                update_result = await call_json(
                    session,
                    "update_record",
                    {
                        "record_id": record_id,
                        "changes": update_changes,
                        "expected_version": 1,
                        "idempotency_key": f"mcp-smoke-update-{run_id}",
                        "user_confirmed": True,
                    },
                )
                require_ok("update_record", update_result)
                repeated_update = await call_json(
                    session,
                    "update_record",
                    {
                        "record_id": record_id,
                        "changes": update_changes,
                        "expected_version": 1,
                        "idempotency_key": f"mcp-smoke-update-{run_id}",
                        "user_confirmed": True,
                    },
                )
                require_ok("update_record repeated", repeated_update)

                reminder_result = await call_json(
                    session,
                    "create_reminder",
                    {
                        "record_id": record_id,
                        "scheduled_at": "2026-07-30T09:00:00+08:00",
                        "reminder_type": "return_deadline",
                        "message": "MCP smoke reminder",
                        "idempotency_key": f"mcp-smoke-reminder-{run_id}",
                        "user_confirmed": True,
                    },
                )
                require_ok("create_reminder", reminder_result)
                reminder_id = reminder_result["reminder"]["id"]
                rejected_create_reminder = await call_json(
                    session,
                    "create_reminder",
                    {
                        "record_id": record_id,
                        "scheduled_at": "2026-07-31T09:00:00+08:00",
                        "reminder_type": "return_deadline",
                        "message": "MCP rejected reminder",
                        "idempotency_key": f"mcp-smoke-reminder-rejected-{run_id}",
                        "user_confirmed": False,
                    },
                )
                batch_reminders = await call_json(
                    session,
                    "create_reminders",
                    {
                        "reminders": [
                            {
                                "record_id": record_id,
                                "scheduled_at": "2026-08-01T08:00:00+08:00",
                                "reminder_type": "return_deadline",
                                "message": "MCP batch return reminder",
                            },
                            {
                                "record_id": record_id,
                                "scheduled_at": "2027-07-25T09:00:00+08:00",
                                "reminder_type": "warranty_deadline",
                                "message": "MCP batch warranty reminder",
                            },
                        ],
                        "idempotency_key": f"mcp-smoke-reminder-batch-{run_id}",
                        "user_confirmed": True,
                    },
                )
                require_ok("create_reminders", batch_reminders)
                list_result = await call_json(session, "list_reminders", {"status": "pending"})
                require_ok("list_reminders", list_result)
                snooze_result = await call_json(
                    session,
                    "snooze_reminder",
                    {
                        "reminder_id": reminder_id,
                        "new_scheduled_at": "2026-07-30T10:00:00+08:00",
                    },
                )
                require_ok("snooze_reminder", snooze_result)
                child_reminder_id = snooze_result["reminder"]["id"]

                rejected_cancel = await call_json(
                    session,
                    "cancel_reminder",
                    {"reminder_id": child_reminder_id, "user_confirmed": False},
                )
                accepted_cancel = await call_json(
                    session,
                    "cancel_reminder",
                    {"reminder_id": child_reminder_id, "user_confirmed": True},
                )
                require_ok("cancel_reminder", accepted_cancel)
                status_preview = await call_json(
                    session,
                    "preview_record_status_update",
                    {
                        "record_id": record_id,
                        "new_status": "completed",
                        "expected_version": 2,
                    },
                )
                require_ok("preview_record_status_update", status_preview)
                update_status_result = await call_json(
                    session,
                    "update_record_status",
                    {
                        "record_id": record_id,
                        "new_status": "completed",
                        "expected_version": 2,
                        "idempotency_key": f"mcp-smoke-status-{run_id}",
                        "user_confirmed": True,
                    },
                )
                require_ok("update_record_status", update_status_result)
                archive_preview = await call_json(
                    session,
                    "preview_record_archive",
                    {"record_id": record_id, "expected_version": 3},
                )
                require_ok("preview_record_archive", archive_preview)
                rejected_archive = await call_json(
                    session,
                    "archive_record",
                    {
                        "record_id": record_id,
                        "expected_version": 3,
                        "idempotency_key": f"mcp-smoke-archive-rejected-{run_id}",
                        "user_confirmed": False,
                    },
                )
                archive_result = await call_json(
                    session,
                    "archive_record",
                    {
                        "record_id": record_id,
                        "expected_version": 3,
                        "idempotency_key": f"mcp-smoke-archive-{run_id}",
                        "user_confirmed": True,
                    },
                )
                require_ok("archive_record", archive_result)
                archived_search = await call_json(
                    session,
                    "search_records",
                    {"query": record_payload["title"], "archive_scope": "archived"},
                )
                require_ok("search_records archived", archived_search)
                restore_preview = await call_json(
                    session,
                    "preview_record_restore",
                    {"record_id": record_id, "expected_version": 4},
                )
                require_ok("preview_record_restore", restore_preview)
                restore_result = await call_json(
                    session,
                    "restore_record",
                    {
                        "record_id": record_id,
                        "expected_version": 4,
                        "idempotency_key": f"mcp-smoke-restore-{run_id}",
                        "user_confirmed": True,
                    },
                )
                require_ok("restore_record", restore_result)
                audit_result = await call_json(session, "list_audit_logs", {"limit": 100})
                require_ok("list_audit_logs", audit_result)
                audit_logs = audit_result["audit_logs"]

                return {
                    "ok": True,
                    "database_path": str(database_path),
                    "tools": tool_names,
                    "default_preference_time": default_preferences["preference"]["default_time"],
                    "rejected_preferences": rejected_preferences,
                    "updated_preference_time": updated_preferences["preference"]["default_time"],
                    "unchanged_preferences": unchanged_preferences["changed"],
                    "record_id": record_id,
                    "upcoming_subscription_count": len(upcoming_subscriptions["records"]),
                    "search_count": len(search_result["records"]),
                    "get_record_title": get_result["record"]["title"],
                    "duplicate_count": len(duplicate_result["duplicate_candidates"]),
                    "record_update_preview_version": update_preview["record"]["version"],
                    "record_update_version": update_result["record"]["version"],
                    "record_update_replay_version": repeated_update["record"]["version"],
                    "rejected_update": rejected_update,
                    "reminder_id": reminder_id,
                    "list_reminders_count": len(list_result["reminders"]),
                    "snoozed_parent_status": snooze_result["parent_reminder"]["status"],
                    "snoozed_child_id": child_reminder_id,
                    "rejected_save": rejected_save,
                    "rejected_create_reminder": rejected_create_reminder,
                    "batch_reminder_count": len(batch_reminders["reminders"]),
                    "rejected_cancel": rejected_cancel,
                    "accepted_cancel": accepted_cancel,
                    "status_preview_version": status_preview["record"]["version"],
                    "updated_record_status": update_status_result["record"]["status"],
                    "archive_preview_version": archive_preview["record"]["version"],
                    "archived_record_version": archive_result["record"]["version"],
                    "archived_search_count": len(archived_search["records"]),
                    "restore_preview_version": restore_preview["record"]["version"],
                    "restored_record_version": restore_result["record"]["version"],
                    "rejected_archive": rejected_archive,
                    "audit_count": len(audit_logs),
                    "audit_rejected_count": sum(log["result"] == "rejected" for log in audit_logs),
                }
    finally:
        if tempdir is not None:
            tempdir.cleanup()


async def call_json(session: ClientSession, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await session.call_tool(tool_name, arguments)
    if result.isError:
        raise RuntimeError(f"MCP tool error: {tool_name}: {result.content}")
    if not result.content:
        raise RuntimeError(f"MCP tool returned no content: {tool_name}")
    text = result.content[0].text
    data = json.loads(text)
    if not isinstance(data, dict):
        raise RuntimeError(f"MCP tool returned non-object JSON: {tool_name}")
    return data


def require_ok(tool_name: str, data: dict[str, Any]) -> None:
    if not data.get("ok"):
        raise RuntimeError(f"{tool_name} failed: {data}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run LifeVault MCP stdio smoke test")
    parser.add_argument("--db", type=Path, default=None, help="Optional SQLite database path")
    args = parser.parse_args()
    result = asyncio.run(run_smoke(args.db))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
