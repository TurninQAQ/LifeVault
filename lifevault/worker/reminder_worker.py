from __future__ import annotations

import time
from datetime import datetime, time as dt_time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from lifevault.config import Settings
from lifevault.models.schemas import RecordStatus
from lifevault.storage.repository import VaultRepository
from lifevault.tools.notification_tools import ConsoleNotificationProvider, DesktopNotificationProvider
from lifevault.tools.idempotency import stable_key


class NotificationProvider(Protocol):
    def send(self, title: str, message: str, record_id: str) -> None:
        ...


class ReminderWorker:
    def __init__(
        self,
        settings: Settings,
        repository: VaultRepository | None = None,
        desktop_provider: NotificationProvider | None = None,
        console_provider: NotificationProvider | None = None,
    ):
        self.settings = settings
        self.repository = repository or VaultRepository(settings.database_path)
        self.desktop_provider = desktop_provider or DesktopNotificationProvider()
        self.console_provider = console_provider or ConsoleNotificationProvider()

    def run_once(self, now: datetime | None = None) -> int:
        current = now or datetime.now(ZoneInfo(self.settings.default_timezone))
        reminders = self.repository.claim_due_reminders(
            self.settings.default_user_id,
            current,
            limit=20,
        )
        processed = 0
        for reminder in reminders:
            record = self.repository.get_record(self.settings.default_user_id, reminder.record_id)
            if record is None:
                self.repository.mark_reminder_failed(self.settings.default_user_id, reminder.id, "record_not_found")
                processed += 1
                continue
            if record.status != RecordStatus.ACTIVE:
                self.repository.mark_reminder_cancelled_by_worker(
                    self.settings.default_user_id,
                    reminder.id,
                    record.status.value,
                )
                processed += 1
                continue

            preference = self.repository.get_preferences(self.settings.default_user_id)
            quiet_until = quiet_hours_resume_at(current, preference.quiet_hours_start, preference.quiet_hours_end)
            if quiet_until is not None:
                snooze_key = stable_key(
                    "quiet-hours",
                    self.settings.default_user_id,
                    reminder.id,
                    quiet_until.isoformat(),
                )
                self.repository.snooze_reminder(
                    self.settings.default_user_id,
                    reminder.id,
                    quiet_until,
                    idempotency_key=snooze_key,
                    actor="worker",
                )
                processed += 1
                continue

            title = "LifeVault 到期提醒"
            try:
                self.desktop_provider.send(title, reminder.message, record_id=record.id)
            except Exception:
                try:
                    self.console_provider.send(title, reminder.message, record_id=record.id)
                except Exception:
                    pass
                self.repository.mark_reminder_failed(
                    self.settings.default_user_id,
                    reminder.id,
                    "desktop_notification_failed",
                )
            else:
                self.repository.mark_reminder_sent(self.settings.default_user_id, reminder.id)
            processed += 1
        return processed

    def run_forever(self, interval_seconds: int = 60) -> None:
        while True:
            self.run_once()
            time.sleep(interval_seconds)


def quiet_hours_resume_at(
    now: datetime,
    quiet_start: str | None,
    quiet_end: str | None,
) -> datetime | None:
    start = _parse_clock_time(quiet_start)
    end = _parse_clock_time(quiet_end)
    if start is None or end is None or start == end:
        return None

    current = now.timetz().replace(tzinfo=None)
    if start < end:
        if start <= current < end:
            return datetime.combine(now.date(), end, tzinfo=now.tzinfo)
        return None

    if current >= start:
        return datetime.combine(now.date() + timedelta(days=1), end, tzinfo=now.tzinfo)
    if current < end:
        return datetime.combine(now.date(), end, tzinfo=now.tzinfo)
    return None


def _parse_clock_time(value: str | None) -> dt_time | None:
    if not value:
        return None
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dt_time(hour=hour, minute=minute)
