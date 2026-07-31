from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from lifevault.models.schemas import (
    DuplicateCandidate,
    LifeRecord,
    RecordStatus,
    RecordType,
    RecordUpdatePatch,
    RecordUpdatePreview,
    Reminder,
    ReminderCreate,
    ReminderStatus,
    ReminderType,
)


COMMON_FIELDS = frozenset({"title", "amount", "currency", "event_date", "notes"})
TYPE_FIELDS = {
    RecordType.PURCHASE: frozenset(
        {
            "merchant",
            "order_number",
            "return_deadline",
            "warranty_deadline",
        }
    ),
    RecordType.SUBSCRIPTION: frozenset(
        {
            "service_name",
            "billing_cycle",
            "next_renewal_date",
            "auto_renew",
        }
    ),
    RecordType.BILL: frozenset({"bill_name", "billing_period", "due_date"}),
}
DUPLICATE_FIELDS = frozenset({"title", "amount", "event_date", "merchant", "order_number"})
TYPED_REMINDER_TYPES = {
    RecordType.PURCHASE: frozenset(
        {ReminderType.RETURN_DEADLINE, ReminderType.WARRANTY_DEADLINE}
    ),
    RecordType.SUBSCRIPTION: frozenset({ReminderType.RENEWAL}),
    RecordType.BILL: frozenset({ReminderType.BILL_DUE}),
}
DATE_FIELDS_BY_REMINDER = {
    ReminderType.RETURN_DEADLINE: "return_deadline",
    ReminderType.WARRANTY_DEADLINE: "warranty_deadline",
    ReminderType.RENEWAL: "next_renewal_date",
    ReminderType.BILL_DUE: "due_date",
}


class RecordUpdateError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        field_errors: dict[str, list[str]] | None = None,
        current_record: LifeRecord | None = None,
        duplicate_candidates: list[DuplicateCandidate] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.field_errors = field_errors or {}
        self.current_record = current_record
        self.duplicate_candidates = duplicate_candidates or []


def plan_record_update(
    current: LifeRecord,
    patch: RecordUpdatePatch,
    reminders: list[Reminder],
    timezone_name: str,
    now: datetime,
) -> RecordUpdatePreview:
    requested_fields = set(patch.model_fields_set)
    if not requested_fields:
        raise RecordUpdateError("no_changes", "At least one record field is required.")

    allowed_fields = COMMON_FIELDS | TYPE_FIELDS[current.record_type]
    disallowed = sorted(requested_fields - allowed_fields)
    if disallowed:
        raise RecordUpdateError(
            "invalid_record_update",
            "One or more fields cannot be edited for this record type.",
            field_errors={
                field: [f"{field} cannot be edited for {current.record_type.value} records."]
                for field in disallowed
            },
        )

    proposed = _apply_patch(current, patch)
    changed_fields = sorted(
        field
        for field in requested_fields
        if _field_value(current, field) != _field_value(proposed, field)
    )
    if not changed_fields:
        raise RecordUpdateError("no_changes", "The record update does not change any values.")

    field_errors = _cross_field_errors(proposed, set(changed_fields))
    if field_errors:
        raise RecordUpdateError(
            "invalid_record_update",
            "The record update failed validation.",
            field_errors=field_errors,
        )

    normalized_now = _normalize_now(now, timezone_name)
    affected_types = _affected_reminder_types(current.record_type, set(changed_fields))
    affected = [
        reminder
        for reminder in reminders
        if reminder.reminder_type in affected_types
    ]
    if any(reminder.status == ReminderStatus.SENDING for reminder in affected):
        raise RecordUpdateError(
            "reminder_in_flight",
            "A reminder affected by this update is currently being sent.",
        )

    reminders_to_cancel = [
        reminder
        for reminder in affected
        if reminder.status in {ReminderStatus.PENDING, ReminderStatus.SNOOZED}
    ]
    child_parent_ids = {
        reminder.parent_id
        for reminder in reminders
        if reminder.parent_id is not None
    }
    sources = [
        reminder
        for reminder in reminders_to_cancel
        if reminder.id not in child_parent_ids
    ]

    reminders_to_create: list[ReminderCreate] = []
    warnings: list[str] = []
    for source in sources:
        replacement, replacement_warnings = _replacement_reminder(
            current,
            proposed,
            source,
            set(changed_fields),
            timezone_name,
            normalized_now,
        )
        warnings.extend(replacement_warnings)
        if replacement is not None:
            reminders_to_create.append(replacement)
    unique_replacements: dict[tuple[ReminderType, datetime], ReminderCreate] = {}
    for reminder in reminders_to_create:
        unique_replacements.setdefault(
            (reminder.reminder_type, reminder.scheduled_at),
            reminder,
        )
    if len(unique_replacements) != len(reminders_to_create):
        warnings.append("Equivalent replacement reminders were merged into one reminder.")

    return RecordUpdatePreview(
        current_record=current,
        record=proposed.model_copy(
            update={
                "version": current.version + 1,
                "updated_at": normalized_now.astimezone(timezone.utc),
            }
        ),
        changed_fields=changed_fields,
        reminders_to_cancel=reminders_to_cancel,
        reminders_to_create=list(unique_replacements.values()),
        warnings=warnings,
    )


def update_needs_duplicate_confirmation(changed_fields: list[str]) -> bool:
    return bool(set(changed_fields) & DUPLICATE_FIELDS)


def _apply_patch(current: LifeRecord, patch: RecordUpdatePatch) -> LifeRecord:
    fields = set(patch.model_fields_set)
    updates: dict[str, Any] = {}
    details = dict(current.details)

    for field in COMMON_FIELDS:
        if field in fields:
            updates[field] = getattr(patch, field)

    if current.record_type == RecordType.PURCHASE:
        _set_detail(details, "merchant", patch.merchant, "merchant" in fields)
        _set_detail(details, "order_number", patch.order_number, "order_number" in fields)
        _set_date_detail(
            details,
            "return_deadline",
            patch.return_deadline,
            "return_deadline" in fields,
        )
        _set_date_detail(
            details,
            "warranty_deadline",
            patch.warranty_deadline,
            "warranty_deadline" in fields,
        )
        if fields & {"return_deadline", "warranty_deadline"}:
            updates["deadline"] = (
                _detail_date(details, "return_deadline")
                or _detail_date(details, "warranty_deadline")
            )
    elif current.record_type == RecordType.SUBSCRIPTION:
        _set_detail(details, "service_name", patch.service_name, "service_name" in fields)
        _set_detail(details, "billing_cycle", patch.billing_cycle, "billing_cycle" in fields)
        _set_detail(details, "auto_renew", patch.auto_renew, "auto_renew" in fields)
        if "next_renewal_date" in fields:
            updates["deadline"] = patch.next_renewal_date
            if patch.next_renewal_date is None:
                details.pop("next_renewal_source", None)
            else:
                details["next_renewal_source"] = "record_update"
        if fields & {"billing_cycle", "next_renewal_date"}:
            renewal = updates.get("deadline", current.deadline)
            anchor = _renewal_anchor(renewal, details.get("billing_cycle"))
            _set_detail(details, "renewal_anchor_day", anchor, True)
    elif current.record_type == RecordType.BILL:
        _set_detail(details, "bill_name", patch.bill_name, "bill_name" in fields)
        _set_detail(details, "billing_period", patch.billing_period, "billing_period" in fields)
        if "due_date" in fields:
            updates["deadline"] = patch.due_date

    updates["details"] = details
    return current.model_copy(update=updates)


def _field_value(record: LifeRecord, field: str) -> Any:
    if field in COMMON_FIELDS:
        return getattr(record, field)
    if field in {"merchant", "order_number", "service_name", "billing_cycle", "auto_renew"}:
        return record.details.get(field)
    if field in {"bill_name", "billing_period"}:
        return record.details.get(field)
    if field == "return_deadline":
        return _deadline_for(record, ReminderType.RETURN_DEADLINE)
    if field == "warranty_deadline":
        return _deadline_for(record, ReminderType.WARRANTY_DEADLINE)
    if field in {"next_renewal_date", "due_date"}:
        return record.deadline
    return None


def _cross_field_errors(
    record: LifeRecord,
    changed_fields: set[str],
) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    if record.record_type == RecordType.PURCHASE:
        for field, reminder_type, label in (
            ("return_deadline", ReminderType.RETURN_DEADLINE, "Return"),
            ("warranty_deadline", ReminderType.WARRANTY_DEADLINE, "Warranty"),
        ):
            deadline = _deadline_for(record, reminder_type)
            if (
                changed_fields & {"event_date", field}
                and record.event_date
                and deadline
                and deadline < record.event_date
            ):
                errors.setdefault(field, []).append(
                    f"{label} deadline cannot be earlier than the purchase date."
                )
    elif (
        record.record_type == RecordType.SUBSCRIPTION
        and changed_fields & {"event_date", "next_renewal_date"}
        and record.event_date
        and record.deadline
        and record.deadline < record.event_date
    ):
        errors.setdefault("next_renewal_date", []).append(
            "Next renewal date cannot be earlier than the event date."
        )
    return errors


def _affected_reminder_types(
    record_type: RecordType,
    changed_fields: set[str],
) -> set[ReminderType]:
    affected: set[ReminderType] = set()
    supported = TYPED_REMINDER_TYPES[record_type]
    if "title" in changed_fields:
        affected.update(supported)
    for reminder_type in supported:
        if DATE_FIELDS_BY_REMINDER[reminder_type] in changed_fields:
            affected.add(reminder_type)
    return affected


def _replacement_reminder(
    current: LifeRecord,
    proposed: LifeRecord,
    source: Reminder,
    changed_fields: set[str],
    timezone_name: str,
    now: datetime,
) -> tuple[ReminderCreate | None, list[str]]:
    warnings: list[str] = []
    if not _record_allows_reminder(
        proposed.record_type,
        proposed.status,
        source.reminder_type,
    ):
        warnings.append(
            f"{source.reminder_type.value} reminder was removed because the record status is not eligible."
        )
        return None, warnings

    new_deadline = _deadline_for(proposed, source.reminder_type)
    if new_deadline is None:
        warnings.append(
            f"{source.reminder_type.value} reminder was removed because its deadline was cleared."
        )
        return None, warnings
    if new_deadline < now.date():
        warnings.append(
            f"{source.reminder_type.value} deadline has already passed; replacement reminder was not created."
        )
        return None, warnings

    source_local = _normalize_now(source.scheduled_at, timezone_name)
    date_field = DATE_FIELDS_BY_REMINDER[source.reminder_type]
    advance_days = 0
    deadline_changed = date_field in changed_fields
    if deadline_changed:
        old_deadline = _deadline_for(current, source.reminder_type)
        if old_deadline is None:
            warnings.append(
                f"{source.reminder_type.value} reminder could not be replanned because its previous deadline is missing."
            )
            return None, warnings
        advance_days = (old_deadline - source_local.date()).days
        scheduled_date = new_deadline - timedelta(days=advance_days)
        scheduled_at = datetime.combine(
            scheduled_date,
            time(
                hour=source_local.hour,
                minute=source_local.minute,
                second=source_local.second,
                microsecond=source_local.microsecond,
                tzinfo=ZoneInfo(timezone_name),
            ),
        )
    else:
        scheduled_at = source_local
        advance_days = (new_deadline - source_local.date()).days

    if deadline_changed and scheduled_at <= now:
        scheduled_at = now.replace(microsecond=0)
        warnings.append(
            f"{source.reminder_type.value} advance time has passed; replacement reminder will be scheduled immediately."
        )

    return (
        ReminderCreate(
            record_id=proposed.id,
            scheduled_at=scheduled_at,
            reminder_type=source.reminder_type,
            message=_reminder_message(
                proposed.title,
                source.reminder_type,
                max(0, advance_days),
            ),
            parent_id=source.id,
        ),
        warnings,
    )


def _deadline_for(record: LifeRecord, reminder_type: ReminderType) -> date | None:
    if reminder_type == ReminderType.RETURN_DEADLINE:
        return _detail_date(record.details, "return_deadline") or (
            record.deadline
            if record.record_type == RecordType.PURCHASE
            and not _detail_date(record.details, "warranty_deadline")
            else None
        )
    if reminder_type == ReminderType.WARRANTY_DEADLINE:
        return _detail_date(record.details, "warranty_deadline")
    if reminder_type in {ReminderType.RENEWAL, ReminderType.BILL_DUE}:
        return record.deadline
    return None


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


def _reminder_message(
    title: str,
    reminder_type: ReminderType,
    advance_days: int,
) -> str:
    if reminder_type == ReminderType.RETURN_DEADLINE:
        return f"你的「{title}」预计还有 {advance_days} 天结束退货期。"
    if reminder_type == ReminderType.WARRANTY_DEADLINE:
        return f"你的「{title}」预计还有 {advance_days} 天结束保修期。"
    if reminder_type == ReminderType.RENEWAL:
        return f"你的「{title}」预计还有 {advance_days} 天续费。"
    return f"你的「{title}」预计还有 {advance_days} 天到缴费截止日。"


def _set_detail(
    details: dict[str, Any],
    key: str,
    value: Any,
    requested: bool,
) -> None:
    if not requested:
        return
    if value is None:
        details.pop(key, None)
    else:
        details[key] = value


def _set_date_detail(
    details: dict[str, Any],
    key: str,
    value: date | None,
    requested: bool,
) -> None:
    _set_detail(details, key, value.isoformat() if value else None, requested)


def _detail_date(details: dict[str, Any], key: str) -> date | None:
    value = details.get(key)
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _renewal_anchor(
    renewal: date | None,
    billing_cycle: Any,
) -> int | str | None:
    if not renewal or billing_cycle not in {"monthly", "yearly", "weekly"}:
        return None
    if billing_cycle == "weekly":
        return renewal.weekday()
    if billing_cycle == "yearly":
        return f"{renewal.month:02d}-{renewal.day:02d}"
    return renewal.day


def _normalize_now(value: datetime, timezone_name: str) -> datetime:
    target = ZoneInfo(timezone_name)
    if value.tzinfo is None:
        return value.replace(tzinfo=target)
    return value.astimezone(target)
