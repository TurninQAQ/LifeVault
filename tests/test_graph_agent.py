from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

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
            self.assertEqual(duplicate_turn.interrupt_type, "record_confirmation")
            duplicate_turn = agent.resume(duplicate_turn.thread_id, {"action": "confirm"})
            self.assertEqual(duplicate_turn.status, "interrupted")
            self.assertEqual(duplicate_turn.interrupt_type, "duplicate_review")
            self.assertTrue(duplicate_turn.duplicate_candidates)
            agent.close()

    def test_record_correction_recalculates_record_and_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            turn = agent.start_create_record(
                "我 2099-07-25 买了一个相机，5000 元，七天退货，"
                "退货前 2 天提醒我。"
            )
            original_schedule = turn.reminders[0]["scheduled_at"]

            turn = agent.resume(
                turn.thread_id,
                {
                    "action": "apply",
                    "corrections": {
                        "amount": 5200,
                        "return_days": 14,
                    },
                },
            )

            self.assertEqual(turn.interrupt_type, "record_confirmation")
            self.assertEqual(turn.field_errors, {})
            self.assertEqual(turn.record["amount"], 5200)
            self.assertEqual(turn.record["details"]["return_deadline"], "2099-08-08")
            self.assertNotEqual(turn.reminders[0]["scheduled_at"], original_schedule)
            agent.close()

    def test_invalid_record_correction_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            turn = agent.start_create_record(
                "我 2099-07-25 买了一个相机，5000 元，七天退货，不提醒。"
            )

            turn = agent.resume(
                turn.thread_id,
                {
                    "action": "apply",
                    "corrections": {
                        "amount": 5200,
                        "return_days": 5000,
                    },
                },
            )

            self.assertEqual(turn.interrupt_type, "record_confirmation")
            self.assertIn("return_days", turn.field_errors)
            self.assertEqual(turn.candidate["amount"], 5000.0)
            self.assertEqual(turn.candidate["return_days"], 7)
            self.assertEqual(turn.record["amount"], 5000.0)
            agent.close()

    def test_duplicate_check_uses_corrected_final_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            existing_text = (
                "我 2099-07-25 在京东买了一个相机，5000 元，订单号 FINAL-1，"
                "七天退货，不提醒。"
            )
            existing = agent.start_create_record(existing_text)
            existing = agent.resume(existing.thread_id, {"action": "confirm"})
            self.assertEqual(existing.status, "completed")

            turn = agent.start_create_record(
                "我 2099-07-25 在京东买了一个镜头，5000 元，订单号 OTHER-1，"
                "七天退货，不提醒。"
            )
            turn = agent.resume(
                turn.thread_id,
                {
                    "action": "apply",
                    "corrections": {
                        "title": "相机",
                        "order_number": "FINAL-1",
                    },
                },
            )
            self.assertEqual(turn.interrupt_type, "record_confirmation")

            turn = agent.resume(turn.thread_id, {"action": "confirm"})
            self.assertEqual(turn.interrupt_type, "duplicate_review")
            self.assertTrue(turn.duplicate_candidates)
            agent.close()

    def test_relative_date_is_frozen_in_review_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first_now = datetime(2026, 7, 30, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            later_now = datetime(2026, 8, 2, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            with patch("lifevault.agent.graph_agent.now_in_timezone", return_value=first_now):
                agent = self.make_agent(tmp)
                turn = agent.start_create_record(
                    "我昨天买了一个键盘，899 元，七天退货，不提醒。"
                )
                self.assertEqual(turn.candidate["event_date"], "2026-07-29")
                thread_id = turn.thread_id
                agent.close()

            with patch("lifevault.agent.graph_agent.now_in_timezone", return_value=later_now):
                resumed = self.make_agent(tmp)
                recovered = resumed.get_state(thread_id)
                self.assertEqual(recovered.candidate["event_date"], "2026-07-29")
                self.assertEqual(recovered.record["details"]["return_deadline"], "2026-08-05")
                resumed.close()

    def test_legacy_unconfirmed_duplicate_checkpoint_resumes_to_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            text = (
                "我 2099-07-25 买了一个键盘，899 元，订单号 LEGACY-DUP，"
                "七天退货，不提醒。"
            )
            saved = agent.start_create_record(text)
            saved = agent.resume(saved.thread_id, {"action": "confirm"})
            self.assertEqual(saved.status, "completed")

            legacy = agent.start_create_record(text)
            config = agent._config(legacy.thread_id)
            agent._graph.update_state(
                config,
                {
                    "record_confirmed": False,
                    "review_action": "duplicate",
                },
                as_node="confirm_record",
            )
            agent._graph.invoke(None, config=config)
            thread_id = legacy.thread_id
            agent.close()

            resumed = self.make_agent(tmp)
            recovered = resumed.get_state(thread_id)
            self.assertEqual(recovered.interrupt_type, "duplicate_review")
            turn = resumed.resume(thread_id, {"action": "continue"})
            self.assertEqual(turn.interrupt_type, "record_confirmation")
            self.assertIsNone(turn.saved_record_id)
            resumed.close()

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

    def test_legacy_single_reminder_checkpoint_resumes_after_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            turn = agent.start_create_record(
                "我 2099-07-25 买了一个键盘，899 元，七天退货，退货前 2 天提醒我。"
            )
            turn = agent.resume(turn.thread_id, {"action": "confirm"})
            config = agent._config(turn.thread_id)
            legacy_reminder = turn.reminder
            agent._graph.update_state(
                config,
                {
                    "reminders": [],
                    "reminder": legacy_reminder,
                    "reminder_ids": [],
                    "saved_reminders": [],
                },
                as_node="save_record",
            )
            agent._graph.invoke(None, config=config)
            agent.close()

            resumed_agent = self.make_agent(tmp)
            recovered = resumed_agent.get_state(turn.thread_id)
            self.assertEqual(recovered.interrupt_type, "reminder_confirmation")
            self.assertEqual(len(recovered.reminders), 1)

            completed = resumed_agent.resume(turn.thread_id, {"action": "confirm"})
            self.assertEqual(completed.status, "completed")
            self.assertEqual(len(completed.reminder_ids), 1)
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
            self.assertEqual(mcp_client.calls[0][0], "get_preferences")
            self.assertEqual(mcp_client.calls[1][0], "find_duplicate")
            self.assertEqual(mcp_client.calls[2][0], "save_record")
            self.assertTrue(mcp_client.calls[2][1]["user_confirmed"])
            self.assertEqual(mcp_client.calls[3][0], "create_reminders")
            self.assertTrue(mcp_client.calls[3][1]["user_confirmed"])
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

    def test_subscription_flow_creates_renewal_reminder_through_mcp(self) -> None:
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
                "我订阅了腾讯视频会员，每月 30 元，2026-08-15 自动续费，续费前 3 天提醒我。"
            )
            self.assertEqual(turn.interrupt_type, "record_confirmation")
            self.assertEqual(turn.record["record_type"], "subscription")
            self.assertEqual(turn.record["deadline"], "2026-08-15")

            turn = agent.resume(turn.thread_id, {"action": "confirm"})
            self.assertEqual(turn.interrupt_type, "reminder_confirmation")
            self.assertEqual(turn.reminder["reminder_type"], "renewal")

            turn = agent.resume(turn.thread_id, {"action": "confirm"})
            self.assertEqual(turn.status, "completed")
            self.assertEqual(
                [call[0] for call in mcp_client.calls],
                ["get_preferences", "find_duplicate", "save_record", "create_reminders"],
            )

            records = repo.search_records(settings.default_user_id, query="腾讯视频")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].record_type.value, "subscription")
            self.assertEqual(records[0].details["billing_cycle"], "monthly")
            agent.close()

    def test_purchase_dual_reminders_can_select_only_warranty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            turn = agent.start_create_record(
                "我 2099-07-25 买了一个相机，5000 元，七天退货，保修一年，"
                "退货前 2 天、保修到期前 30 天提醒我。"
            )

            self.assertEqual(turn.interrupt_type, "record_confirmation")
            self.assertEqual(turn.record["details"]["return_deadline"], "2099-08-01")
            self.assertEqual(turn.record["details"]["warranty_deadline"], "2100-07-25")
            self.assertEqual(
                [reminder["reminder_type"] for reminder in turn.reminders],
                ["return_deadline", "warranty_deadline"],
            )

            turn = agent.resume(turn.thread_id, {"action": "confirm"})
            self.assertEqual(turn.interrupt_type, "reminder_confirmation")
            turn = agent.resume(
                turn.thread_id,
                {
                    "action": "confirm",
                    "selected_reminder_types": ["warranty_deadline"],
                },
            )

            self.assertEqual(turn.status, "completed")
            self.assertEqual(len(turn.reminder_ids), 1)
            reminders = agent.repository.list_reminders("local")
            self.assertEqual([reminder.reminder_type.value for reminder in reminders], ["warranty_deadline"])
            agent.close()

    def test_purchase_with_only_warranty_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            turn = agent.start_create_record(
                "我 2099-07-25 买了一个相机，5000 元，保修两年，保修到期前 90 天提醒我。"
            )

            self.assertEqual(turn.interrupt_type, "record_confirmation")
            self.assertNotIn("return_or_warranty_deadline", turn.missing_fields)
            self.assertEqual(turn.record["deadline"], "2101-07-25")
            self.assertEqual(len(turn.reminders), 1)
            self.assertEqual(turn.reminders[0]["reminder_type"], "warranty_deadline")
            agent.close()

    def test_purchase_without_reminder_request_skips_reminder_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            turn = agent.start_create_record(
                "我 2099-07-25 买了一个键盘，899 元，七天退货，不用提醒。"
            )

            self.assertEqual(turn.interrupt_type, "record_confirmation")
            self.assertEqual(turn.reminders, [])
            turn = agent.resume(turn.thread_id, {"action": "confirm"})

            self.assertEqual(turn.status, "completed")
            self.assertEqual(turn.reminder_ids, [])
            self.assertEqual(agent.repository.list_reminders("local"), [])
            agent.close()

    def test_purchase_dual_reminders_can_all_be_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp)
            turn = agent.start_create_record(
                "我 2099-07-25 买了一个显示器，1299 元，七天退货，保修一年，这些期限都提醒我。"
            )
            turn = agent.resume(turn.thread_id, {"action": "confirm"})
            self.assertEqual(len(turn.reminders), 2)

            turn = agent.resume(turn.thread_id, {"action": "skip"})

            self.assertEqual(turn.status, "completed")
            self.assertEqual(turn.reminder_ids, [])
            self.assertEqual(agent.repository.list_reminders("local"), [])
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

    def get_preferences(self) -> dict:
        return self.call_tool("get_preferences", {})

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

    def create_reminders(
        self,
        reminders: list[dict],
        idempotency_key: str,
        user_confirmed: bool,
    ) -> dict:
        return self.call_tool(
            "create_reminders",
            {
                "reminders": reminders,
                "idempotency_key": idempotency_key,
                "user_confirmed": user_confirmed,
            },
        )


class RejectSaveMcpClient:
    def call_tool(self, name: str, arguments: dict) -> dict:
        raise NotImplementedError

    def find_duplicate(self, record: dict, limit: int = 5) -> dict:
        return {"ok": True, "duplicate_candidates": []}

    def get_preferences(self) -> dict:
        return {
            "ok": True,
            "preference": {
                "user_id": "local",
                "default_time": "09:00",
                "quiet_hours_start": None,
                "quiet_hours_end": None,
                "default_advance_days": 2,
            },
        }

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

    def create_reminders(
        self,
        reminders: list[dict],
        idempotency_key: str,
        user_confirmed: bool,
    ) -> dict:
        return {"ok": False, "error": {"code": "not_called", "message": "should not be called"}}


if __name__ == "__main__":
    unittest.main()
