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


if __name__ == "__main__":
    unittest.main()
