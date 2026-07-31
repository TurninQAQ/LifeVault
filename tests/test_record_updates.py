from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lifevault.models.schemas import (
    LifeRecordCreate,
    RecordUpdatePatch,
    ReminderCreate,
    ReminderStatus,
    ReminderType,
)
from lifevault.records.update_planner import RecordUpdateError
from lifevault.storage.database import connect
from lifevault.storage.repository import VaultRepository


NOW = datetime(2026, 7, 31, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class RecordUpdateTest(unittest.TestCase):
    def test_preview_and_update_replan_reminder_atomically_and_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "record-update.db")
            record = self._purchase(repo)
            original = repo.create_reminder(
                "local",
                ReminderCreate(
                    record_id=record.id,
                    scheduled_at=datetime(
                        2099,
                        7,
                        30,
                        9,
                        0,
                        tzinfo=ZoneInfo("Asia/Shanghai"),
                    ),
                    reminder_type=ReminderType.RETURN_DEADLINE,
                    message="旧提醒",
                ),
                "return-reminder",
            )
            patch = RecordUpdatePatch(
                title="新相机",
                return_deadline=date(2099, 8, 8),
            )

            preview = repo.preview_record_update(
                "local",
                record.id,
                patch,
                expected_version=1,
                timezone_name="Asia/Shanghai",
                now=NOW,
            )

            self.assertEqual(preview.record.version, 2)
            self.assertEqual(
                preview.record.details["return_deadline"],
                "2099-08-08",
            )
            self.assertEqual(
                preview.reminders_to_create[0].scheduled_at.isoformat(),
                "2099-08-06T09:00:00+08:00",
            )
            self.assertEqual(repo.get_record("local", record.id).version, 1)
            self.assertEqual(
                repo.get_reminder("local", original.id).status,
                ReminderStatus.PENDING,
            )

            result = repo.update_record(
                "local",
                record.id,
                patch,
                expected_version=1,
                timezone_name="Asia/Shanghai",
                now=NOW,
                idempotency_key="update-camera",
                duplicate_confirmed=False,
            )
            repeated = repo.update_record(
                "local",
                record.id,
                patch,
                expected_version=1,
                timezone_name="Asia/Shanghai",
                now=NOW,
                idempotency_key="update-camera",
                duplicate_confirmed=False,
            )

            self.assertEqual(result.record.version, 2)
            self.assertEqual(result.record.title, "新相机")
            self.assertEqual(result.cancelled_reminders[0].status, ReminderStatus.CANCELLED)
            self.assertEqual(result.created_reminders[0].parent_id, original.id)
            self.assertIn("新相机", result.created_reminders[0].message)
            self.assertEqual(
                repeated.created_reminders[0].id,
                result.created_reminders[0].id,
            )
            self.assertEqual(len(repo.list_audit_logs("local", action="update_record")), 1)

    def test_title_only_update_can_replace_reminder_at_same_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "same-slot.db")
            record = self._purchase(repo)
            reminder = repo.create_reminder(
                "local",
                ReminderCreate(
                    record_id=record.id,
                    scheduled_at=datetime(
                        2099,
                        7,
                        30,
                        9,
                        0,
                        tzinfo=ZoneInfo("Asia/Shanghai"),
                    ),
                    reminder_type=ReminderType.RETURN_DEADLINE,
                    message="旧标题",
                ),
                "same-slot-reminder",
            )

            result = repo.update_record(
                "local",
                record.id,
                RecordUpdatePatch(title="新标题"),
                expected_version=1,
                timezone_name="Asia/Shanghai",
                now=NOW,
                idempotency_key="same-slot-update",
                duplicate_confirmed=False,
            )

            self.assertEqual(result.created_reminders[0].scheduled_at, reminder.scheduled_at)
            self.assertNotEqual(result.created_reminders[0].id, reminder.id)
            self.assertEqual(
                repo.get_reminder("local", reminder.id).status,
                ReminderStatus.CANCELLED,
            )

    def test_title_only_update_preserves_a_past_pending_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "past-title.db")
            record = self._purchase(repo)
            old_schedule = datetime(
                2026,
                7,
                31,
                9,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            )
            repo.create_reminder(
                "local",
                ReminderCreate(
                    record_id=record.id,
                    scheduled_at=old_schedule,
                    reminder_type=ReminderType.RETURN_DEADLINE,
                    message="旧标题",
                ),
                "past-title-reminder",
            )

            preview = repo.preview_record_update(
                "local",
                record.id,
                RecordUpdatePatch(title="新标题"),
                expected_version=1,
                timezone_name="Asia/Shanghai",
                now=NOW,
            )

            self.assertEqual(
                preview.reminders_to_create[0].scheduled_at,
                old_schedule,
            )

    def test_version_idempotency_and_sending_conflicts_do_not_partially_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "conflicts.db")
            record = self._purchase(repo)
            reminder = repo.create_reminder(
                "local",
                ReminderCreate(
                    record_id=record.id,
                    scheduled_at=datetime(
                        2026,
                        7,
                        31,
                        9,
                        0,
                        tzinfo=ZoneInfo("Asia/Shanghai"),
                    ),
                    reminder_type=ReminderType.RETURN_DEADLINE,
                    message="发送中",
                ),
                "sending-reminder",
            )
            claimed = repo.claim_due_reminders("local", NOW)
            self.assertEqual([item.id for item in claimed], [reminder.id])

            with self.assertRaises(RecordUpdateError) as sending:
                repo.update_record(
                    "local",
                    record.id,
                    RecordUpdatePatch(title="不能提交"),
                    expected_version=1,
                    timezone_name="Asia/Shanghai",
                    now=NOW,
                    idempotency_key="sending-conflict",
                    duplicate_confirmed=False,
                )
            self.assertEqual(sending.exception.code, "reminder_in_flight")
            self.assertEqual(repo.get_record("local", record.id).version, 1)

            with self.assertRaises(RecordUpdateError) as version:
                repo.preview_record_update(
                    "local",
                    record.id,
                    RecordUpdatePatch(notes="备注"),
                    expected_version=99,
                    timezone_name="Asia/Shanghai",
                    now=NOW,
                )
            self.assertEqual(version.exception.code, "version_conflict")
            self.assertEqual(version.exception.current_record.version, 1)

    def test_duplicate_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "duplicates.db")
            first = self._purchase(repo, key="first", title="第一台相机", order_number="ORDER-1")
            second = self._purchase(repo, key="second", title="第二台相机", order_number="ORDER-2")
            patch = RecordUpdatePatch(order_number="ORDER-1")

            preview = repo.preview_record_update(
                "local",
                second.id,
                patch,
                expected_version=1,
                timezone_name="Asia/Shanghai",
                now=NOW,
            )
            self.assertEqual(
                [candidate.record_id for candidate in preview.duplicate_candidates],
                [first.id],
            )

            with self.assertRaises(RecordUpdateError) as duplicate:
                repo.update_record(
                    "local",
                    second.id,
                    patch,
                    expected_version=1,
                    timezone_name="Asia/Shanghai",
                    now=NOW,
                    idempotency_key="duplicate-update",
                    duplicate_confirmed=False,
                )
            self.assertEqual(
                duplicate.exception.code,
                "duplicate_confirmation_required",
            )
            self.assertEqual(repo.get_record("local", second.id).details["order_number"], "ORDER-2")

            result = repo.update_record(
                "local",
                second.id,
                patch,
                expected_version=1,
                timezone_name="Asia/Shanghai",
                now=NOW,
                idempotency_key="duplicate-update-confirmed",
                duplicate_confirmed=True,
            )
            self.assertEqual(result.record.details["order_number"], "ORDER-1")

    def test_invalid_type_field_and_date_relation_return_field_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "validation.db")
            record = self._purchase(repo)
            cases = [
                (
                    RecordUpdatePatch(service_name="不允许"),
                    "service_name",
                ),
                (
                    RecordUpdatePatch(return_deadline=date(2099, 7, 1)),
                    "return_deadline",
                ),
            ]
            for patch, field in cases:
                with self.subTest(field=field), self.assertRaises(RecordUpdateError) as invalid:
                    repo.preview_record_update(
                        "local",
                        record.id,
                        patch,
                        expected_version=1,
                        timezone_name="Asia/Shanghai",
                        now=NOW,
                    )
                self.assertEqual(invalid.exception.code, "invalid_record_update")
                self.assertIn(field, invalid.exception.field_errors)

    def test_legacy_details_are_preserved_during_unrelated_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "legacy.db")
            record = self._purchase(repo)
            with connect(repo.database_path) as conn:
                conn.execute(
                    "UPDATE life_records SET details_json = ? WHERE id = ?",
                    (
                        '{"merchant":"京东","order_number":"ORDER-1",'
                        '"return_deadline":"2099-08-01","legacy_private":"keep"}',
                        record.id,
                    ),
                )

            result = repo.update_record(
                "local",
                record.id,
                RecordUpdatePatch(notes="新备注"),
                expected_version=1,
                timezone_name="Asia/Shanghai",
                now=NOW,
                idempotency_key="legacy-update",
                duplicate_confirmed=False,
            )

            self.assertEqual(result.record.details["legacy_private"], "keep")

    def test_existing_global_reminder_slot_schema_migrates_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "legacy-schema.db"
            setup_repo = VaultRepository(database_path)
            record = self._purchase(setup_repo)
            reminder = setup_repo.create_reminder(
                "local",
                ReminderCreate(
                    record_id=record.id,
                    scheduled_at=datetime(
                        2099,
                        7,
                        30,
                        9,
                        0,
                        tzinfo=ZoneInfo("Asia/Shanghai"),
                    ),
                    reminder_type=ReminderType.RETURN_DEADLINE,
                    message="迁移前提醒",
                ),
                "legacy-schema-reminder",
            )
            with connect(database_path) as conn:
                conn.executescript(
                    """
                    PRAGMA foreign_keys = OFF;
                    BEGIN IMMEDIATE;
                    ALTER TABLE reminders RENAME TO reminders_new_schema;
                    CREATE TABLE reminders (
                        id TEXT PRIMARY KEY,
                        record_id TEXT NOT NULL REFERENCES life_records(id),
                        user_id TEXT NOT NULL,
                        scheduled_at TEXT NOT NULL,
                        reminder_type TEXT NOT NULL,
                        message TEXT NOT NULL,
                        status TEXT NOT NULL,
                        parent_id TEXT REFERENCES reminders(id),
                        idempotency_key TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        sent_at TEXT,
                        UNIQUE(user_id, idempotency_key),
                        UNIQUE(record_id, reminder_type, scheduled_at)
                    );
                    INSERT INTO reminders
                    SELECT * FROM reminders_new_schema;
                    DROP TABLE reminders_new_schema;
                    COMMIT;
                    PRAGMA foreign_keys = ON;
                    """
                )

            repo = VaultRepository(database_path)
            migrated_record = repo.get_record("local", record.id)
            migrated_reminder = repo.get_reminder("local", reminder.id)
            self.assertEqual(migrated_record.title, "相机")
            self.assertEqual(migrated_reminder.message, "迁移前提醒")
            with connect(database_path) as conn:
                table_sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'reminders'"
                ).fetchone()["sql"]
                active_index = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' "
                    "AND name = 'idx_reminders_active_slot'"
                ).fetchone()["sql"]
            self.assertNotIn(
                "UNIQUE(record_id, reminder_type, scheduled_at)",
                table_sql,
            )
            self.assertIn("WHERE status IN ('pending', 'sending')", active_index)

            result = repo.update_record(
                "local",
                record.id,
                RecordUpdatePatch(title="迁移后相机"),
                expected_version=1,
                timezone_name="Asia/Shanghai",
                now=NOW,
                idempotency_key="legacy-schema-update",
                duplicate_confirmed=False,
            )
            self.assertEqual(result.created_reminders[0].scheduled_at, reminder.scheduled_at)

    @staticmethod
    def _purchase(
        repo: VaultRepository,
        *,
        key: str = "camera",
        title: str = "相机",
        order_number: str = "ORDER-1",
    ):
        return repo.save_record(
            "local",
            LifeRecordCreate(
                record_type="purchase",
                title=title,
                amount=5000,
                event_date=date(2099, 7, 25),
                deadline=date(2099, 8, 1),
                details={
                    "merchant": "京东",
                    "order_number": order_number,
                    "return_deadline": "2099-08-01",
                    "warranty_deadline": "2100-07-25",
                    "legacy_field": "preserved",
                },
            ),
            key,
        )


if __name__ == "__main__":
    unittest.main()
