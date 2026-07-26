from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lifevault.models.schemas import LifeRecordCreate, RecordType, ReminderCreate, ReminderType
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


if __name__ == "__main__":
    unittest.main()
