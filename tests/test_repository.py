from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from lifevault.models.schemas import LifeRecordCreate, RecordStatus, RecordType, ReminderCreate, ReminderStatus, ReminderType
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

    def test_list_upcoming_subscriptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "test.db")
            repo.save_record(
                "local",
                LifeRecordCreate(
                    record_type=RecordType.SUBSCRIPTION,
                    title="腾讯视频",
                    amount=30,
                    deadline=date(2026, 8, 15),
                    details={"billing_cycle": "monthly", "auto_renew": True},
                ),
                "subscription-auto",
            )
            repo.save_record(
                "local",
                LifeRecordCreate(
                    record_type=RecordType.SUBSCRIPTION,
                    title="手动会员",
                    amount=20,
                    deadline=date(2026, 8, 20),
                    details={"billing_cycle": "monthly", "auto_renew": False},
                ),
                "subscription-manual",
            )
            repo.save_record(
                "local",
                LifeRecordCreate(
                    record_type=RecordType.SUBSCRIPTION,
                    title="过远会员",
                    amount=100,
                    deadline=date(2026, 12, 1),
                    details={"billing_cycle": "yearly", "auto_renew": True},
                ),
                "subscription-far",
            )
            repo.save_record(
                "local",
                LifeRecordCreate(
                    record_type=RecordType.SUBSCRIPTION,
                    title="已取消会员",
                    amount=10,
                    deadline=date(2026, 8, 10),
                    status=RecordStatus.CANCELLED,
                    details={"billing_cycle": "monthly", "auto_renew": False},
                ),
                "subscription-cancelled",
            )

            all_upcoming = repo.list_upcoming_subscriptions("local", date_from=date(2026, 7, 27), days=30)
            self.assertEqual([record.title for record in all_upcoming], ["腾讯视频", "手动会员"])

            manual_only = repo.list_upcoming_subscriptions(
                "local",
                date_from=date(2026, 7, 27),
                days=30,
                include_auto_renew=False,
            )
            self.assertEqual([record.title for record in manual_only], ["手动会员"])


if __name__ == "__main__":
    unittest.main()
