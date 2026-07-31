from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from lifevault.config import Settings
from lifevault.mcp_server.client import InProcessPersonalVaultMcpClient
from lifevault.models.schemas import (
    LifeRecordCreate,
    RecordStatus,
    RecordUpdatePatch,
    ReminderCreate,
    ReminderStatus,
    ReminderType,
)
from lifevault.records.update_planner import RecordUpdateError
from lifevault.storage.database import connect
from lifevault.storage.repository import VaultRepository
from lifevault.worker.reminder_worker import ReminderWorker


NOW = datetime(2026, 7, 31, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class RecordLifecycleTest(unittest.TestCase):
    def test_archive_cancels_reminders_and_restore_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lifecycle.db"
            repo = VaultRepository(path)
            record = repo.save_record(
                "local",
                LifeRecordCreate(record_type="subscription", title="ChatGPT Plus"),
                "lifecycle-record",
            )
            pending = self._reminder(repo, record.id, "pending")
            snoozed = self._reminder(repo, record.id, "snoozed")
            with connect(path) as conn:
                conn.execute(
                    "UPDATE reminders SET status = ? WHERE id = ?",
                    (ReminderStatus.SNOOZED.value, snoozed.id),
                )

            preview = repo.preview_record_archive("local", record.id, 1, NOW)
            self.assertEqual(preview.operation, "archive_record")
            self.assertEqual(
                {item.id for item in preview.reminders_to_cancel},
                {pending.id, snoozed.id},
            )
            archived = repo.archive_record(
                "local", record.id, 1, "archive-once", NOW
            )
            replay = repo.archive_record(
                "local", record.id, 1, "archive-once", NOW
            )
            self.assertEqual(archived.record.version, 2)
            self.assertEqual(replay.record.version, 2)
            self.assertIsNotNone(archived.record.archived_at)
            self.assertTrue(
                all(item.status == ReminderStatus.CANCELLED for item in archived.cancelled_reminders)
            )

            restored = repo.restore_record(
                "local", record.id, 2, "restore-once", NOW
            )
            replay_restore = repo.restore_record(
                "local", record.id, 2, "restore-once", NOW
            )
            self.assertEqual(restored.record.version, 3)
            self.assertEqual(replay_restore.record.version, 3)
            self.assertIsNone(restored.record.archived_at)
            self.assertEqual(restored.cancelled_reminders, [])
            self.assertEqual(
                [item.status for item in repo.list_reminders("local")],
                [ReminderStatus.CANCELLED, ReminderStatus.CANCELLED],
            )
            self.assertEqual(len(repo.list_audit_logs("local", action="archive_record")), 1)
            self.assertEqual(len(repo.list_audit_logs("local", action="restore_record")), 1)

    def test_no_changes_sending_and_audit_failure_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lifecycle-conflicts.db"
            repo = VaultRepository(path)
            record = repo.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="房租"),
                "conflict-record",
            )
            reminder = self._reminder(repo, record.id, "sending")
            repo.claim_due_reminders("local", NOW)
            with self.assertRaises(RecordUpdateError) as sending:
                repo.archive_record("local", record.id, 1, "blocked", NOW)
            self.assertEqual(sending.exception.code, "reminder_in_flight")
            self.assertEqual(repo.get_record("local", record.id).version, 1)
            self.assertEqual(repo.get_reminder("local", reminder.id).status, ReminderStatus.SENDING)

            with connect(path) as conn:
                conn.execute(
                    "UPDATE reminders SET status = ? WHERE id = ?",
                    (ReminderStatus.CANCELLED.value, reminder.id),
                )
            failing = FailingAuditRepository(path)
            with self.assertRaises(RuntimeError):
                failing.archive_record("local", record.id, 1, "audit-fails", NOW)
            self.assertEqual(repo.get_record("local", record.id).version, 1)
            with connect(path) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) AS count FROM record_lifecycle_operations"
                ).fetchone()["count"]
            self.assertEqual(count, 0)

            repo.archive_record("local", record.id, 1, "archive-ok", NOW)
            with self.assertRaises(RecordUpdateError) as unchanged:
                repo.archive_record("local", record.id, 2, "archive-again", NOW)
            self.assertEqual(unchanged.exception.code, "no_changes")
            with connect(path) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) AS count FROM record_lifecycle_operations"
                ).fetchone()["count"]
            self.assertEqual(count, 1)

    def test_archive_search_edit_guards_and_duplicate_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "scope.db")
            first = repo.save_record(
                "local",
                LifeRecordCreate(
                    record_type="purchase",
                    title="机械键盘",
                    amount=899,
                    details={"merchant": "京东", "order_number": "ORDER-1"},
                ),
                "scope-first",
            )
            repo.archive_record("local", first.id, 1, "scope-archive", NOW)
            second = repo.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="房租"),
                "scope-second",
            )

            self.assertEqual([item.id for item in repo.search_records("local")], [second.id])
            self.assertEqual(
                [item.id for item in repo.search_records("local", archive_scope="archived")],
                [first.id],
            )
            self.assertEqual(
                {item.id for item in repo.search_records("local", archive_scope="all")},
                {first.id, second.id},
            )
            duplicate = repo.find_duplicate(
                "local",
                LifeRecordCreate(
                    record_type="purchase",
                    title="机械键盘",
                    amount=899,
                    details={"merchant": "京东", "order_number": "ORDER-1"},
                ),
            )[0]
            self.assertTrue(duplicate.archived)

            with self.assertRaises(RecordUpdateError) as content:
                repo.preview_record_update(
                    "local",
                    first.id,
                    RecordUpdatePatch(notes="blocked"),
                    2,
                    "Asia/Shanghai",
                    NOW,
                )
            self.assertEqual(content.exception.code, "record_archived")
            with self.assertRaises(RecordUpdateError) as status:
                repo.preview_record_status_update(
                    "local", first.id, RecordStatus.RETURNED, 2, NOW
                )
            self.assertEqual(status.exception.code, "record_archived")

    def test_mcp_confirmation_and_archived_worker_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                database_path=Path(tmp) / "mcp.db",
                langgraph_checkpoint_path=Path(tmp) / "graph.db",
                use_qwen=False,
            )
            repo = VaultRepository(settings.database_path)
            record = repo.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="水电费"),
                "mcp-record",
            )
            client = InProcessPersonalVaultMcpClient(settings, repo)
            rejected = client.archive_record(record.id, 1, "mcp-archive", False)
            self.assertEqual(rejected["error"]["code"], "confirmation_required")
            self.assertTrue(client.preview_record_archive(record.id, 1)["ok"])
            archived = client.archive_record(record.id, 1, "mcp-archive", True)
            self.assertTrue(archived["ok"])
            self.assertTrue(client.get_record(record.id)["record"]["archived_at"])
            self.assertEqual(client.search_records()["records"], [])
            self.assertEqual(
                [item["id"] for item in client.search_records(archive_scope="archived")["records"]],
                [record.id],
            )

            legacy = repo.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="遗留账单", deadline=date(2026, 7, 31)),
                "legacy-record",
            )
            legacy_reminder = self._reminder(repo, legacy.id, "legacy")
            with connect(settings.database_path) as conn:
                conn.execute(
                    "UPDATE life_records SET archived_at = ? WHERE id = ?",
                    (NOW.isoformat(), legacy.id),
                )
            provider = RecordingProvider()
            worker = ReminderWorker(settings, repo, desktop_provider=provider, console_provider=provider)
            self.assertEqual(worker.run_once(NOW), 1)
            self.assertEqual(
                repo.get_reminder("local", legacy_reminder.id).status,
                ReminderStatus.CANCELLED,
            )
            self.assertEqual(provider.calls, [])

    @staticmethod
    def _reminder(repo: VaultRepository, record_id: str, suffix: str):
        return repo.create_reminder(
            "local",
            ReminderCreate(
                record_id=record_id,
                scheduled_at=NOW + timedelta(
                    minutes=1 if suffix == "snoozed" else 0
                ),
                reminder_type=ReminderType.BILL_DUE,
                message="到期提醒",
            ),
            f"reminder-{suffix}",
        )


class FailingAuditRepository(VaultRepository):
    def _audit_conn(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("forced audit failure")


class RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send(self, title: str, message: str, record_id: str) -> None:
        self.calls.append((title, message, record_id))


if __name__ == "__main__":
    unittest.main()
