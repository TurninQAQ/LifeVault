from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lifevault.agent.service import LifeVaultAgent
from lifevault.config import Settings
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


if __name__ == "__main__":
    unittest.main()
