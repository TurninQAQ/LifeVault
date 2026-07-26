from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from lifevault.config import Settings
from lifevault.models.schemas import RecordStatus
from lifevault.storage.repository import VaultRepository
from lifevault.tools.notification_tools import ConsoleNotificationProvider, DesktopNotificationProvider


class ReminderWorker:
    def __init__(self, settings: Settings, repository: VaultRepository | None = None):
        self.settings = settings
        self.repository = repository or VaultRepository(settings.database_path)
        self.desktop_provider = DesktopNotificationProvider()
        self.console_provider = ConsoleNotificationProvider()

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
                self.repository.mark_reminder_failed(self.settings.default_user_id, reminder.id, "record not found")
                processed += 1
                continue
            if record.status != RecordStatus.ACTIVE:
                self.repository.mark_reminder_cancelled_by_worker(
                    self.settings.default_user_id,
                    reminder.id,
                    f"record status is {record.status.value}",
                )
                processed += 1
                continue

            title = "LifeVault 到期提醒"
            try:
                self.desktop_provider.send(title, reminder.message, record_id=record.id)
            except Exception as exc:
                self.console_provider.send(title, reminder.message, record_id=record.id)
                self.repository.mark_reminder_failed(self.settings.default_user_id, reminder.id, str(exc))
            else:
                self.repository.mark_reminder_sent(self.settings.default_user_id, reminder.id)
            processed += 1
        return processed

    def run_forever(self, interval_seconds: int = 60) -> None:
        while True:
            self.run_once()
            time.sleep(interval_seconds)
