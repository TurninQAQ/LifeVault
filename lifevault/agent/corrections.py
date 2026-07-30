from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from lifevault.models.schemas import (
    CandidateCorrections,
    ExtractedRecordCandidate,
    RecordType,
)

if TYPE_CHECKING:
    from lifevault.agent.service import LifeVaultAgent


COMMON_FIELDS = {
    "title",
    "amount",
    "currency",
    "event_date",
    "notes",
    "reminder_time",
}

TYPE_FIELDS = {
    RecordType.PURCHASE: {
        "merchant",
        "order_number",
        "return_days",
        "warranty_months",
        "return_deadline_date",
        "warranty_deadline_date",
        "return_reminder_requested",
        "warranty_reminder_requested",
        "return_remind_before_days",
        "warranty_remind_before_days",
    },
    RecordType.SUBSCRIPTION: {
        "service_name",
        "billing_cycle",
        "next_renewal_date",
        "auto_renew",
        "reminder_requested",
        "remind_before_days",
    },
    RecordType.BILL: {
        "bill_name",
        "billing_period",
        "due_date",
        "reminder_requested",
        "remind_before_days",
    },
}

DATE_SOURCE_FIELDS = {
    "event_date": ("event_date_text",),
    "return_deadline_date": (
        "return_deadline_text",
        "deadline_date",
        "deadline_text",
    ),
    "warranty_deadline_date": ("warranty_deadline_text",),
    "next_renewal_date": (
        "next_renewal_text",
        "deadline_date",
        "deadline_text",
    ),
    "due_date": (
        "due_date_text",
        "deadline_date",
        "deadline_text",
    ),
}

MISSING_FIELD_MAP = {
    "title": "title",
    "amount": "amount",
    "purchase_date": "event_date",
    "return_or_warranty_deadline": "return_deadline_date",
    "return_deadline": "return_deadline_date",
    "warranty_deadline": "warranty_deadline_date",
    "next_renewal_date": "next_renewal_date",
    "due_date": "due_date",
}


@dataclass(frozen=True)
class CorrectionResult:
    candidate: ExtractedRecordCandidate
    field_errors: dict[str, list[str]]


def apply_candidate_corrections(
    base: ExtractedRecordCandidate,
    raw_corrections: Any,
    service: LifeVaultAgent,
    now: datetime,
) -> CorrectionResult:
    if not isinstance(raw_corrections, dict):
        return CorrectionResult(base, {"corrections": ["Corrections must be a JSON object."]})
    if not base.record_type:
        return CorrectionResult(base, {"record_type": ["Record type is missing."]})

    try:
        corrections = CandidateCorrections.model_validate(raw_corrections)
    except ValidationError as exc:
        return CorrectionResult(base, _validation_errors(exc))

    changed_fields = set(corrections.model_fields_set)
    allowed_fields = COMMON_FIELDS | TYPE_FIELDS[base.record_type]
    disallowed = sorted(changed_fields - allowed_fields)
    if disallowed:
        return CorrectionResult(
            base,
            {
                field: [f"{field} cannot be edited for {base.record_type.value} records."]
                for field in disallowed
            },
        )

    updates = corrections.model_dump(
        mode="python",
        include=changed_fields,
    )
    candidate_data = base.model_dump(mode="python")
    candidate_data.update(updates)
    for field in changed_fields:
        for source_field in DATE_SOURCE_FIELDS.get(field, ()):
            candidate_data[source_field] = None

    if base.record_type == RecordType.PURCHASE and changed_fields & {
        "return_reminder_requested",
        "warranty_reminder_requested",
    }:
        candidate_data["reminder_requested"] = bool(
            candidate_data.get("return_reminder_requested")
            or candidate_data.get("warranty_reminder_requested")
        )

    try:
        candidate = ExtractedRecordCandidate.model_validate(candidate_data)
    except ValidationError as exc:
        return CorrectionResult(base, _validation_errors(exc))

    candidate = service._canonicalize_candidate(candidate, now)
    field_errors = _candidate_errors(candidate, service, now)
    if field_errors:
        return CorrectionResult(base, field_errors)
    return CorrectionResult(candidate, {})


def _candidate_errors(
    candidate: ExtractedRecordCandidate,
    service: LifeVaultAgent,
    now: datetime,
) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    for missing in service._missing_fields(candidate, now):
        field = MISSING_FIELD_MAP.get(missing, missing)
        _add_error(errors, field, f"Required value is missing: {missing}.")

    event_date = candidate.event_date
    if candidate.record_type == RecordType.PURCHASE:
        return_deadline, warranty_deadline, _warnings = service._purchase_deadlines(
            candidate,
            event_date,
            now,
        )
        if event_date and return_deadline and return_deadline < event_date:
            _add_error(
                errors,
                "return_deadline_date",
                "Return deadline cannot be earlier than the purchase date.",
            )
        if event_date and warranty_deadline and warranty_deadline < event_date:
            _add_error(
                errors,
                "warranty_deadline_date",
                "Warranty deadline cannot be earlier than the purchase date.",
            )
    elif candidate.record_type == RecordType.SUBSCRIPTION:
        renewal, _source = service._subscription_renewal_date(candidate, now)
        if event_date and renewal and renewal < event_date:
            _add_error(
                errors,
                "next_renewal_date",
                "Next renewal date cannot be earlier than the event date.",
            )
    return errors


def _validation_errors(exc: ValidationError) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    for item in exc.errors(include_url=False):
        location = item.get("loc") or ("corrections",)
        field = str(location[0])
        _add_error(errors, field, str(item.get("msg", "Invalid value.")))
    return errors


def _add_error(errors: dict[str, list[str]], field: str, message: str) -> None:
    errors.setdefault(field, []).append(message)
