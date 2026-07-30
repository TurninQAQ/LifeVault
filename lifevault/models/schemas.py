from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    merchant: str | None = None
    order_number: str | None = None
    return_days: int | None = None
    warranty_months: int | None = None

    service_name: str | None = None
    billing_cycle: str | None = None
    next_renewal_text: str | None = None
    auto_renew: bool | None = None

    bill_name: str | None = None
    billing_period: str | None = None
    due_date_text: str | None = None

    reminder_requested: bool = False
    remind_before_days: int | None = None
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
    reminder: ReminderCreate | None = None
    warnings: list[str] = Field(default_factory=list)

    @property
    def is_ready_to_save(self) -> bool:
        return self.record is not None and not self.missing_fields


class SaveResult(BaseModel):
    record: LifeRecord
    reminder: Reminder | None = None
    duplicate_candidates: list[DuplicateCandidate] = Field(default_factory=list)


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
    reminder: dict[str, Any] | None = None
    saved_record_id: str | None = None
    reminder_id: str | None = None
    errors: list[str] = Field(default_factory=list)


class UserPreference(BaseModel):
    user_id: str
    default_time: str = "09:00"
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    default_advance_days: int = 2
