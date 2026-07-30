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
