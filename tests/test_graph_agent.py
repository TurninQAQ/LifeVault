from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lifevault.agent.graph_agent import GraphAgent
from lifevault.config import Settings
from lifevault.mcp_server.client import InProcessPersonalVaultMcpClient
from lifevault.storage.repository import VaultRepository


class GraphAgentTest(unittest.TestCase):
    def make_agent(self, tmp: str) -> GraphAgent:
        settings = Settings(
            database_path=Path(tmp) / "lifevault.db",
            langgraph_checkpoint_path=Path(tmp) / "langgraph.sqlite",
            use_qwen=False,
        )
        repo = VaultRepository(settings.database_path)
        return GraphAgent(settings, repo)

    def test_record_and_reminder_confirmation_interrupts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            turn = agent.start_create_record(
                "我 2026-07-25 在京东买了一个耳机，3499 元，订单号 A100，七天无理由，退货前两天提醒我。"
            )
            self.assertEqual(turn.status, "interrupted")
            self.assertEqual(turn.interrupt_type, "record_confirmation")

            turn = agent.resume(turn.thread_id, {"action": "confirm"})
            self.assertEqual(turn.status, "interrupted")
            self.assertEqual(turn.interrupt_type, "reminder_confirmation")
            self.assertIsNotNone(turn.saved_record_id)

            turn = agent.resume(turn.thread_id, {"action": "confirm"})
            self.assertEqual(turn.status, "completed")
            self.assertIsNotNone(turn.saved_record_id)
            self.assertIsNotNone(turn.reminder_id)
            agent.close()

    def test_missing_fields_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            turn = agent.start_create_record("我昨天在京东买了一个耳机，七天无理由，退货前两天提醒我。")
            self.assertEqual(turn.interrupt_type, "missing_fields")
            self.assertIn("amount", turn.missing_fields)

            turn = agent.resume(turn.thread_id, {"text": "金额 3499 元，订单号 A101"})
            self.assertEqual(turn.status, "interrupted")
            self.assertEqual(turn.interrupt_type, "record_confirmation")
            self.assertEqual(turn.record["amount"], 3499.0)
            agent.close()

    def test_duplicate_review_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            text = "我 2026-07-25 在京东买了一个耳机，3499 元，订单号 A102，七天无理由，退货前两天提醒我。"
            turn = agent.start_create_record(text)
            turn = agent.resume(turn.thread_id, {"action": "confirm"})
            turn = agent.resume(turn.thread_id, {"action": "skip"})
            self.assertEqual(turn.status, "completed")

            duplicate_turn = agent.start_create_record(text)
            self.assertEqual(duplicate_turn.status, "interrupted")
            self.assertEqual(duplicate_turn.interrupt_type, "duplicate_review")
            self.assertTrue(duplicate_turn.duplicate_candidates)
            agent.close()

    def test_resume_after_new_agent_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            turn = agent.start_create_record(
                "我 2026-07-25 在京东买了一个键盘，899 元，订单号 A103，七天无理由，退货前两天提醒我。"
            )
            self.assertEqual(turn.interrupt_type, "record_confirmation")
            thread_id = turn.thread_id
            agent.close()

            resumed_agent = self.make_agent(tmp)
            recovered = resumed_agent.get_state(thread_id)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.interrupt_type, "record_confirmation")
            turn = resumed_agent.resume(thread_id, {"action": "confirm"})
            self.assertEqual(turn.interrupt_type, "reminder_confirmation")
            resumed_agent.close()

    def test_graph_uses_mcp_client_for_confirmed_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                database_path=Path(tmp) / "lifevault.db",
                langgraph_checkpoint_path=Path(tmp) / "langgraph.sqlite",
                use_qwen=False,
            )
            repo = VaultRepository(settings.database_path)
            mcp_client = RecordingMcpClient(settings, repo)
            agent = GraphAgent(settings, repo, mcp_client=mcp_client)

            turn = agent.start_create_record(
                "我 2026-07-25 在京东买了一个显示器，1299 元，订单号 MCP-GRAPH，七天无理由，退货前两天提醒我。"
            )
            self.assertEqual(turn.interrupt_type, "record_confirmation")
            turn = agent.resume(turn.thread_id, {"action": "confirm"})
            self.assertEqual(turn.interrupt_type, "reminder_confirmation")
            turn = agent.resume(turn.thread_id, {"action": "confirm"})

            self.assertEqual(turn.status, "completed")
            self.assertEqual(mcp_client.calls[0][0], "find_duplicate")
            self.assertEqual(mcp_client.calls[1][0], "save_record")
            self.assertTrue(mcp_client.calls[1][1]["user_confirmed"])
            self.assertEqual(mcp_client.calls[2][0], "create_reminder")
            self.assertTrue(mcp_client.calls[2][1]["user_confirmed"])
            self.assertEqual(len(repo.search_records(settings.default_user_id, query="显示器")), 1)
            agent.close()

    def test_graph_handles_mcp_save_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                database_path=Path(tmp) / "lifevault.db",
                langgraph_checkpoint_path=Path(tmp) / "langgraph.sqlite",
                use_qwen=False,
            )
            repo = VaultRepository(settings.database_path)
            agent = GraphAgent(settings, repo, mcp_client=RejectSaveMcpClient())

            turn = agent.start_create_record(
                "我 2026-07-25 在京东买了一个支架，99 元，订单号 MCP-ERR，七天无理由，退货前两天提醒我。"
            )
            self.assertEqual(turn.interrupt_type, "record_confirmation")
            turn = agent.resume(turn.thread_id, {"action": "confirm"})

            self.assertEqual(turn.status, "cancelled")
            self.assertIn("MCP save_record failed", turn.errors[0])
            self.assertEqual(len(repo.search_records(settings.default_user_id, query="支架")), 0)
            agent.close()


class RecordingMcpClient:
    def __init__(self, settings: Settings, repository: VaultRepository):
        self.inner = InProcessPersonalVaultMcpClient(settings, repository)
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        return self.inner.call_tool(name, arguments)

    def find_duplicate(self, record: dict, limit: int = 5) -> dict:
        return self.call_tool("find_duplicate", {"record": record, "limit": limit})

    def save_record(
        self,
        record: dict,
        idempotency_key: str,
        user_confirmed: bool,
        source_ids: list[str] | None = None,
    ) -> dict:
        return self.call_tool(
            "save_record",
            {
                "record": record,
                "idempotency_key": idempotency_key,
                "user_confirmed": user_confirmed,
                "source_ids": source_ids or [],
            },
        )

    def create_reminder(
        self,
        record_id: str,
        scheduled_at: str,
        reminder_type: str,
        idempotency_key: str,
        user_confirmed: bool,
        message: str | None = None,
        parent_id: str | None = None,
    ) -> dict:
        return self.call_tool(
            "create_reminder",
            {
                "record_id": record_id,
                "scheduled_at": scheduled_at,
                "reminder_type": reminder_type,
                "idempotency_key": idempotency_key,
                "user_confirmed": user_confirmed,
                "message": message,
                "parent_id": parent_id,
            },
        )


class RejectSaveMcpClient:
    def call_tool(self, name: str, arguments: dict) -> dict:
        raise NotImplementedError

    def find_duplicate(self, record: dict, limit: int = 5) -> dict:
        return {"ok": True, "duplicate_candidates": []}

    def save_record(
        self,
        record: dict,
        idempotency_key: str,
        user_confirmed: bool,
        source_ids: list[str] | None = None,
    ) -> dict:
        return {"ok": False, "error": {"code": "forced_failure", "message": "test failure"}}

    def create_reminder(
        self,
        record_id: str,
        scheduled_at: str,
        reminder_type: str,
        idempotency_key: str,
        user_confirmed: bool,
        message: str | None = None,
        parent_id: str | None = None,
    ) -> dict:
        return {"ok": False, "error": {"code": "not_called", "message": "should not be called"}}


if __name__ == "__main__":
    unittest.main()
