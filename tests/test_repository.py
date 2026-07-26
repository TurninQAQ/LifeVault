from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from lifevault.models.schemas import LifeRecordCreate, RecordType, ReminderCreate, ReminderStatus, ReminderType
from lifevault.storage.repository import VaultRepository
from lifevault.tools.date_tools import calculate_reminder_at, parse_date_text


class RepositoryTest(unittest.TestCase):
    def test_idempotent_record_and_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "test.db")
            record_create = LifeRecordCreate(
                record_type=RecordType.PURCHASE,
                title="耳机",
                amount=3499,
                event_date=parse_date_text("2026-07-25", "Asia/Shanghai"),
                deadline=parse_date_text("2026-08-01", "Asia/Shanghai"),
                details={"merchant": "京东", "order_number": "123456"},
            )
            first = repo.save_record("local", record_create, "same-key")
            second = repo.save_record("local", record_create, "same-key")
            self.assertEqual(first.id, second.id)

            reminder_create = ReminderCreate(
                record_id=first.id,
                scheduled_at=calculate_reminder_at(first.deadline, 2, "09:00", "Asia/Shanghai"),
                reminder_type=ReminderType.RETURN_DEADLINE,
                message="测试提醒",
            )
            r1 = repo.create_reminder("local", reminder_create, "reminder-key")
            r2 = repo.create_reminder("local", reminder_create, "reminder-key")
            self.assertEqual(r1.id, r2.id)

    def test_snooze_creates_child_and_marks_parent_snoozed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "test.db")
            record = repo.save_record(
                "local",
                LifeRecordCreate(
                    record_type=RecordType.PURCHASE,
                    title="耳机",
                    amount=3499,
                    event_date=parse_date_text("2026-07-25", "Asia/Shanghai"),
                    deadline=parse_date_text("2026-08-01", "Asia/Shanghai"),
                ),
                "record-key",
            )
            reminder = repo.create_reminder(
                "local",
                ReminderCreate(
                    record_id=record.id,
                    scheduled_at=calculate_reminder_at(record.deadline, 2, "09:00", "Asia/Shanghai"),
                    reminder_type=ReminderType.RETURN_DEADLINE,
                    message="测试提醒",
                ),
                "reminder-key",
            )

            child = repo.snooze_reminder(
                "local",
                reminder.id,
                reminder.scheduled_at + timedelta(hours=1),
                "snooze-key",
            )
            parent = repo.get_reminder("local", reminder.id)
            self.assertEqual(parent.status, ReminderStatus.SNOOZED)
            self.assertEqual(child.status, ReminderStatus.PENDING)
            self.assertEqual(child.parent_id, reminder.id)

            cancelled = repo.cancel_reminder("local", child.id, user_confirmed=True)
            self.assertEqual(cancelled.status, ReminderStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
