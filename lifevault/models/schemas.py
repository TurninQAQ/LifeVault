from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)


class RecordType(str, Enum):
    PURCHASE = "purchase"
    SUBSCRIPTION = "subscription"
    BILL = "bill"


class RecordStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    RETURNED = "returned"
    PAID = "paid"
    CANCELLED = "cancelled"


class ReminderStatus(str, Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    SNOOZED = "snoozed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ReminderType(str, Enum):
    RETURN_DEADLINE = "return_deadline"
    WARRANTY_DEADLINE = "warranty_deadline"
    RENEWAL = "renewal"
    BILL_DUE = "bill_due"
    CUSTOM = "custom"


class ToolName(str, Enum):
    PARSE_RELATIVE_DATE = "parse_relative_date"
    CALCULATE_DEADLINE = "calculate_deadline"
    CALCULATE_REMINDER_AT = "calculate_reminder_at"
    FIND_DUPLICATE = "find_duplicate"


ALLOWED_TOOL_PLAN = {tool.value for tool in ToolName}


class ExtractedRecordCandidate(BaseModel):
    """Model-produced candidate. It is never saved without deterministic validation."""

    model_config = ConfigDict(extra="ignore")

    intent: Literal["create_record", "search_records", "update_status", "unknown"] = "unknown"
    record_type: RecordType | None = None
    title: str | None = None
    amount: float | None = None
    currency: str = "CNY"

    event_date: date | None = None
    event_date_text: str | None = None
    deadline_date: date | None = None
    deadline_text: str | None = None
    return_deadline_date: date | None = None
    warranty_deadline_date: date | None = None
    next_renewal_date: date | None = None
    due_date: date | None = None

    merchant: str | None = None
    order_number: str | None = None
    return_days: int | None = Field(default=None, gt=0)
    warranty_months: int | None = Field(default=None, gt=0)
    return_deadline_text: str | None = None
    warranty_deadline_text: str | None = None

    service_name: str | None = None
    billing_cycle: str | None = None
    next_renewal_text: str | None = None
    auto_renew: bool | None = None

    bill_name: str | None = None
    billing_period: str | None = None
    due_date_text: str | None = None

    reminder_requested: bool = False
    return_reminder_requested: bool = False
    warranty_reminder_requested: bool = False
    remind_before_days: int | None = Field(default=None, ge=0, le=365)
    return_remind_before_days: int | None = Field(default=None, ge=0, le=365)
    warranty_remind_before_days: int | None = Field(default=None, ge=0, le=365)
    reminder_time: str | None = None
    notes: str | None = None
    search_query: str | None = None
    tool_plan: list[str] = Field(default_factory=list)

    @field_validator("title", "merchant", "order_number", "service_name", "bill_name", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("tool_plan")
    @classmethod
    def keep_allowed_tools(cls, value: list[str]) -> list[str]:
        return [tool for tool in value if tool in ALLOWED_TOOL_PLAN]


class CandidateCorrections(BaseModel):
    """User-authored candidate changes accepted only during record review."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )

    title: str | None = None
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = None
    event_date: date | None = None
    notes: str | None = None

    merchant: str | None = None
    order_number: str | None = None
    return_days: StrictInt | None = Field(default=None, ge=1, le=3650)
    warranty_months: StrictInt | None = Field(default=None, ge=1, le=1200)
    return_deadline_date: date | None = None
    warranty_deadline_date: date | None = None

    service_name: str | None = None
    billing_cycle: Literal["monthly", "yearly", "weekly", "unknown"] | None = None
    next_renewal_date: date | None = None
    auto_renew: StrictBool | None = None

    bill_name: str | None = None
    billing_period: str | None = None
    due_date: date | None = None

    reminder_requested: StrictBool | None = None
    return_reminder_requested: StrictBool | None = None
    warranty_reminder_requested: StrictBool | None = None
    remind_before_days: StrictInt | None = Field(default=None, ge=0, le=365)
    return_remind_before_days: StrictInt | None = Field(default=None, ge=0, le=365)
    warranty_remind_before_days: StrictInt | None = Field(default=None, ge=0, le=365)
    reminder_time: str | None = None

    @field_validator(
        "title",
        "merchant",
        "order_number",
        "service_name",
        "bill_name",
        "notes",
        "billing_period",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            return None
        if any(ord(character) < 32 for character in normalized):
            raise ValueError("Control characters are not allowed.")
        return normalized

    @field_validator("title", "merchant", "service_name", "bill_name")
    @classmethod
    def validate_short_text(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 200:
            raise ValueError("Must contain at most 200 characters.")
        return value

    @field_validator("order_number")
    @classmethod
    def validate_order_number(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 128:
            raise ValueError("Must contain at most 128 characters.")
        return value

    @field_validator("notes", "billing_period")
    @classmethod
    def validate_long_text(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 1000:
            raise ValueError("Must contain at most 1000 characters.")
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def validate_currency(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized):
            raise ValueError("Currency must be a three-letter code.")
        return normalized

    @field_validator("reminder_time")
    @classmethod
    def validate_reminder_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Reminder time must use HH:MM.")
        return value

    @field_validator("title", "amount", "currency")
    @classmethod
    def validate_required_values(cls, value: Any, info: Any) -> Any:
        if value is None:
            raise ValueError(f"{info.field_name} cannot be cleared.")
        return value

    @field_validator(
        "auto_renew",
        "reminder_requested",
        "return_reminder_requested",
        "warranty_reminder_requested",
    )
    @classmethod
    def validate_boolean_values(cls, value: bool | None, info: Any) -> bool:
        if value is None:
            raise ValueError(f"{info.field_name} must be true or false.")
        return value


class LifeRecordCreate(BaseModel):
    record_type: RecordType
    title: str
    amount: float | None = None
    currency: str = "CNY"
    event_date: date | None = None
    deadline: date | None = None
    status: RecordStatus = RecordStatus.ACTIVE
    details: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
    source_text_hash: str | None = None
    source_text_preview: str | None = None


class LifeRecord(LifeRecordCreate):
    id: str
    user_id: str
    version: int
    created_at: datetime
    updated_at: datetime


class ReminderCreate(BaseModel):
    record_id: str
    scheduled_at: datetime
    reminder_type: ReminderType
    message: str
    parent_id: str | None = None


class ReminderBatchCreate(BaseModel):
    reminders: list[ReminderCreate] = Field(min_length=1, max_length=5)
    idempotency_key: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_single_record(self) -> ReminderBatchCreate:
        record_ids = {reminder.record_id for reminder in self.reminders}
        if len(record_ids) != 1:
            raise ValueError("All reminders in a batch must belong to the same record.")
        unique_slots = {
            (reminder.reminder_type, reminder.scheduled_at)
            for reminder in self.reminders
        }
        if len(unique_slots) != len(self.reminders):
            raise ValueError("A reminder batch cannot contain duplicate reminder slots.")
        return self

    @property
    def record_id(self) -> str:
        return self.reminders[0].record_id


class Reminder(ReminderCreate):
    id: str
    user_id: str
    status: ReminderStatus
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None = None


class AuditLog(BaseModel):
    id: int
    user_id: str
    actor: str
    action: str
    target_id: str | None = None
    result: str
    params_summary: str | None = None
    created_at: datetime


class DuplicateCandidate(BaseModel):
    record_id: str
    title: str
    reason: str
    score: float


class DraftResult(BaseModel):
    thread_id: str = Field(default_factory=lambda: str(uuid4()))
    raw_input: str
    sanitized_input: str
    candidate: ExtractedRecordCandidate
    missing_fields: list[str] = Field(default_factory=list)
    duplicate_candidates: list[DuplicateCandidate] = Field(default_factory=list)
    record: LifeRecordCreate | None = None
    reminders: list[ReminderCreate] = Field(default_factory=list)
    reminder: ReminderCreate | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_reminders(cls, value: Any) -> Any:
        return _normalize_plural_fields(value, "reminders", "reminder")

    @property
    def is_ready_to_save(self) -> bool:
        return self.record is not None and not self.missing_fields


class SaveResult(BaseModel):
    record: LifeRecord
    reminders: list[Reminder] = Field(default_factory=list)
    reminder: Reminder | None = None
    duplicate_candidates: list[DuplicateCandidate] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_reminders(cls, value: Any) -> Any:
        return _normalize_plural_fields(value, "reminders", "reminder")


class GraphTurn(BaseModel):
    thread_id: str
    status: Literal["running", "interrupted", "completed", "cancelled"]
    interrupt_type: str | None = None
    prompt: str | None = None
    interrupt_payload: dict[str, Any] | None = None
    missing_fields: list[str] = Field(default_factory=list)
    duplicate_candidates: list[dict[str, Any]] = Field(default_factory=list)
    candidate: dict[str, Any] | None = None
    record: dict[str, Any] | None = None
    reminders: list[dict[str, Any]] = Field(default_factory=list)
    reminder: dict[str, Any] | None = None
    saved_record_id: str | None = None
    reminder_ids: list[str] = Field(default_factory=list)
    reminder_id: str | None = None
    field_errors: dict[str, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_reminders(cls, value: Any) -> Any:
        normalized = _normalize_plural_fields(value, "reminders", "reminder")
        return _normalize_plural_fields(normalized, "reminder_ids", "reminder_id")


class UserPreference(BaseModel):
    user_id: str
    default_time: str = "09:00"
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    default_advance_days: int = 2


class UserPreferencePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_time: str | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    default_advance_days: StrictInt | None = Field(default=None, ge=0, le=30)

    @field_validator("default_time", "quiet_hours_start", "quiet_hours_end")
    @classmethod
    def validate_clock(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            if info.field_name == "default_time":
                raise ValueError("default_time cannot be null.")
            return None
        parts = value.split(":")
        if len(parts) != 2 or any(len(part) != 2 or not part.isdigit() for part in parts):
            raise ValueError(f"{info.field_name} must use HH:MM.")
        hour, minute = (int(part) for part in parts)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError(f"{info.field_name} must use HH:MM.")
        return value

    @field_validator("default_advance_days")
    @classmethod
    def validate_advance_days(cls, value: int | None) -> int:
        if value is None:
            raise ValueError("default_advance_days cannot be null.")
        return value

    @model_validator(mode="after")
    def validate_patch(self) -> UserPreferencePatch:
        fields = self.model_fields_set
        if not fields:
            raise ValueError("At least one preference field is required.")
        quiet_start_set = "quiet_hours_start" in fields
        quiet_end_set = "quiet_hours_end" in fields
        if quiet_start_set != quiet_end_set:
            raise ValueError("quiet_hours_start and quiet_hours_end must be updated together.")
        if quiet_start_set and ((self.quiet_hours_start is None) != (self.quiet_hours_end is None)):
            raise ValueError("Quiet hours must be set or cleared together.")
        return self


def _normalize_plural_fields(value: Any, plural: str, singular: str) -> Any:
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    plural_value = normalized.get(plural)
    singular_value = normalized.get(singular)
    if not plural_value and singular_value:
        normalized[plural] = [singular_value]
    elif isinstance(plural_value, list) and plural_value and not singular_value:
        normalized[singular] = plural_value[0]
    return normalized


class UserPreferenceUpdateResult(BaseModel):
    preference: UserPreference
    changed: bool
    changed_fields: list[str] = Field(default_factory=list)
