from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from lifevault.models.schemas import (
    LifeRecordCreate,
    RecordType,
    ReminderBatchCreate,
    ReminderCreate,
    ReminderType,
    UserPreferencePatch,
)
from lifevault.storage.database import connect
from lifevault.storage.repository import VaultRepository


class AuditRepositoryTest(unittest.TestCase):
    def test_successful_write_is_audited_once_without_sensitive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "audit.db")
            record = LifeRecordCreate(
                record_type=RecordType.PURCHASE,
                title="私人订单标题",
                notes="不要写入审计",
            )

            first = repo.save_record("local", record, "secret-idempotency-key", actor="mcp")
            second = repo.save_record("local", record, "secret-idempotency-key", actor="mcp")

            self.assertEqual(first.id, second.id)
            logs = repo.list_audit_logs("local", action="save_record")
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].target_id, first.id)
            self.assertEqual(logs[0].result, "ok")
            self.assertIn('"record_type": "purchase"', logs[0].params_summary or "")
            self.assertNotIn("私人订单标题", logs[0].params_summary or "")
            self.assertNotIn("secret-idempotency-key", logs[0].params_summary or "")

    def test_audit_failure_rolls_back_business_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = FailingAuditRepository(Path(tmp) / "audit.db")
            with self.assertRaises(RuntimeError):
                repo.save_record(
                    "local",
                    LifeRecordCreate(record_type=RecordType.PURCHASE, title="不会提交"),
                    "rollback-key",
                )

            self.assertEqual(repo.search_records("local"), [])

    def test_audit_failure_rolls_back_preference_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = FailingAuditRepository(Path(tmp) / "audit.db")
            with self.assertRaises(RuntimeError):
                repo.update_preferences(
                    "local",
                    UserPreferencePatch(default_time="07:30"),
                )

            self.assertEqual(repo.get_preferences("local").default_time, "09:00")

    def test_audit_failure_rolls_back_batch_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "audit.db"
            setup_repo = VaultRepository(database_path)
            record = setup_repo.save_record(
                "local",
                LifeRecordCreate(record_type=RecordType.PURCHASE, title="批量回滚"),
                "batch-rollback-record",
            )
            repo = FailingAuditRepository(database_path)
            batch = ReminderBatchCreate(
                reminders=[
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
                        message="不要提交",
                    ),
                    ReminderCreate(
                        record_id=record.id,
                        scheduled_at=datetime(
                            2100,
                            6,
                            25,
                            9,
                            0,
                            tzinfo=ZoneInfo("Asia/Shanghai"),
                        ),
                        reminder_type=ReminderType.WARRANTY_DEADLINE,
                        message="也不要提交",
                    ),
                ],
                idempotency_key="batch-rollback",
            )

            with self.assertRaises(RuntimeError):
                repo.create_reminders("local", batch)

            self.assertEqual(repo.list_reminders("local"), [])
            with connect(database_path) as conn:
                batch_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM reminder_batches"
                ).fetchone()["count"]
            self.assertEqual(batch_count, 0)

    def test_query_filters_user_scope_and_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "audit.db")
            record_id = str(uuid4())
            reminder_id = str(uuid4())
            other_record_id = str(uuid4())
            oldest = repo.record_audit_event(
                "local",
                actor="mcp",
                action="save_record",
                target_id=record_id,
                result="rejected",
                params={"record_type": "purchase", "error_code": "confirmation_required"},
            )
            middle = repo.record_audit_event(
                "local",
                actor="worker",
                action="send_reminder",
                target_id=reminder_id,
                result="failed",
                params={"error_code": "desktop_notification_failed"},
            )
            newest = repo.record_audit_event(
                "local",
                actor="mcp",
                action="update_record_status",
                target_id=record_id,
                result="ok",
                params={"new_status": "completed"},
            )
            repo.record_audit_event(
                "other",
                actor="mcp",
                action="save_record",
                target_id=other_record_id,
                result="ok",
                params={"record_type": "bill"},
            )

            first_page = repo.list_audit_logs("local", limit=2)
            self.assertEqual([log.id for log in first_page], [newest.id, middle.id])
            second_page = repo.list_audit_logs("local", before_id=middle.id, limit=2)
            self.assertEqual([log.id for log in second_page], [oldest.id])
            failed = repo.list_audit_logs("local", actor="worker", result="failed")
            self.assertEqual([log.id for log in failed], [middle.id])
            self.assertNotIn(other_record_id, [log.target_id for log in repo.list_audit_logs("local")])

    def test_reader_removes_disallowed_fields_from_legacy_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "audit.db"
            repo = VaultRepository(database_path)
            with connect(database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO audit_logs(
                        user_id, actor, action, target_id, result, params_summary, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "local",
                        "mcp",
                        "save_record",
                        "legacy-record",
                        "ok",
                        '{"record_type": "purchase", "title": "旧敏感标题"}',
                        "2026-07-30T00:00:00+00:00",
                    ),
                )

            log = repo.list_audit_logs("local")[0]
            self.assertIn('"record_type": "purchase"', log.params_summary or "")
            self.assertNotIn("旧敏感标题", log.params_summary or "")

    def test_allowed_field_names_still_require_safe_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = VaultRepository(Path(tmp) / "audit.db")
            log = repo.record_audit_event(
                "local",
                actor="mcp",
                action="save_record",
                target_id="PRIVATE-TARGET-ID",
                result="rejected",
                params={
                    "record_type": "PRIVATE-VALUE-IN-ALLOWED-FIELD",
                    "error_code": "invalid_record",
                },
            )

            self.assertIn("invalid_record", log.params_summary or "")
            self.assertNotIn("PRIVATE-VALUE", log.params_summary or "")
            self.assertIsNone(log.target_id)


class FailingAuditRepository(VaultRepository):
    def _audit_conn(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("forced audit failure")


if __name__ == "__main__":
    unittest.main()
