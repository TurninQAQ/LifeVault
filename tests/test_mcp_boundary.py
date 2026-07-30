from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class McpBoundaryTest(unittest.TestCase):
    def test_agent_search_does_not_read_records_directly(self) -> None:
        source = (PROJECT_ROOT / "lifevault" / "agent" / "service.py").read_text()
        self.assertNotIn("self.repository.search_records", source)
        self.assertNotIn("self.repository.get_preferences", source)
        self.assertIn("self.mcp_client.search_records", source)
        self.assertIn("self.mcp_client.get_preferences", source)

    def test_cli_record_and_reminder_paths_use_mcp_client(self) -> None:
        source = (PROJECT_ROOT / "lifevault" / "cli.py").read_text()
        for forbidden in [
            "repository.search_records",
            "repository.list_reminders",
            "repository.list_audit_logs",
            "repository.update_record_status",
            "repository.cancel_reminder",
            "repository.get_preferences",
            "repository.update_preferences",
        ]:
            self.assertNotIn(forbidden, source)
        for required in [
            "mcp_client.search_records",
            "mcp_client.list_reminders",
            "mcp_client.list_audit_logs",
            "mcp_client.snooze_reminder",
            "mcp_client.update_record_status",
            "mcp_client.list_upcoming_subscriptions",
            "mcp_client.get_preferences",
            "mcp_client.update_preferences",
        ]:
            self.assertIn(required, source)

    def test_streamlit_record_and_reminder_paths_use_mcp_client(self) -> None:
        source = (PROJECT_ROOT / "lifevault" / "app" / "main.py").read_text()
        for forbidden in [
            "repository.search_records",
            "repository.list_reminders",
            "repository.list_audit_logs",
            "repository.update_record_status",
            "repository.cancel_reminder",
            "repository.get_preferences",
            "repository.update_preferences",
        ]:
            self.assertNotIn(forbidden, source)
        for required in [
            "mcp_client.search_records",
            "mcp_client.list_reminders",
            "mcp_client.list_audit_logs",
            "mcp_client.snooze_reminder",
            "mcp_client.update_record_status",
            "mcp_client.cancel_reminder",
            "mcp_client.get_preferences",
            "mcp_client.update_preferences",
        ]:
            self.assertIn(required, source)


if __name__ == "__main__":
    unittest.main()
