from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from lifevault.agent.update_graph_agent import RecordUpdateGraphAgent
from lifevault.config import Settings
from lifevault.models.schemas import LifeRecordCreate, RecordUpdatePatch
from lifevault.storage.repository import VaultRepository


class RecordUpdateGraphAgentTest(unittest.TestCase):
    def make_services(self, tmp: str) -> tuple[RecordUpdateGraphAgent, VaultRepository]:
        settings = Settings(
            database_path=Path(tmp) / "lifevault.db",
            langgraph_checkpoint_path=Path(tmp) / "graph.sqlite",
            use_qwen=False,
        )
        repo = VaultRepository(settings.database_path)
        return RecordUpdateGraphAgent(settings, repo), repo

    def seed_subscription(self, repo: VaultRepository):
        return repo.save_record(
            "local",
            LifeRecordCreate(
                record_type="subscription",
                title="ChatGPT Plus",
                amount=20,
                currency="USD",
                event_date=date(2026, 7, 1),
                deadline=date(2026, 8, 15),
                details={
                    "service_name": "ChatGPT Plus",
                    "billing_cycle": "monthly",
                    "auto_renew": True,
                },
            ),
            "seed-subscription",
        )

    def test_search_always_requires_target_selection_then_updates_through_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent, repo = self.make_services(tmp)
            record = self.seed_subscription(repo)

            turn = agent.start("把 ChatGPT Plus 的月费改成 25 美元")
            self.assertEqual(turn.interrupt_type, "target_selection")
            self.assertEqual([item["id"] for item in turn.candidates], [record.id])

            turn = agent.resume(turn.thread_id, {"record_id": record.id})
            self.assertEqual(turn.interrupt_type, "update_confirmation")
            self.assertEqual(turn.changes, {"amount": 25.0, "currency": "USD"})
            turn = agent.resume(turn.thread_id, {"action": "confirm"})

            self.assertEqual(turn.status, "completed")
            self.assertEqual(repo.get_record("local", record.id).amount, 25)
            agent.close()

    def test_preselected_target_and_missing_update_supplement_keep_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent, repo = self.make_services(tmp)
            record = self.seed_subscription(repo)

            turn = agent.start("修改这条记录", preselected_record_id=record.id)
            self.assertEqual(turn.interrupt_type, "missing_update_details")
            self.assertEqual(turn.selected_record_id, record.id)
            turn = agent.resume(turn.thread_id, {"text": "下次续费日改到下个月 20 号"})

            self.assertEqual(turn.interrupt_type, "update_confirmation")
            self.assertEqual(turn.selected_record_id, record.id)
            self.assertIn("next_renewal_date", turn.changes)
            agent.close()

    def test_relative_date_is_frozen_and_checkpoint_does_not_store_full_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = datetime(2026, 7, 31, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            later = datetime(2026, 8, 3, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            with patch(
                "lifevault.agent.update_graph_agent.now_in_timezone",
                return_value=first,
            ):
                agent, repo = self.make_services(tmp)
                record = self.seed_subscription(repo)
                turn = agent.start(
                    "下次续费日改到下个月 20 号",
                    preselected_record_id=record.id,
                )
                self.assertEqual(turn.changes["next_renewal_date"], "2026-08-20")
                state = agent._graph.get_state(agent._config(turn.thread_id)).values
                self.assertNotIn("raw_input", state)
                self.assertNotIn("record", state)
                self.assertNotIn("notes", state.get("selected_summary", {}))
                thread_id = turn.thread_id
                agent.close()

            with patch(
                "lifevault.agent.update_graph_agent.now_in_timezone",
                return_value=later,
            ):
                resumed, _repo = self.make_services(tmp)
                recovered = resumed.get_state(thread_id)
                self.assertEqual(recovered.changes["next_renewal_date"], "2026-08-20")
                resumed.close()

    def test_no_changes_external_action_and_mixed_update_do_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent, repo = self.make_services(tmp)
            record = self.seed_subscription(repo)

            unchanged = agent.start(
                "金额改成 20 美元",
                preselected_record_id=record.id,
            )
            self.assertEqual(unchanged.status, "completed")
            self.assertTrue(unchanged.no_changes)
            self.assertEqual(repo.get_record("local", record.id).version, 1)

            external = agent.start("帮我取消 ChatGPT Plus 订阅")
            self.assertEqual(external.status, "cancelled")
            self.assertIn("cannot", external.errors[0].lower())

            mixed = agent.start(
                "金额改成 30 美元并标记为已取消",
                preselected_record_id=record.id,
            )
            self.assertEqual(mixed.status, "cancelled")
            self.assertIn("separately", mixed.errors[0])
            self.assertEqual(repo.get_record("local", record.id).version, 1)
            agent.close()

    def test_version_conflict_refreshes_preview_and_requires_confirmation_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent, repo = self.make_services(tmp)
            record = self.seed_subscription(repo)
            turn = agent.start("金额改成 25 美元", preselected_record_id=record.id)
            self.assertEqual(turn.interrupt_type, "update_confirmation")

            repo.update_record(
                "local",
                record.id,
                RecordUpdatePatch(notes="concurrent"),
                expected_version=1,
                timezone_name="Asia/Shanghai",
                now=datetime(2026, 7, 31, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                idempotency_key="concurrent-update",
                duplicate_confirmed=False,
            )
            refreshed = agent.resume(turn.thread_id, {"action": "confirm"})

            self.assertEqual(refreshed.interrupt_type, "update_confirmation")
            self.assertEqual(refreshed.record["version"], 2)
            self.assertTrue(any("changed after preview" in item for item in refreshed.warnings))
            completed = agent.resume(refreshed.thread_id, {"action": "confirm"})
            self.assertEqual(completed.status, "completed")
            self.assertEqual(repo.get_record("local", record.id).version, 3)
            agent.close()

    def test_cross_field_preview_error_can_be_corrected_without_losing_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent, repo = self.make_services(tmp)
            record = repo.save_record(
                "local",
                LifeRecordCreate(
                    record_type="purchase",
                    title="相机",
                    event_date=date(2026, 7, 20),
                    details={"warranty_deadline": "2027-07-20"},
                ),
                "cross-field-record",
            )
            turn = agent.start(
                "保修截止日改到 2026-07-01",
                preselected_record_id=record.id,
            )
            self.assertEqual(turn.interrupt_type, "update_confirmation")
            self.assertIn("warranty_deadline", turn.field_errors)

            corrected = agent.resume(
                turn.thread_id,
                {"action": "apply", "changes": {"warranty_deadline": "2027-08-01"}},
            )
            self.assertEqual(corrected.interrupt_type, "update_confirmation")
            self.assertEqual(corrected.field_errors, {})
            self.assertEqual(corrected.selected_record_id, record.id)
            agent.close()

    def test_archive_and_restore_use_target_extraction_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent, repo = self.make_services(tmp)
            record = self.seed_subscription(repo)

            with patch.object(
                agent.extractor,
                "extract_update",
                side_effect=AssertionError("lifecycle must skip patch extraction"),
            ):
                turn = agent.start("归档 ChatGPT Plus 会员记录")
                self.assertEqual(turn.interrupt_type, "target_selection")
                turn = agent.resume(turn.thread_id, {"record_id": record.id})
                self.assertEqual(turn.operation, "archive_record")
                self.assertEqual(turn.interrupt_type, "update_confirmation")
                turn = agent.resume(turn.thread_id, {"action": "confirm"})
                self.assertEqual(turn.status, "completed")

            archived = repo.get_record("local", record.id)
            self.assertIsNotNone(archived.archived_at)
            restore = agent.start("取消归档 ChatGPT Plus 会员记录")
            self.assertEqual([item["id"] for item in restore.candidates], [record.id])
            restore = agent.resume(restore.thread_id, {"record_id": record.id})
            self.assertEqual(restore.operation, "restore_record")
            restore = agent.resume(restore.thread_id, {"action": "confirm"})
            self.assertEqual(restore.status, "completed")
            self.assertIsNone(repo.get_record("local", record.id).archived_at)
            agent.close()

    def test_ambiguous_lifecycle_language_requires_explicit_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent, repo = self.make_services(tmp)
            record = self.seed_subscription(repo)
            bill = repo.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="停车费"),
                "ambiguous-bill",
            )
            archive = agent.start("归档这条记录")
            self.assertEqual(archive.interrupt_type, "missing_target")
            archive = agent.resume(archive.thread_id, {"text": "停车费"})
            self.assertEqual(archive.interrupt_type, "target_selection")
            self.assertEqual([item["id"] for item in archive.candidates], [bill.id])
            repo.archive_record(
                "local",
                record.id,
                1,
                "ambiguous-seed-archive",
                datetime(2026, 7, 31, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

            restore = agent.start("恢复 ChatGPT Plus")
            self.assertEqual(restore.interrupt_type, "missing_target")
            restore = agent.resume(restore.thread_id, {"text": "ChatGPT Plus"})
            self.assertEqual(restore.interrupt_type, "missing_target")
            restore = agent.resume(
                restore.thread_id,
                {"text": "取消归档 ChatGPT Plus 会员记录"},
            )
            self.assertEqual(restore.interrupt_type, "target_selection")
            self.assertEqual([item["id"] for item in restore.candidates], [record.id])

            delete = agent.start("删除它")
            self.assertEqual(delete.interrupt_type, "missing_target")
            agent.close()


if __name__ == "__main__":
    unittest.main()
