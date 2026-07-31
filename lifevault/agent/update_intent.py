from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError

from lifevault.models.schemas import (
    NaturalRecordUpdateIntent,
    RecordType,
    RecordUpdatePatch,
)
from lifevault.records.update_planner import COMMON_FIELDS, TYPE_FIELDS
from lifevault.tools.date_tools import parse_date_text, parse_subscription_renewal_date


DATE_TEXT_FIELDS = {
    "event_date_text": "event_date",
    "return_deadline_text": "return_deadline",
    "warranty_deadline_text": "warranty_deadline",
    "next_renewal_text": "next_renewal_date",
    "due_date_text": "due_date",
}
NON_DATE_FIELDS = {
    "title",
    "amount",
    "currency",
    "notes",
    "merchant",
    "order_number",
    "service_name",
    "billing_cycle",
    "auto_renew",
    "bill_name",
    "billing_period",
}


def build_record_update_changes(
    intent: NaturalRecordUpdateIntent,
    record_type: RecordType,
    timezone_name: str,
    frozen_at: datetime,
) -> tuple[dict[str, Any], dict[str, str], dict[str, list[str]]]:
    """Turn model output into a type-safe, absolute record patch."""

    allowed_fields = COMMON_FIELDS | TYPE_FIELDS[record_type]
    changes: dict[str, Any] = {}
    date_sources: dict[str, str] = {}
    field_errors: dict[str, list[str]] = {}
    intent_data = intent.model_dump(mode="python")

    for field in NON_DATE_FIELDS & allowed_fields:
        value = intent_data.get(field)
        if value is not None:
            changes[field] = value

    for text_field, patch_field in DATE_TEXT_FIELDS.items():
        if patch_field not in allowed_fields:
            continue
        source = intent_data.get(text_field)
        if source is None:
            continue
        if patch_field == "next_renewal_date":
            parsed = parse_subscription_renewal_date(
                source,
                intent.billing_cycle,
                timezone_name,
                frozen_at,
            )
        else:
            parsed = parse_date_text(source, timezone_name, frozen_at)
        if parsed is None:
            field_errors.setdefault(patch_field, []).append(
                f"Cannot parse date text: {source}"
            )
            continue
        changes[patch_field] = parsed.isoformat()
        date_sources[patch_field] = source

    for field in intent.clear_fields:
        if field not in allowed_fields:
            field_errors.setdefault(field, []).append(
                f"{field} cannot be edited for {record_type.value} records."
            )
            continue
        if field in changes:
            field_errors.setdefault(field, []).append(
                "A field cannot be assigned and cleared in the same update."
            )
            continue
        changes[field] = None

    if field_errors:
        return changes, date_sources, field_errors
    try:
        patch = RecordUpdatePatch.model_validate(changes)
    except ValidationError as exc:
        return changes, date_sources, validation_errors(exc)
    return patch.model_dump(mode="json", exclude_unset=True), date_sources, {}


def validate_structured_changes(
    changes: dict[str, Any],
    record_type: RecordType,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    allowed_fields = COMMON_FIELDS | TYPE_FIELDS[record_type]
    disallowed = sorted(set(changes) - allowed_fields)
    if disallowed:
        return {}, {
            field: [f"{field} cannot be edited for {record_type.value} records."]
            for field in disallowed
        }
    try:
        patch = RecordUpdatePatch.model_validate(changes)
    except ValidationError as exc:
        return {}, validation_errors(exc)
    return patch.model_dump(mode="json", exclude_unset=True), {}


def target_date_range(
    text: str | None,
    timezone_name: str,
    now: datetime,
) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    raw = re.sub(r"\s+", "", text)
    today = now.date()

    if raw in {"上个月", "上月"}:
        year, month = _shift_month(today.year, today.month, -1)
        return _month_range(year, month)
    if raw in {"这个月", "本月", "当月"}:
        return _month_range(today.year, today.month)
    if raw == "去年":
        return f"{today.year - 1}-01-01", f"{today.year - 1}-12-31"
    if raw == "今年":
        return f"{today.year}-01-01", f"{today.year}-12-31"

    match = re.fullmatch(r"(\d{4})年?(\d{1,2})月?", raw)
    if match:
        return _month_range(int(match.group(1)), int(match.group(2)))
    match = re.fullmatch(r"(\d{1,2})月", raw)
    if match:
        return _month_range(today.year, int(match.group(1)))

    parsed = parse_date_text(raw, timezone_name, now)
    if parsed is None:
        return None, None
    value = parsed.isoformat()
    return value, value


def validation_errors(exc: ValidationError) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    for item in exc.errors(include_url=False):
        location = item.get("loc") or ("changes",)
        field = str(location[0])
        errors.setdefault(field, []).append(str(item.get("msg", "Invalid value.")))
    return errors


def _month_range(year: int, month: int) -> tuple[str | None, str | None]:
    if not 1 <= month <= 12:
        return None, None
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1).isoformat(), date(year, month, last_day).isoformat()


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + delta
    return absolute // 12, absolute % 12 + 1
