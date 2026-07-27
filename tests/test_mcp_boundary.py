from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class McpBoundaryTest(unittest.TestCase):
    def test_agent_search_does_not_read_records_directly(self) -> None:
        source = (PROJECT_ROOT / "lifevault" / "agent" / "service.py").read_text()
        self.assertNotIn("self.repository.search_records", source)
        self.assertIn("self.mcp_client.search_records", source)

    def test_cli_record_and_reminder_paths_use_mcp_client(self) -> None:
        source = (PROJECT_ROOT / "lifevault" / "cli.py").read_text()
        for forbidden in [
            "repository.search_records",
            "repository.list_reminders",
            "repository.update_record_status",
            "repository.cancel_reminder",
        ]:
            self.assertNotIn(forbidden, source)
        for required in [
            "mcp_client.search_records",
            "mcp_client.list_reminders",
            "mcp_client.snooze_reminder",
            "mcp_client.update_record_status",
            "mcp_client.list_upcoming_subscriptions",
        ]:
            self.assertIn(required, source)

    def test_streamlit_record_and_reminder_paths_use_mcp_client(self) -> None:
        source = (PROJECT_ROOT / "lifevault" / "app" / "main.py").read_text()
        for forbidden in [
            "repository.search_records",
            "repository.list_reminders",
            "repository.update_record_status",
            "repository.cancel_reminder",
        ]:
            self.assertNotIn(forbidden, source)
        for required in [
            "mcp_client.search_records",
            "mcp_client.list_reminders",
            "mcp_client.snooze_reminder",
            "mcp_client.update_record_status",
            "mcp_client.cancel_reminder",
        ]:
            self.assertIn(required, source)


if __name__ == "__main__":
    unittest.main()
