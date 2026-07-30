from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from lifevault.config import Settings
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
from lifevault.worker.reminder_worker import ReminderWorker, quiet_hours_resume_at


class ReminderWorkerTest(unittest.TestCase):
    def test_successful_send_marks_reminder_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo = make_repo(tmp)
            now = aware_now()
            record_id, reminder_id = create_due_reminder(repo, now)
            desktop = RecordingProvider()
            worker = ReminderWorker(settings, repo, desktop_provider=desktop, console_provider=RecordingProvider())

            processed = worker.run_once(now)

            reminder = repo.get_reminder(settings.default_user_id, reminder_id)
            self.assertEqual(processed, 1)
            self.assertEqual(reminder.status, ReminderStatus.SENT)
            self.assertEqual(desktop.calls, [("LifeVault 到期提醒", "测试提醒", record_id)])
            audit = repo.list_audit_logs(settings.default_user_id, action="send_reminder")
            self.assertEqual([(log.target_id, log.result) for log in audit], [(reminder_id, "ok")])

    def test_repeated_run_does_not_send_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo = make_repo(tmp)
            now = aware_now()
            _record_id, reminder_id = create_due_reminder(repo, now)
            desktop = RecordingProvider()
            worker = ReminderWorker(settings, repo, desktop_provider=desktop, console_provider=RecordingProvider())

            self.assertEqual(worker.run_once(now), 1)
            self.assertEqual(worker.run_once(now + timedelta(minutes=1)), 0)

            reminder = repo.get_reminder(settings.default_user_id, reminder_id)
            self.assertEqual(reminder.status, ReminderStatus.SENT)
            self.assertEqual(len(desktop.calls), 1)

    def test_expired_auto_renew_subscription_rolls_over_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo = make_repo(tmp)
            now = datetime(2026, 7, 30, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            record, reminder = create_subscription_reminder(
                repo,
                deadline=date(2026, 7, 29),
                auto_renew=True,
                scheduled_at=datetime(2026, 7, 27, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
            desktop = RecordingProvider()
            worker = ReminderWorker(
                settings,
                repo,
                desktop_provider=desktop,
                console_provider=RecordingProvider(),
            )

            self.assertEqual(worker.run_once(now), 1)
            self.assertEqual(worker.run_once(now + timedelta(minutes=1)), 0)

            updated = repo.get_record(settings.default_user_id, record.id)
            reminders = repo.list_reminders(settings.default_user_id)
            self.assertEqual(updated.deadline, date(2026, 8, 29))
            self.assertEqual(updated.version, 2)
            self.assertEqual(updated.details["previous_renewal_date"], "2026-07-29")
            self.assertEqual(len(reminders), 2)
            self.assertEqual(repo.get_reminder(settings.default_user_id, reminder.id).status, ReminderStatus.SENT)
            next_reminder = next(item for item in reminders if item.id != reminder.id)
            self.assertEqual(next_reminder.status, ReminderStatus.PENDING)
            self.assertEqual(next_reminder.reminder_type, ReminderType.RENEWAL)
            self.assertEqual(next_reminder.scheduled_at.isoformat(), "2026-08-27T08:30:00+08:00")
            self.assertEqual(len(desktop.calls), 1)
            audit = repo.list_audit_logs(settings.default_user_id, action="rollover_subscription")
            self.assertEqual([(log.target_id, log.result) for log in audit], [(record.id, "ok")])
            self.assertIn('"billing_cycle": "monthly"', audit[0].params_summary or "")

    def test_manual_subscription_and_cancelled_reminder_do_not_roll_over(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo = make_repo(tmp)
            now = datetime(2026, 7, 30, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            manual, _manual_reminder = create_subscription_reminder(
                repo,
                deadline=date(2026, 7, 29),
                auto_renew=False,
                scheduled_at=datetime(2026, 7, 27, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                key_suffix="manual",
            )
            cancelled, cancelled_reminder = create_subscription_reminder(
                repo,
                deadline=date(2026, 7, 28),
                auto_renew=True,
                scheduled_at=datetime(2026, 7, 26, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                key_suffix="cancelled",
            )
            repo.cancel_reminder(
                settings.default_user_id,
                cancelled_reminder.id,
                user_confirmed=True,
            )
            worker = ReminderWorker(
                settings,
                repo,
                desktop_provider=RecordingProvider(),
                console_provider=RecordingProvider(),
            )

            self.assertEqual(worker.run_once(now), 1)

            self.assertEqual(repo.get_record(settings.default_user_id, manual.id).deadline, date(2026, 7, 29))
            self.assertEqual(repo.get_record(settings.default_user_id, cancelled.id).deadline, date(2026, 7, 28))
            self.assertEqual(len(repo.list_reminders(settings.default_user_id)), 2)
            self.assertEqual(
                repo.list_audit_logs(settings.default_user_id, action="rollover_subscription"),
                [],
            )

    def test_rollover_after_long_pause_schedules_only_a_future_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo = make_repo(tmp)
            record, reminder = create_subscription_reminder(
                repo,
                deadline=date(2026, 1, 31),
                auto_renew=True,
                scheduled_at=datetime(2026, 1, 29, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                key_suffix="long-pause",
            )
            repo.mark_reminder_sent(settings.default_user_id, reminder.id)
            now = datetime(2026, 5, 30, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            worker = ReminderWorker(
                settings,
                repo,
                desktop_provider=RecordingProvider(),
                console_provider=RecordingProvider(),
            )

            self.assertEqual(worker.run_once(now), 0)

            updated = repo.get_record(settings.default_user_id, record.id)
            reminders = repo.list_reminders(settings.default_user_id)
            next_reminder = next(item for item in reminders if item.id != reminder.id)
            self.assertEqual(updated.deadline, date(2026, 6, 30))
            self.assertEqual(next_reminder.scheduled_at.isoformat(), "2026-06-28T09:00:00+08:00")
            self.assertGreater(next_reminder.scheduled_at, now)

    def test_inactive_record_cancels_reminder_before_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo = make_repo(tmp)
            now = aware_now()
            record_id, reminder_id = create_due_reminder(repo, now)
            repo.update_record_status(settings.default_user_id, record_id, RecordStatus.COMPLETED, expected_version=1)
            desktop = RecordingProvider()
            worker = ReminderWorker(settings, repo, desktop_provider=desktop, console_provider=RecordingProvider())

            processed = worker.run_once(now)

            reminder = repo.get_reminder(settings.default_user_id, reminder_id)
            self.assertEqual(processed, 1)
            self.assertEqual(reminder.status, ReminderStatus.CANCELLED)
            self.assertEqual(desktop.calls, [])
            audit = repo.list_audit_logs(settings.default_user_id, action="cancel_reminder")
            self.assertEqual(audit[0].actor, "worker")
            self.assertEqual(audit[0].target_id, reminder_id)
            self.assertIn('"record_status": "completed"', audit[0].params_summary or "")

    def test_completed_purchase_still_sends_warranty_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo = make_repo(tmp)
            now = aware_now()
            record = repo.save_record(
                settings.default_user_id,
                LifeRecordCreate(
                    record_type=RecordType.PURCHASE,
                    title="已保留相机",
                    amount=5000,
                    deadline=now.date(),
                    status=RecordStatus.COMPLETED,
                ),
                "completed-warranty-record",
            )
            reminder = repo.create_reminder(
                settings.default_user_id,
                ReminderCreate(
                    record_id=record.id,
                    scheduled_at=now - timedelta(minutes=5),
                    reminder_type=ReminderType.WARRANTY_DEADLINE,
                    message="保修即将结束",
                ),
                "completed-warranty-reminder",
            )
            desktop = RecordingProvider()
            worker = ReminderWorker(
                settings,
                repo,
                desktop_provider=desktop,
                console_provider=RecordingProvider(),
            )

            self.assertEqual(worker.run_once(now), 1)

            self.assertEqual(
                repo.get_reminder(settings.default_user_id, reminder.id).status,
                ReminderStatus.SENT,
            )
            self.assertEqual(desktop.calls, [("LifeVault 到期提醒", "保修即将结束", record.id)])

    def test_returned_purchase_cancels_warranty_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo = make_repo(tmp)
            now = aware_now()
            record = repo.save_record(
                settings.default_user_id,
                LifeRecordCreate(
                    record_type=RecordType.PURCHASE,
                    title="已退相机",
                    amount=5000,
                    deadline=now.date(),
                    status=RecordStatus.RETURNED,
                ),
                "returned-warranty-record",
            )
            reminder = repo.create_reminder(
                settings.default_user_id,
                ReminderCreate(
                    record_id=record.id,
                    scheduled_at=now - timedelta(minutes=5),
                    reminder_type=ReminderType.WARRANTY_DEADLINE,
                    message="不应发送",
                ),
                "returned-warranty-reminder",
            )
            desktop = RecordingProvider()
            worker = ReminderWorker(
                settings,
                repo,
                desktop_provider=desktop,
                console_provider=RecordingProvider(),
            )

            self.assertEqual(worker.run_once(now), 1)

            self.assertEqual(
                repo.get_reminder(settings.default_user_id, reminder.id).status,
                ReminderStatus.CANCELLED,
            )
            self.assertEqual(desktop.calls, [])

    def test_desktop_failure_falls_back_to_console_and_marks_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo = make_repo(tmp)
            now = aware_now()
            record_id, reminder_id = create_due_reminder(repo, now)
            desktop = RecordingProvider(should_fail=True)
            console = RecordingProvider()
            worker = ReminderWorker(settings, repo, desktop_provider=desktop, console_provider=console)

            processed = worker.run_once(now)

            reminder = repo.get_reminder(settings.default_user_id, reminder_id)
            self.assertEqual(processed, 1)
            self.assertEqual(reminder.status, ReminderStatus.FAILED)
            self.assertEqual(desktop.calls, [("LifeVault 到期提醒", "测试提醒", record_id)])
            self.assertEqual(console.calls, [("LifeVault 到期提醒", "测试提醒", record_id)])
            audit = repo.list_audit_logs(settings.default_user_id, action="send_reminder")
            self.assertEqual(audit[0].result, "failed")
            self.assertIn("desktop_notification_failed", audit[0].params_summary or "")

    def test_both_notification_providers_failing_still_marks_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo = make_repo(tmp)
            now = aware_now()
            _record_id, reminder_id = create_due_reminder(repo, now)
            worker = ReminderWorker(
                settings,
                repo,
                desktop_provider=RecordingProvider(should_fail=True),
                console_provider=RecordingProvider(should_fail=True),
            )

            self.assertEqual(worker.run_once(now), 1)

            reminder = repo.get_reminder(settings.default_user_id, reminder_id)
            self.assertEqual(reminder.status, ReminderStatus.FAILED)
            audit = repo.list_audit_logs(settings.default_user_id, action="send_reminder")
            self.assertEqual(audit[0].result, "failed")

    def test_quiet_hours_snoozes_to_quiet_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo = make_repo(tmp)
            now = datetime(2026, 7, 27, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
            _record_id, reminder_id = create_due_reminder(repo, now)
            repo.update_preferences(
                settings.default_user_id,
                UserPreferencePatch(
                    quiet_hours_start="22:00",
                    quiet_hours_end="08:00",
                ),
                actor="worker",
            )
            desktop = RecordingProvider()
            worker = ReminderWorker(settings, repo, desktop_provider=desktop, console_provider=RecordingProvider())

            processed = worker.run_once(now)

            parent = repo.get_reminder(settings.default_user_id, reminder_id)
            children = [item for item in repo.list_reminders(settings.default_user_id) if item.parent_id == reminder_id]
            self.assertEqual(processed, 1)
            self.assertEqual(parent.status, ReminderStatus.SNOOZED)
            self.assertEqual(len(children), 1)
            self.assertEqual(children[0].status, ReminderStatus.PENDING)
            self.assertEqual(children[0].scheduled_at.isoformat(), "2026-07-28T08:00:00+08:00")
            self.assertEqual(desktop.calls, [])

    def test_invalid_quiet_hours_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo = make_repo(tmp)
            now = aware_now()
            _record_id, reminder_id = create_due_reminder(repo, now)
            with connect(repo.database_path) as conn:
                conn.execute(
                    """
                    INSERT INTO user_preferences(
                        user_id, default_time, quiet_hours_start, quiet_hours_end, default_advance_days
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (settings.default_user_id, "09:00", "bad", "08:00", 2),
                )
            desktop = RecordingProvider()
            worker = ReminderWorker(settings, repo, desktop_provider=desktop, console_provider=RecordingProvider())

            processed = worker.run_once(now)

            reminder = repo.get_reminder(settings.default_user_id, reminder_id)
            self.assertEqual(processed, 1)
            self.assertEqual(reminder.status, ReminderStatus.SENT)
            self.assertEqual(len(desktop.calls), 1)

    def test_quiet_hours_resume_at_handles_same_day_and_cross_midnight(self) -> None:
        same_day = datetime(2026, 7, 27, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        cross_midnight_late = datetime(2026, 7, 27, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        cross_midnight_early = datetime(2026, 7, 28, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertEqual(quiet_hours_resume_at(same_day, "12:00", "13:00").isoformat(), "2026-07-27T13:00:00+08:00")
        self.assertEqual(
            quiet_hours_resume_at(cross_midnight_late, "22:00", "08:00").isoformat(),
            "2026-07-28T08:00:00+08:00",
        )
        self.assertEqual(
            quiet_hours_resume_at(cross_midnight_early, "22:00", "08:00").isoformat(),
            "2026-07-28T08:00:00+08:00",
        )
        self.assertIsNone(quiet_hours_resume_at(same_day, "22:00", "08:00"))
        self.assertIsNone(quiet_hours_resume_at(same_day, "bad", "08:00"))


class RecordingProvider:
    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.calls: list[tuple[str, str, str]] = []

    def send(self, title: str, message: str, record_id: str) -> None:
        self.calls.append((title, message, record_id))
        if self.should_fail:
            raise RuntimeError("forced notification failure")


def make_repo(tmp: str) -> tuple[Settings, VaultRepository]:
    settings = Settings(
        database_path=Path(tmp) / "worker.db",
        use_qwen=False,
    )
    return settings, VaultRepository(settings.database_path)


def aware_now() -> datetime:
    return datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def create_due_reminder(repo: VaultRepository, now: datetime) -> tuple[str, str]:
    record = repo.save_record(
        "local",
        LifeRecordCreate(
            record_type=RecordType.PURCHASE,
            title="测试耳机",
            amount=299,
            deadline=now.date(),
        ),
        f"record-{now.isoformat()}",
    )
    reminder = repo.create_reminder(
        "local",
        ReminderCreate(
            record_id=record.id,
            scheduled_at=now - timedelta(minutes=5),
            reminder_type=ReminderType.RETURN_DEADLINE,
            message="测试提醒",
        ),
        f"reminder-{now.isoformat()}",
    )
    return record.id, reminder.id


def create_subscription_reminder(
    repo: VaultRepository,
    deadline: date,
    auto_renew: bool,
    scheduled_at: datetime,
    key_suffix: str = "auto",
):
    record = repo.save_record(
        "local",
        LifeRecordCreate(
            record_type=RecordType.SUBSCRIPTION,
            title=f"测试会员-{key_suffix}",
            amount=30,
            deadline=deadline,
            details={
                "billing_cycle": "monthly",
                "auto_renew": auto_renew,
                "renewal_anchor_day": deadline.day,
                "remind_before_days": 2,
                "reminder_time": scheduled_at.strftime("%H:%M"),
            },
        ),
        f"subscription-record-{key_suffix}",
    )
    reminder = repo.create_reminder(
        "local",
        ReminderCreate(
            record_id=record.id,
            scheduled_at=scheduled_at,
            reminder_type=ReminderType.RENEWAL,
            message="测试续费提醒",
        ),
        f"subscription-reminder-{key_suffix}",
    )
    return record, reminder


if __name__ == "__main__":
    unittest.main()
