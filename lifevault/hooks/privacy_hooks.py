from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any
from uuid import UUID


CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
LONG_DIGIT_GROUP = re.compile(r"(?<!\d)(\d[ -]?){13,19}(?!\d)")
CHINESE_ID = re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])")
AUDIT_SUMMARY_MAX_CHARS = 500
AUDIT_PARAM_ALLOWLIST = {
    "save_record": frozenset({"record_type", "error_code"}),
    "update_record_status": frozenset(
        {
            "new_status",
            "old_version",
            "new_version",
            "cancelled_reminder_count",
            "reminder_types",
            "error_code",
        }
    ),
    "create_reminder": frozenset({"reminder_type", "scheduled_at", "error_code"}),
    "create_reminders": frozenset({"reminder_count", "reminder_types", "error_code"}),
    "snooze_reminder": frozenset({"new_reminder_id", "new_scheduled_at", "error_code"}),
    "cancel_reminder": frozenset({"record_status", "error_code"}),
    "send_reminder": frozenset({"delivery", "record_status", "error_code"}),
    "rollover_subscription": frozenset({"billing_cycle", "reminder_type", "error_code"}),
    "update_preferences": frozenset({"changed_fields", "error_code"}),
    "update_record": frozenset(
        {
            "changed_fields",
            "old_version",
            "new_version",
            "cancelled_reminder_count",
            "created_reminder_count",
            "reminder_types",
            "error_code",
        }
    ),
    "archive_record": frozenset(
        {
            "old_version",
            "new_version",
            "cancelled_reminder_count",
            "reminder_types",
            "error_code",
        }
    ),
    "restore_record": frozenset({"old_version", "new_version", "error_code"}),
}
AUDIT_ENUM_VALUES = {
    "record_type": frozenset({"purchase", "subscription", "bill"}),
    "new_status": frozenset({"active", "completed", "returned", "paid", "cancelled"}),
    "record_status": frozenset({"active", "completed", "returned", "paid", "cancelled"}),
    "reminder_type": frozenset(
        {"return_deadline", "warranty_deadline", "renewal", "bill_due", "custom"}
    ),
    "delivery": frozenset({"desktop", "console"}),
    "billing_cycle": frozenset({"monthly", "yearly", "weekly"}),
}
AUDIT_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
AUDIT_PREFERENCE_FIELDS = frozenset(
    {
        "default_time",
        "default_advance_days",
        "quiet_hours_start",
        "quiet_hours_end",
    }
)
AUDIT_RECORD_FIELDS = frozenset(
    {
        "title",
        "amount",
        "currency",
        "event_date",
        "notes",
        "merchant",
        "order_number",
        "return_deadline",
        "warranty_deadline",
        "service_name",
        "billing_cycle",
        "next_renewal_date",
        "auto_renew",
        "bill_name",
        "billing_period",
        "due_date",
    }
)


def sanitize_input(text: str, max_chars: int) -> str:
    cleaned = CONTROL_CHARS.sub("", text).strip()
    cleaned = CHINESE_ID.sub("[REDACTED_ID]", cleaned)
    cleaned = LONG_DIGIT_GROUP.sub("[REDACTED_NUMBER]", cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]
    return cleaned


def sanitize_audit_params(action: str, params: dict[str, Any] | str | None) -> str | None:
    allowed = AUDIT_PARAM_ALLOWLIST.get(action, frozenset())
    if not allowed or params is None:
        return None

    if isinstance(params, str):
        try:
            parsed = json.loads(params)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        params = parsed

    safe: dict[str, Any] = {}
    for key, value in params.items():
        if key not in allowed or value is None:
            continue
        safe_value = _audit_value(key, value)
        if safe_value is not None:
            safe[key] = safe_value
    if not safe:
        return None
    return sanitize_input(
        json.dumps(safe, ensure_ascii=False, sort_keys=True),
        AUDIT_SUMMARY_MAX_CHARS,
    )


def sanitize_audit_target_id(target_id: str | None) -> str | None:
    if not target_id:
        return None
    try:
        return str(UUID(target_id))
    except (AttributeError, TypeError, ValueError):
        return None


def _audit_value(key: str, value: Any) -> str | int | float | bool | list[str] | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if key in AUDIT_ENUM_VALUES:
        return value if isinstance(value, str) and value in AUDIT_ENUM_VALUES[key] else None
    if key == "error_code":
        return value if isinstance(value, str) and AUDIT_ERROR_CODE.fullmatch(value) else None
    if key in {"scheduled_at", "new_scheduled_at"}:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value).isoformat()
        except ValueError:
            return None
    if key == "new_reminder_id":
        if not isinstance(value, str):
            return None
        try:
            return str(UUID(value))
        except ValueError:
            return None
    if key == "changed_fields":
        if not isinstance(value, list):
            return None
        return sorted(
            {
                field
                for field in value
                if isinstance(field, str)
                and field in AUDIT_PREFERENCE_FIELDS | AUDIT_RECORD_FIELDS
            }
        )
    if key == "reminder_count":
        return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5 else None
    if key == "reminder_types":
        if not isinstance(value, list):
            return None
        allowed = AUDIT_ENUM_VALUES["reminder_type"]
        return sorted({item for item in value if isinstance(item, str) and item in allowed})
    if key in {"old_version", "new_version"}:
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
    if key in {"cancelled_reminder_count", "created_reminder_count"}:
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100 else None
    return None
