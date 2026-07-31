from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lifevault.models.schemas import (
    LifeRecordCreate,
    RecordStatus,
    ReminderCreate,
    ReminderStatus,
    ReminderType,
)
from lifevault.records.update_planner import RecordUpdateError
from lifevault.storage.database import connect
from lifevault.storage.repository import VaultRepository


NOW = datetime(2026, 7, 31, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class RecordStatusUpdateTest(unittest.TestCase):
    def test_preview_and_update_cancel_invalid_reminders_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "status.db")
            record = repo.save_record(
                "local",
                LifeRecordCreate(record_type="subscription", title="视频会员"),
                "status-record",
            )
            reminder = repo.create_reminder(
                "local",
                ReminderCreate(
                    record_id=record.id,
                    scheduled_at=NOW,
                    reminder_type=ReminderType.RENEWAL,
                    message="续费提醒",
                ),
                "status-reminder",
            )

            preview = repo.preview_record_status_update(
                "local",
                record.id,
                RecordStatus.CANCELLED,
                expected_version=1,
                now=NOW,
            )
            self.assertEqual(preview.record.version, 2)
            self.assertEqual([item.id for item in preview.reminders_to_cancel], [reminder.id])

            first = repo.update_record_status(
                "local",
                record.id,
                RecordStatus.CANCELLED,
                expected_version=1,
                idempotency_key="cancel-subscription",
                now=NOW,
            )
            repeated = repo.update_record_status(
                "local",
                record.id,
                RecordStatus.CANCELLED,
                expected_version=1,
                idempotency_key="cancel-subscription",
                now=NOW,
            )
            self.assertEqual(first.record.version, 2)
            self.assertEqual(repeated.record.version, 2)
            self.assertEqual(first.cancelled_reminders[0].status, ReminderStatus.CANCELLED)
            self.assertEqual(len(repo.list_audit_logs("local", action="update_record_status")), 1)

    def test_type_whitelist_no_changes_and_sending_conflict_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "status-conflicts.db")
            record = repo.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="房租"),
                "bill-record",
            )
            with self.assertRaises(RecordUpdateError) as invalid:
                repo.preview_record_status_update(
                    "local",
                    record.id,
                    RecordStatus.RETURNED,
                    expected_version=1,
                    now=NOW,
                )
            self.assertEqual(invalid.exception.code, "invalid_status")

            with self.assertRaises(RecordUpdateError) as unchanged:
                repo.preview_record_status_update(
                    "local",
                    record.id,
                    RecordStatus.ACTIVE,
                    expected_version=1,
                    now=NOW,
                )
            self.assertEqual(unchanged.exception.code, "no_changes")
            self.assertEqual(repo.list_audit_logs("local", action="update_record_status"), [])

            reminder = repo.create_reminder(
                "local",
                ReminderCreate(
                    record_id=record.id,
                    scheduled_at=NOW,
                    reminder_type=ReminderType.BILL_DUE,
                    message="发送中",
                ),
                "sending-status-reminder",
            )
            repo.claim_due_reminders("local", NOW)
            with self.assertRaises(RecordUpdateError) as sending:
                repo.update_record_status(
                    "local",
                    record.id,
                    RecordStatus.PAID,
                    expected_version=1,
                    idempotency_key="sending-conflict",
                    now=NOW,
                )
            self.assertEqual(sending.exception.code, "reminder_in_flight")
            self.assertEqual(repo.get_record("local", record.id).version, 1)
            self.assertEqual(repo.get_reminder("local", reminder.id).status, ReminderStatus.SENDING)

    def test_audit_failure_rolls_back_status_and_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status-rollback.db"
            setup = VaultRepository(path)
            record = setup.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="电费"),
                "rollback-status-record",
            )
            reminder = setup.create_reminder(
                "local",
                ReminderCreate(
                    record_id=record.id,
                    scheduled_at=NOW,
                    reminder_type=ReminderType.BILL_DUE,
                    message="电费提醒",
                ),
                "rollback-status-reminder",
            )
            repo = FailingAuditRepository(path)

            with self.assertRaises(RuntimeError):
                repo.update_record_status(
                    "local",
                    record.id,
                    RecordStatus.PAID,
                    expected_version=1,
                    idempotency_key="rollback-status",
                    now=NOW,
                )

            self.assertEqual(setup.get_record("local", record.id).status, RecordStatus.ACTIVE)
            self.assertEqual(setup.get_reminder("local", reminder.id).status, ReminderStatus.PENDING)
            with connect(path) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) AS count FROM record_status_update_operations"
                ).fetchone()["count"]
            self.assertEqual(count, 0)


class FailingAuditRepository(VaultRepository):
    def _audit_conn(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("forced audit failure")


if __name__ == "__main__":
    unittest.main()
