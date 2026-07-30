from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lifevault.config import Settings
from lifevault.mcp_server.client import InProcessPersonalVaultMcpClient
from lifevault.storage.repository import VaultRepository


class McpClientTest(unittest.TestCase):
    def test_in_process_client_confirmation_and_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(database_path=Path(tmp) / "mcp-client.db", use_qwen=False)
            repo = VaultRepository(settings.database_path)
            client = InProcessPersonalVaultMcpClient(settings, repo)

            record = {
                "record_type": "purchase",
                "title": "MCP Client 耳机",
                "amount": 299.0,
                "event_date": "2026-07-25",
                "deadline": "2026-08-01",
                "details": {"merchant": "京东", "order_number": "CLIENT-100"},
            }
            rejected = client.save_record(record, "client-record-rejected", user_confirmed=False)
            self.assertFalse(rejected["ok"])
            self.assertEqual(rejected["error"]["code"], "confirmation_required")

            saved = client.save_record(record, "client-record", user_confirmed=True)
            self.assertTrue(saved["ok"])
            record_id = saved["record"]["id"]

            searched = client.search_records(query="MCP Client 耳机", record_types=["purchase"])
            self.assertTrue(searched["ok"])
            self.assertEqual(len(searched["records"]), 1)

            fetched = client.get_record(record_id)
            self.assertTrue(fetched["ok"])
            self.assertEqual(fetched["record"]["title"], "MCP Client 耳机")

            duplicate = client.find_duplicate(record)
            self.assertTrue(duplicate["ok"])
            self.assertTrue(duplicate["duplicate_candidates"])

            rejected_reminder = client.create_reminder(
                record_id=record_id,
                scheduled_at="2026-07-30T09:00:00+08:00",
                reminder_type="return_deadline",
                idempotency_key="client-reminder-rejected",
                user_confirmed=False,
                message="client reminder",
            )
            self.assertFalse(rejected_reminder["ok"])
            self.assertEqual(rejected_reminder["error"]["code"], "confirmation_required")

            reminder = client.create_reminder(
                record_id=record_id,
                scheduled_at="2026-07-30T09:00:00+08:00",
                reminder_type="return_deadline",
                idempotency_key="client-reminder",
                user_confirmed=True,
                message="client reminder",
            )
            self.assertTrue(reminder["ok"])
            self.assertEqual(reminder["reminder"]["record_id"], record_id)

            listed = client.list_reminders(status="pending")
            self.assertTrue(listed["ok"])
            self.assertEqual(len(listed["reminders"]), 1)

            snoozed = client.snooze_reminder(
                reminder["reminder"]["id"],
                "2026-07-30T10:00:00+08:00",
            )
            self.assertTrue(snoozed["ok"])
            self.assertEqual(snoozed["parent_reminder"]["status"], "snoozed")
            self.assertEqual(snoozed["reminder"]["status"], "pending")

            cancelled = client.cancel_reminder(snoozed["reminder"]["id"], user_confirmed=True)
            self.assertTrue(cancelled["ok"])
            self.assertEqual(cancelled["reminder"]["status"], "cancelled")

            updated = client.update_record_status(record_id, "completed", expected_version=1)
            self.assertTrue(updated["ok"])
            self.assertEqual(updated["record"]["status"], "completed")

    def test_audit_logs_capture_write_outcomes_without_sensitive_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(database_path=Path(tmp) / "mcp-client.db", use_qwen=False)
            repo = VaultRepository(settings.database_path)
            client = InProcessPersonalVaultMcpClient(settings, repo)
            record = {
                "record_type": "purchase",
                "title": "PRIVATE-AUDIT-TITLE",
                "notes": "PRIVATE-AUDIT-NOTES",
            }

            rejected = client.save_record(record, "PRIVATE-REJECTED-KEY", user_confirmed=False)
            self.assertFalse(rejected["ok"])
            saved = client.save_record(record, "PRIVATE-SAVED-KEY", user_confirmed=True)
            self.assertTrue(saved["ok"])
            failed_update = client.update_record_status(
                saved["record"]["id"],
                "completed",
                expected_version=99,
            )
            self.assertFalse(failed_update["ok"])
            rejected_reminder = client.create_reminder(
                record_id=saved["record"]["id"],
                scheduled_at="2026-08-01T09:00:00+08:00",
                reminder_type="custom",
                idempotency_key="PRIVATE-REMINDER-KEY",
                user_confirmed=False,
                message="PRIVATE-REMINDER-MESSAGE",
            )
            self.assertFalse(rejected_reminder["ok"])

            result = client.list_audit_logs(limit=20)
            self.assertTrue(result["ok"])
            logs = result["audit_logs"]
            outcomes = {(log["action"], log["result"]) for log in logs}
            self.assertIn(("save_record", "ok"), outcomes)
            self.assertIn(("save_record", "rejected"), outcomes)
            self.assertIn(("update_record_status", "failed"), outcomes)
            self.assertIn(("create_reminder", "rejected"), outcomes)
            self.assertEqual(
                sum(log["action"] == "save_record" and log["result"] == "ok" for log in logs),
                1,
            )
            summaries = " ".join(log.get("params_summary") or "" for log in logs)
            for secret in [
                "PRIVATE-AUDIT-TITLE",
                "PRIVATE-AUDIT-NOTES",
                "PRIVATE-REJECTED-KEY",
                "PRIVATE-SAVED-KEY",
                "PRIVATE-REMINDER-KEY",
                "PRIVATE-REMINDER-MESSAGE",
            ]:
                self.assertNotIn(secret, summaries)

            filtered = client.list_audit_logs(action="update_record_status", result="failed")
            self.assertTrue(filtered["ok"])
            self.assertEqual(len(filtered["audit_logs"]), 1)

    def test_in_process_client_lists_upcoming_subscriptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(database_path=Path(tmp) / "mcp-client.db", use_qwen=False)
            repo = VaultRepository(settings.database_path)
            client = InProcessPersonalVaultMcpClient(settings, repo)

            saved = client.save_record(
                {
                    "record_type": "subscription",
                    "title": "MCP Client 会员",
                    "amount": 30.0,
                    "deadline": "2099-08-15",
                    "details": {"billing_cycle": "monthly", "auto_renew": True},
                },
                "client-subscription",
                user_confirmed=True,
            )
            self.assertTrue(saved["ok"])

            upcoming = client.list_upcoming_subscriptions(days=30000, limit=5)
            self.assertTrue(upcoming["ok"])
            self.assertEqual(len(upcoming["records"]), 1)
            self.assertEqual(upcoming["records"][0]["title"], "MCP Client 会员")

    def test_batch_reminders_are_atomic_scoped_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(database_path=Path(tmp) / "mcp-client.db", use_qwen=False)
            repo = VaultRepository(settings.database_path)
            client = InProcessPersonalVaultMcpClient(settings, repo)
            first_record = client.save_record(
                {
                    "record_type": "purchase",
                    "title": "批量提醒商品",
                    "amount": 100,
                    "event_date": "2099-07-25",
                    "deadline": "2099-08-01",
                },
                "batch-record-1",
                user_confirmed=True,
            )["record"]
            second_record = client.save_record(
                {
                    "record_type": "purchase",
                    "title": "另一件商品",
                    "amount": 200,
                    "event_date": "2099-07-25",
                    "deadline": "2099-08-01",
                },
                "batch-record-2",
                user_confirmed=True,
            )["record"]
            reminders = [
                {
                    "record_id": first_record["id"],
                    "scheduled_at": "2099-07-30T09:00:00+08:00",
                    "reminder_type": "return_deadline",
                    "message": "退货提醒",
                },
                {
                    "record_id": first_record["id"],
                    "scheduled_at": "2100-06-25T09:00:00+08:00",
                    "reminder_type": "warranty_deadline",
                    "message": "保修提醒",
                },
            ]

            rejected = client.create_reminders(
                reminders,
                idempotency_key="batch-reminders-rejected",
                user_confirmed=False,
            )
            self.assertFalse(rejected["ok"])
            self.assertEqual(repo.list_reminders("local"), [])

            created = client.create_reminders(
                reminders,
                idempotency_key="batch-reminders",
                user_confirmed=True,
            )
            repeated = client.create_reminders(
                reminders,
                idempotency_key="batch-reminders",
                user_confirmed=True,
            )
            self.assertTrue(created["ok"])
            self.assertEqual(
                [item["id"] for item in created["reminders"]],
                [item["id"] for item in repeated["reminders"]],
            )
            self.assertEqual(len(repo.list_reminders("local")), 2)

            reused_key = client.create_reminders(
                [
                    {
                        **reminders[0],
                        "scheduled_at": "2099-07-29T09:00:00+08:00",
                    }
                ],
                idempotency_key="batch-reminders",
                user_confirmed=True,
            )
            self.assertFalse(reused_key["ok"])
            self.assertEqual(len(repo.list_reminders("local")), 2)

            cross_record = client.create_reminders(
                [
                    reminders[0],
                    {
                        **reminders[1],
                        "record_id": second_record["id"],
                    },
                ],
                idempotency_key="batch-cross-record",
                user_confirmed=True,
            )
            self.assertFalse(cross_record["ok"])
            self.assertEqual(len(repo.list_reminders("local")), 2)

            too_many = client.create_reminders(
                [
                    {
                        "record_id": first_record["id"],
                        "scheduled_at": f"2101-01-0{index + 1}T09:00:00+08:00",
                        "reminder_type": "custom",
                        "message": f"提醒 {index}",
                    }
                    for index in range(6)
                ],
                idempotency_key="batch-too-many",
                user_confirmed=True,
            )
            self.assertFalse(too_many["ok"])
            self.assertEqual(len(repo.list_reminders("local")), 2)

            audit = client.list_audit_logs(action="create_reminders")
            successes = [log for log in audit["audit_logs"] if log["result"] == "ok"]
            self.assertEqual(len(successes), 1)
            self.assertIn('"reminder_count": 2', successes[0]["params_summary"])
            self.assertNotIn("退货提醒", successes[0]["params_summary"])

    def test_preferences_require_confirmation_and_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(database_path=Path(tmp) / "mcp-client.db", use_qwen=False)
            repo = VaultRepository(settings.database_path)
            client = InProcessPersonalVaultMcpClient(settings, repo)

            current = client.get_preferences()
            self.assertTrue(current["ok"])
            self.assertEqual(current["preference"]["default_time"], "09:00")

            rejected = client.update_preferences(
                {"default_time": "PRIVATE-TIME"},
                user_confirmed=False,
            )
            self.assertFalse(rejected["ok"])
            self.assertEqual(rejected["error"]["code"], "confirmation_required")

            invalid = client.update_preferences(
                {"quiet_hours_start": "22:00"},
                user_confirmed=True,
            )
            self.assertFalse(invalid["ok"])
            self.assertEqual(invalid["error"]["code"], "update_preferences_failed")

            changed = client.update_preferences(
                {
                    "default_time": "07:30",
                    "default_advance_days": 4,
                    "quiet_hours_start": "22:00",
                    "quiet_hours_end": "08:00",
                },
                user_confirmed=True,
            )
            self.assertTrue(changed["ok"])
            self.assertTrue(changed["changed"])
            self.assertEqual(changed["preference"]["default_time"], "07:30")

            unchanged = client.update_preferences(
                {"default_time": "07:30"},
                user_confirmed=True,
            )
            self.assertTrue(unchanged["ok"])
            self.assertFalse(unchanged["changed"])

            audit = client.list_audit_logs(action="update_preferences")
            self.assertTrue(audit["ok"])
            self.assertEqual(
                sum(log["result"] == "ok" for log in audit["audit_logs"]),
                1,
            )
            summaries = " ".join(log.get("params_summary") or "" for log in audit["audit_logs"])
            self.assertIn("changed_fields", summaries)
            for sensitive_value in ["PRIVATE-TIME", "07:30", "22:00", "08:00"]:
                self.assertNotIn(sensitive_value, summaries)


if __name__ == "__main__":
    unittest.main()
