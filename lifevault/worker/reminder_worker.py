from __future__ import annotations

import time
from datetime import date, datetime, time as dt_time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from lifevault.config import Settings
from lifevault.models.schemas import RecordStatus, RecordType, ReminderCreate, ReminderType
from lifevault.storage.repository import VaultRepository
from lifevault.tools.date_tools import calculate_next_renewal_date, calculate_reminder_at
from lifevault.tools.idempotency import stable_key
from lifevault.tools.notification_tools import ConsoleNotificationProvider, DesktopNotificationProvider


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
        current = _normalize_now(now, self.settings.default_timezone)
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
            if not _record_allows_reminder(record.record_type, record.status, reminder.reminder_type):
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
        self._rollover_expired_subscriptions(current)
        return processed

    def _rollover_expired_subscriptions(self, current: datetime) -> None:
        preference = self.repository.get_preferences(self.settings.default_user_id)
        records = self.repository.list_subscription_rollover_candidates(
            self.settings.default_user_id,
            before=current.date(),
        )
        for record in records:
            billing_cycle = str(record.details["billing_cycle"])
            renewal_anchor = record.details.get("renewal_anchor_day")
            before_days = record.details.get("remind_before_days")
            if isinstance(before_days, bool) or not isinstance(before_days, int) or not 0 <= before_days <= 30:
                before_days = preference.default_advance_days
            reminder_time = record.details.get("reminder_time")
            if not isinstance(reminder_time, str):
                reminder_time = preference.default_time

            next_deadline = calculate_next_renewal_date(
                record.deadline,
                billing_cycle,
                today=current.date() + timedelta(days=1),
                renewal_anchor=renewal_anchor,
            )
            if next_deadline is None:
                continue

            scheduled_at = _calculate_rollover_reminder_at(
                next_deadline,
                before_days,
                reminder_time,
                self.settings.default_timezone,
                preference.default_time,
            )
            while scheduled_at <= current:
                following_deadline = calculate_next_renewal_date(
                    next_deadline,
                    billing_cycle,
                    today=next_deadline + timedelta(days=1),
                    renewal_anchor=renewal_anchor,
                )
                if following_deadline is None:
                    break
                next_deadline = following_deadline
                scheduled_at = _calculate_rollover_reminder_at(
                    next_deadline,
                    before_days,
                    reminder_time,
                    self.settings.default_timezone,
                    preference.default_time,
                )
            if scheduled_at <= current:
                continue

            reminder = ReminderCreate(
                record_id=record.id,
                scheduled_at=scheduled_at,
                reminder_type=ReminderType.RENEWAL,
                message=f"你的「{record.title}」预计还有 {before_days} 天续费。",
            )
            idempotency_key = stable_key(
                "subscription-rollover",
                self.settings.default_user_id,
                record.id,
                next_deadline.isoformat(),
                scheduled_at.isoformat(),
            )
            self.repository.rollover_subscription(
                self.settings.default_user_id,
                record.id,
                record.version,
                next_deadline,
                reminder,
                idempotency_key,
            )

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


def _record_allows_reminder(
    record_type: RecordType,
    record_status: RecordStatus,
    reminder_type: ReminderType,
) -> bool:
    if record_status == RecordStatus.ACTIVE:
        return True
    return (
        record_type == RecordType.PURCHASE
        and record_status == RecordStatus.COMPLETED
        and reminder_type == ReminderType.WARRANTY_DEADLINE
    )


def _normalize_now(value: datetime | None, timezone_name: str) -> datetime:
    timezone = ZoneInfo(timezone_name)
    if value is None:
        return datetime.now(timezone)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _calculate_rollover_reminder_at(
    deadline: date,
    before_days: int,
    reminder_time: str,
    timezone_name: str,
    fallback_time: str,
) -> datetime:
    try:
        return calculate_reminder_at(
            deadline,
            before_days,
            reminder_time,
            timezone_name,
        )
    except ValueError:
        return calculate_reminder_at(
            deadline,
            before_days,
            fallback_time,
            timezone_name,
        )
