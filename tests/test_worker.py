from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
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
    UserPreference,
)
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

    def test_quiet_hours_snoozes_to_quiet_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo = make_repo(tmp)
            now = datetime(2026, 7, 27, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
            _record_id, reminder_id = create_due_reminder(repo, now)
            repo.update_preferences(
                UserPreference(
                    user_id=settings.default_user_id,
                    quiet_hours_start="22:00",
                    quiet_hours_end="08:00",
                )
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
            repo.update_preferences(
                UserPreference(
                    user_id=settings.default_user_id,
                    quiet_hours_start="bad",
                    quiet_hours_end="08:00",
                )
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


if __name__ == "__main__":
    unittest.main()
