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


if __name__ == "__main__":
    unittest.main()
