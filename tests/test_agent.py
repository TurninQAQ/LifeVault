from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lifevault.agent.service import LifeVaultAgent
from lifevault.config import Settings
from lifevault.models.schemas import (
    ExtractedRecordCandidate,
    LifeRecordCreate,
    RecordType,
    UserPreference,
    UserPreferencePatch,
)
from lifevault.storage.repository import VaultRepository


class AgentTest(unittest.TestCase):
    def test_fallback_purchase_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                database_path=Path(tmp) / "test.db",
                use_qwen=False,
            )
            repo = VaultRepository(settings.database_path)
            agent = LifeVaultAgent(settings, repo)
            draft = agent.create_draft("我 2026-07-25 在京东买了一个耳机，3499 元，订单号 123456，七天无理由，退货前两天提醒我。")
            self.assertTrue(draft.is_ready_to_save, draft.missing_fields)
            self.assertEqual(draft.record.title, "耳机")
            self.assertEqual(draft.record.deadline.isoformat(), "2026-08-01")
            result = agent.save_draft(draft, user_confirmed_record=True, user_confirmed_reminder=True)
            self.assertIsNotNone(result.record.id)
            self.assertIsNotNone(result.reminder)

    def test_fallback_subscription_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                database_path=Path(tmp) / "test.db",
                use_qwen=False,
            )
            repo = VaultRepository(settings.database_path)
            agent = LifeVaultAgent(settings, repo)
            draft = agent.create_draft("我订阅了腾讯视频会员，每月 30 元，2026-08-15 自动续费，续费前 3 天提醒我。")

            self.assertTrue(draft.is_ready_to_save, draft.missing_fields)
            self.assertEqual(draft.record.record_type.value, "subscription")
            self.assertEqual(draft.record.title, "腾讯视频")
            self.assertEqual(draft.record.deadline.isoformat(), "2026-08-15")
            self.assertEqual(draft.record.details["service_name"], "腾讯视频")
            self.assertEqual(draft.record.details["billing_cycle"], "monthly")
            self.assertTrue(draft.record.details["auto_renew"])
            self.assertEqual(draft.record.details["renewal_anchor_day"], 15)
            self.assertEqual(draft.record.details["remind_before_days"], 3)
            self.assertEqual(draft.reminder.reminder_type.value, "renewal")
            self.assertEqual(draft.reminder.scheduled_at.isoformat(), "2026-08-12T09:00:00+08:00")

    def test_search_uses_mcp_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                database_path=Path(tmp) / "test.db",
                use_qwen=False,
            )
            repo = VaultRepository(settings.database_path)
            mcp_client = SearchRecordingMcpClient()
            agent = LifeVaultAgent(settings, repo, mcp_client=mcp_client)

            records, answer = agent.search("查一下耳机订单")

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].title, "耳机")
            self.assertIn("耳机", answer)
            self.assertEqual(mcp_client.calls, [("search_records", {"query": "耳机", "record_types": ["purchase"], "limit": 20})])

    def test_agent_reads_reminder_defaults_through_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(database_path=Path(tmp) / "test.db", use_qwen=False)
            repo = VaultRepository(settings.database_path)
            repo.update_preferences(
                settings.default_user_id,
                UserPreferencePatch(default_time="07:30", default_advance_days=4),
                actor="user",
            )
            agent = LifeVaultAgent(settings, repo)

            draft = agent.create_draft(
                "房租 1000 元，2099-08-01 前交，提醒我。"
            )

            self.assertEqual(draft.reminder.scheduled_at.isoformat(), "2099-07-28T07:30:00+08:00")

    def test_purchase_dual_deadlines_and_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(database_path=Path(tmp) / "test.db", use_qwen=False)
            repo = VaultRepository(settings.database_path)
            agent = LifeVaultAgent(settings, repo)

            draft = agent.create_draft(
                "我 2099-01-31 买了一个相机，5000 元，七天退货，保修一个月，"
                "退货前 2 天、保修到期前 10 天提醒我。"
            )

            self.assertTrue(draft.is_ready_to_save, draft.missing_fields)
            self.assertEqual(draft.record.details["return_deadline"], "2099-02-07")
            self.assertEqual(draft.record.details["warranty_deadline"], "2099-02-28")
            self.assertEqual(
                [reminder.reminder_type.value for reminder in draft.reminders],
                ["return_deadline", "warranty_deadline"],
            )
            result = agent.save_draft(
                draft,
                user_confirmed_record=True,
                user_confirmed_reminder=True,
            )
            self.assertEqual(len(result.reminders), 2)
            self.assertEqual(result.reminder.id, result.reminders[0].id)

    def test_purchase_without_reminder_language_has_no_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(database_path=Path(tmp) / "test.db", use_qwen=False)
            agent = LifeVaultAgent(settings)

            draft = agent.create_draft(
                "我 2099-07-25 买了一个键盘，899 元，七天退货。"
            )

            self.assertTrue(draft.is_ready_to_save)
            self.assertEqual(draft.reminders, [])
            self.assertIsNone(draft.reminder)

    def test_explicit_warranty_deadline_wins_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(database_path=Path(tmp) / "test.db", use_qwen=False)
            agent = LifeVaultAgent(settings)

            draft = agent.create_draft(
                "我 2099-07-25 买了一个手机，5999 元，保修一年且保修到 2101-07-25，"
                "保修到期前 30 天提醒。"
            )

            self.assertEqual(draft.record.details["warranty_deadline"], "2101-07-25")
            self.assertTrue(
                any("Explicit warranty deadline differs" in warning for warning in draft.warnings)
            )

    def test_missed_advance_time_becomes_immediate_but_expired_deadline_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(database_path=Path(tmp) / "test.db", use_qwen=False)
            agent = LifeVaultAgent(settings)
            now = datetime(2026, 7, 30, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            candidate = ExtractedRecordCandidate(
                intent="create_record",
                record_type=RecordType.PURCHASE,
                title="相机",
                amount=5000,
                reminder_requested=True,
                return_reminder_requested=True,
                warranty_reminder_requested=True,
                return_remind_before_days=2,
                warranty_remind_before_days=30,
            )
            record = LifeRecordCreate(
                record_type=RecordType.PURCHASE,
                title="相机",
                amount=5000,
                event_date=date(2026, 7, 20),
                deadline=date(2026, 7, 31),
                details={
                    "return_deadline": "2026-07-31",
                    "warranty_deadline": "2026-07-29",
                },
            )

            reminders, warnings = agent._build_reminders(
                candidate,
                record,
                UserPreference(user_id="local"),
                now,
            )

            self.assertEqual(len(reminders), 1)
            self.assertEqual(reminders[0].scheduled_at, now)
            self.assertEqual(reminders[0].reminder_type.value, "return_deadline")
            self.assertEqual(len(warnings), 2)
            self.assertTrue(any("scheduled immediately" in warning for warning in warnings))
            self.assertTrue(any("already passed" in warning for warning in warnings))

    def test_model_tool_plan_cannot_update_preferences(self) -> None:
        candidate = ExtractedRecordCandidate(tool_plan=["update_preferences"])

        self.assertEqual(candidate.tool_plan, [])


class SearchRecordingMcpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def search_records(
        self,
        query: str | None = None,
        record_types: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
    ) -> dict:
        self.calls.append(
            (
                "search_records",
                {
                    "query": query,
                    "record_types": record_types,
                    "limit": limit,
                },
            )
        )
        return {
            "ok": True,
            "records": [
                {
                    "id": "record-1",
                    "user_id": "local",
                    "record_type": "purchase",
                    "title": "耳机",
                    "amount": 299.0,
                    "currency": "CNY",
                    "event_date": "2026-07-25",
                    "deadline": "2026-08-01",
                    "status": "active",
                    "version": 1,
                    "details": {"merchant": "京东"},
                    "notes": None,
                    "source_text_hash": None,
                    "source_text_preview": None,
                    "created_at": "2026-07-27T00:00:00+00:00",
                    "updated_at": "2026-07-27T00:00:00+00:00",
                }
            ],
        }


if __name__ == "__main__":
    unittest.main()
