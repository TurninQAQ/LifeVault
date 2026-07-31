from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from lifevault.mcp_server.smoke import run_smoke


class McpServerTest(unittest.TestCase):
    def test_stdio_mcp_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(run_smoke(Path(tmp) / "mcp.db", cwd=Path.cwd()))
            expected_tools = {
                "save_record",
                "search_records",
                "list_upcoming_subscriptions",
                "get_record",
                "preview_record_update",
                "update_record",
                "find_duplicate",
                "update_record_status",
                "preview_record_archive",
                "archive_record",
                "preview_record_restore",
                "restore_record",
                "create_reminder",
                "create_reminders",
                "list_reminders",
                "snooze_reminder",
                "cancel_reminder",
                "list_audit_logs",
                "get_preferences",
                "update_preferences",
            }
            self.assertTrue(result["ok"])
            self.assertTrue(expected_tools.issubset(set(result["tools"])))
            self.assertGreaterEqual(result["upcoming_subscription_count"], 1)
            self.assertEqual(result["search_count"], 1)
            self.assertTrue(result["get_record_title"].startswith("MCP 测试耳机"))
            self.assertGreaterEqual(result["duplicate_count"], 1)
            self.assertEqual(result["record_update_preview_version"], 2)
            self.assertEqual(result["record_update_version"], 2)
            self.assertEqual(result["record_update_replay_version"], 2)
            self.assertFalse(result["rejected_update"]["ok"])
            self.assertEqual(
                result["rejected_update"]["error"]["code"],
                "confirmation_required",
            )
            self.assertGreaterEqual(result["list_reminders_count"], 1)
            self.assertEqual(result["snoozed_parent_status"], "snoozed")
            self.assertFalse(result["rejected_save"]["ok"])
            self.assertEqual(result["rejected_save"]["error"]["code"], "confirmation_required")
            self.assertFalse(result["rejected_create_reminder"]["ok"])
            self.assertEqual(result["rejected_create_reminder"]["error"]["code"], "confirmation_required")
            self.assertEqual(result["batch_reminder_count"], 2)
            self.assertFalse(result["rejected_cancel"]["ok"])
            self.assertEqual(result["rejected_cancel"]["error"]["code"], "confirmation_required")
            self.assertTrue(result["accepted_cancel"]["ok"])
            self.assertEqual(result["accepted_cancel"]["reminder"]["status"], "cancelled")
            self.assertEqual(result["updated_record_status"], "completed")
            self.assertEqual(result["archive_preview_version"], 4)
            self.assertEqual(result["archived_record_version"], 4)
            self.assertEqual(result["archived_search_count"], 1)
            self.assertEqual(result["restore_preview_version"], 5)
            self.assertEqual(result["restored_record_version"], 5)
            self.assertFalse(result["rejected_archive"]["ok"])
            self.assertGreaterEqual(result["audit_count"], 8)
            self.assertGreaterEqual(result["audit_rejected_count"], 3)
            self.assertEqual(result["default_preference_time"], "09:00")
            self.assertFalse(result["rejected_preferences"]["ok"])
            self.assertEqual(result["updated_preference_time"], "07:30")
            self.assertFalse(result["unchanged_preferences"])


if __name__ == "__main__":
    unittest.main()
