from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lifevault.agent.graph_agent import GraphAgent
from lifevault.config import Settings
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


if __name__ == "__main__":
    unittest.main()
