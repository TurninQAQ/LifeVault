from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from pydantic import ValidationError

from lifevault.models.schemas import (
    LifeRecordCreate,
    RecordStatus,
    RecordType,
    ReminderCreate,
    ReminderStatus,
    ReminderType,
    UserPreferencePatch,
)
from lifevault.storage.database import connect
from lifevault.storage.repository import VaultRepository
from lifevault.tools.date_tools import calculate_reminder_at, parse_date_text


class RepositoryTest(unittest.TestCase):
    def test_preferences_are_partial_idempotent_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "test.db"
            repo = VaultRepository(database_path)

            default = repo.get_preferences("local")
            self.assertEqual(default.default_time, "09:00")
            with connect(database_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM user_preferences").fetchone()[0]
            self.assertEqual(count, 0)

            changed = repo.update_preferences(
                "local",
                UserPreferencePatch(
                    default_time="07:30",
                    quiet_hours_start="22:00",
                    quiet_hours_end="08:00",
                ),
            )
            self.assertTrue(changed.changed)
            self.assertEqual(
                changed.changed_fields,
                ["default_time", "quiet_hours_end", "quiet_hours_start"],
            )
            self.assertEqual(changed.preference.default_advance_days, 2)

            unchanged = repo.update_preferences(
                "local",
                UserPreferencePatch(default_time="07:30"),
            )
            self.assertFalse(unchanged.changed)
            self.assertEqual(unchanged.changed_fields, [])
            logs = repo.list_audit_logs("local", action="update_preferences")
            self.assertEqual(len(logs), 1)
            self.assertIn("changed_fields", logs[0].params_summary or "")
            self.assertNotIn("07:30", logs[0].params_summary or "")
            self.assertNotIn("22:00", logs[0].params_summary or "")

    def test_preference_patch_rejects_invalid_or_incomplete_values(self) -> None:
        invalid_patches = [
            {},
            {"default_time": "7:30"},
            {"default_time": None},
            {"default_advance_days": 31},
            {"quiet_hours_start": "22:00"},
            {"quiet_hours_start": None, "quiet_hours_end": "08:00"},
        ]
        for patch in invalid_patches:
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                UserPreferencePatch.model_validate(patch)

    def test_partial_preference_update_does_not_rewrite_legacy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "test.db"
            repo = VaultRepository(database_path)
            with connect(database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO user_preferences(
                        user_id, default_time, quiet_hours_start, quiet_hours_end, default_advance_days
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    ("local", "09:00", "legacy-invalid", "08:00", 2),
                )

            result = repo.update_preferences(
                "local",
                UserPreferencePatch(default_time="07:30"),
            )

            self.assertTrue(result.changed)
            self.assertIsNone(result.preference.quiet_hours_start)
            with connect(database_path) as conn:
                row = conn.execute(
                    "SELECT quiet_hours_start, quiet_hours_end FROM user_preferences WHERE user_id = ?",
                    ("local",),
                ).fetchone()
            self.assertEqual(row["quiet_hours_start"], "legacy-invalid")
            self.assertEqual(row["quiet_hours_end"], "08:00")

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

    def test_purchase_date_search_includes_typed_warranty_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "test.db")
            record = repo.save_record(
                "local",
                LifeRecordCreate(
                    record_type=RecordType.PURCHASE,
                    title="保修中的相机",
                    amount=5000,
                    event_date=date(2026, 7, 25),
                    deadline=date(2026, 8, 1),
                    details={
                        "return_deadline": "2026-08-01",
                        "warranty_deadline": "2027-07-25",
                    },
                ),
                "purchase-with-warranty",
            )

            records = repo.search_records(
                "local",
                record_types=[RecordType.PURCHASE],
                date_from=date(2027, 7, 1),
                date_to=date(2027, 7, 31),
            )

            self.assertEqual([item.id for item in records], [record.id])

            event_records = repo.search_records(
                "local",
                record_types=[RecordType.PURCHASE],
                date_from=date(2026, 7, 25),
                date_to=date(2026, 7, 25),
            )
            self.assertEqual([item.id for item in event_records], [record.id])


if __name__ == "__main__":
    unittest.main()
