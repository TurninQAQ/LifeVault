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
            draft = agent.create_draft("我昨天在京东买了一个耳机，3499 元，订单号 123456，七天无理由，退货前两天提醒我。")
            self.assertTrue(draft.is_ready_to_save, draft.missing_fields)
            self.assertEqual(draft.record.title, "耳机")
            self.assertEqual(draft.record.deadline.isoformat(), "2026-08-01")
            result = agent.save_draft(draft, user_confirmed_record=True, user_confirmed_reminder=True)
            self.assertIsNotNone(result.record.id)
            self.assertIsNotNone(result.reminder)


if __name__ == "__main__":
    unittest.main()
