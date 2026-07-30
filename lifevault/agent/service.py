from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from lifevault.config import Settings
from lifevault.hooks.privacy_hooks import sanitize_input
from lifevault.models.llm_factory import Extractor
from lifevault.models.schemas import (
    DraftResult,
    ExtractedRecordCandidate,
    LifeRecord,
    LifeRecordCreate,
    RecordType,
    Reminder,
    ReminderCreate,
    ReminderType,
    SaveResult,
    UserPreference,
)
from lifevault.mcp_server.client import InProcessPersonalVaultMcpClient, PersonalVaultMcpClient
from lifevault.storage.repository import VaultRepository
from lifevault.tools.date_tools import (
    calculate_calendar_month_deadline,
    calculate_deadline,
    calculate_next_renewal_date,
    calculate_reminder_at,
    normalize_billing_cycle,
    now_in_timezone,
    parse_date_text,
    parse_subscription_renewal_date,
)
from lifevault.tools.idempotency import stable_key


class ConfirmationRequired(PermissionError):
    pass


class LifeVaultAgent:
    def __init__(
        self,
        settings: Settings,
        repository: VaultRepository | None = None,
        mcp_client: PersonalVaultMcpClient | None = None,
    ):
        self.settings = settings
        self.repository = repository or VaultRepository(settings.database_path)
        self.mcp_client = mcp_client or InProcessPersonalVaultMcpClient(settings, self.repository)
        self.extractor = Extractor(settings)

    def create_draft(self, raw_input: str, thread_id: str | None = None) -> DraftResult:
        now = now_in_timezone(self.settings.default_timezone)
        sanitized = sanitize_input(raw_input, self.settings.input_max_chars)
        candidate, warnings = self.extractor.extract_record(sanitized, now)

        draft = DraftResult(
            thread_id=thread_id or str(uuid4()),
            raw_input=raw_input,
            sanitized_input=sanitized,
            candidate=candidate,
            warnings=warnings,
        )

        if candidate.intent == "search_records":
            self.repository.save_checkpoint(draft.thread_id, draft.model_dump(mode="json"))
            return draft

        missing = self._missing_fields(candidate, now)
        draft.missing_fields = missing
        if missing:
            self.repository.save_checkpoint(draft.thread_id, draft.model_dump(mode="json"))
            return draft

        preference = self._get_preferences()
        record = self._build_record(candidate, sanitized, now, preference)
        draft.record = record
        if record.record_type == RecordType.PURCHASE:
            _return_deadline, _warranty_deadline, deadline_warnings = self._purchase_deadlines(
                candidate,
                record.event_date,
                now,
            )
            draft.warnings.extend(deadline_warnings)
        draft.duplicate_candidates = self.repository.find_duplicate(self.settings.default_user_id, record)
        reminders, reminder_warnings = self._build_reminders(candidate, record, preference, now)
        draft.reminders = reminders
        draft.reminder = reminders[0] if reminders else None
        draft.warnings.extend(reminder_warnings)
        self.repository.save_checkpoint(draft.thread_id, draft.model_dump(mode="json"))
        return draft

    def save_draft(
        self,
        draft: DraftResult,
        user_confirmed_record: bool,
        user_confirmed_reminder: bool,
    ) -> SaveResult:
        if not user_confirmed_record:
            raise ConfirmationRequired("Saving a record requires user confirmation.")
        if not draft.record:
            raise ValueError(f"Draft is not ready to save. Missing fields: {draft.missing_fields}")

        record_key = stable_key(
            "record",
            self.settings.default_user_id,
            draft.record.source_text_hash,
            draft.record.record_type.value,
            draft.record.title,
            draft.record.event_date,
            draft.record.deadline,
        )
        saved_record = self.repository.save_record(
            self.settings.default_user_id,
            draft.record,
            idempotency_key=record_key,
            actor="agent",
        )

        saved_reminders: list[Reminder] = []
        if draft.reminders:
            if not user_confirmed_reminder:
                raise ConfirmationRequired("Creating reminders requires user confirmation.")
            reminders = [
                reminder.model_copy(update={"record_id": saved_record.id})
                for reminder in draft.reminders
            ]
            batch_key = stable_key(
                "reminder-batch",
                self.settings.default_user_id,
                saved_record.id,
                *[
                    f"{reminder.reminder_type.value}:{reminder.scheduled_at.isoformat()}"
                    for reminder in reminders
                ],
            )
            create_result = self.mcp_client.create_reminders(
                [reminder.model_dump(mode="json") for reminder in reminders],
                idempotency_key=batch_key,
                user_confirmed=True,
            )
            if not create_result.get("ok"):
                raise RuntimeError(_mcp_error_message("create_reminders", create_result))
            saved_reminders = [
                Reminder.model_validate(reminder)
                for reminder in create_result.get("reminders", [])
            ]

        return SaveResult(
            record=saved_record,
            reminders=saved_reminders,
            reminder=saved_reminders[0] if saved_reminders else None,
            duplicate_candidates=draft.duplicate_candidates,
        )

    def search(self, text: str) -> tuple[list[LifeRecord], str]:
        now = now_in_timezone(self.settings.default_timezone)
        sanitized = sanitize_input(text, self.settings.input_max_chars)
        candidate, _warnings = self.extractor.extract_record(sanitized, now)
        query = candidate.search_query or candidate.title or sanitized
        record_types = None
        if candidate.intent == "search_records" and candidate.record_type:
            record_types = [candidate.record_type.value]
        result = self.mcp_client.search_records(
            query=query,
            record_types=record_types,
            limit=20,
        )
        if not result.get("ok"):
            raise RuntimeError(_mcp_error_message("search_records", result))
        records = [LifeRecord.model_validate(record) for record in result.get("records", [])]
        answer = self._format_search_answer(records)
        return records, answer

    def _missing_fields(self, candidate: ExtractedRecordCandidate, now: datetime) -> list[str]:
        missing: list[str] = []
        if candidate.intent not in {"create_record", "unknown"}:
            return missing
        if not candidate.record_type:
            missing.append("record_type")

        title = candidate.title or candidate.service_name or candidate.bill_name
        if not title:
            missing.append("title")

        if candidate.amount is None:
            missing.append("amount")

        if candidate.record_type == RecordType.PURCHASE:
            event_date = candidate.event_date or parse_date_text(
                candidate.event_date_text,
                self.settings.default_timezone,
                now,
            )
            if not event_date:
                missing.append("purchase_date")
            return_deadline, warranty_deadline, _warnings = self._purchase_deadlines(
                candidate,
                event_date,
                now,
            )
            if not return_deadline and not warranty_deadline:
                missing.append("return_or_warranty_deadline")
            if candidate.return_reminder_requested and not return_deadline:
                missing.append("return_deadline")
            if candidate.warranty_reminder_requested and not warranty_deadline:
                missing.append("warranty_deadline")
        elif candidate.record_type == RecordType.SUBSCRIPTION:
            renewal, _source = self._subscription_renewal_date(candidate, now)
            if not renewal:
                missing.append("next_renewal_date")
        elif candidate.record_type == RecordType.BILL:
            due = candidate.deadline_date or parse_date_text(
                candidate.due_date_text or candidate.deadline_text,
                self.settings.default_timezone,
                now,
            )
            if not due:
                missing.append("due_date")
        return missing

    def _build_record(
        self,
        candidate: ExtractedRecordCandidate,
        sanitized_input: str,
        now: datetime,
        preference: UserPreference,
    ) -> LifeRecordCreate:
        assert candidate.record_type is not None
        title = candidate.title or candidate.service_name or candidate.bill_name
        assert title is not None
        event_date: date | None = candidate.event_date or parse_date_text(
            candidate.event_date_text,
            self.settings.default_timezone,
            now,
        )
        deadline: date | None = candidate.deadline_date
        details: dict[str, Any] = {}

        if candidate.record_type == RecordType.PURCHASE:
            return_deadline, warranty_deadline, _warnings = self._purchase_deadlines(
                candidate,
                event_date,
                now,
            )
            deadline = return_deadline or warranty_deadline
            details = {
                "merchant": candidate.merchant,
                "order_number": candidate.order_number,
                "return_days": candidate.return_days,
                "warranty_months": candidate.warranty_months,
                "return_deadline": return_deadline.isoformat() if return_deadline else None,
                "warranty_deadline": warranty_deadline.isoformat() if warranty_deadline else None,
            }
        elif candidate.record_type == RecordType.SUBSCRIPTION:
            deadline, renewal_source = self._subscription_renewal_date(candidate, now)
            cycle = normalize_billing_cycle(candidate.billing_cycle)
            remind_before_days = candidate.remind_before_days
            if remind_before_days is None:
                remind_before_days = preference.default_advance_days
            details = {
                "service_name": candidate.service_name or title,
                "billing_cycle": cycle,
                "auto_renew": candidate.auto_renew,
                "renewal_anchor_day": self._renewal_anchor_day(deadline, cycle),
                "next_renewal_source": renewal_source,
                "remind_before_days": remind_before_days,
                "reminder_time": candidate.reminder_time or preference.default_time,
            }
        elif candidate.record_type == RecordType.BILL:
            details = {
                "bill_name": candidate.bill_name or title,
                "billing_period": candidate.billing_period,
            }
            if not deadline:
                deadline = parse_date_text(
                    candidate.due_date_text or candidate.deadline_text,
                    self.settings.default_timezone,
                    now,
                )

        details = {key: value for key, value in details.items() if value is not None}
        source_hash = hashlib.sha256(sanitized_input.encode("utf-8")).hexdigest()
        return LifeRecordCreate(
            record_type=candidate.record_type,
            title=title,
            amount=candidate.amount,
            currency=candidate.currency,
            event_date=event_date,
            deadline=deadline,
            details=details,
            notes=candidate.notes,
            source_text_hash=source_hash,
            source_text_preview=sanitized_input[:240],
        )

    def _build_reminders(
        self,
        candidate: ExtractedRecordCandidate,
        record: LifeRecordCreate,
        preference: UserPreference,
        now: datetime,
    ) -> tuple[list[ReminderCreate], list[str]]:
        reminder_requested = (
            candidate.reminder_requested
            or candidate.return_reminder_requested
            or candidate.warranty_reminder_requested
        )
        if not reminder_requested:
            return [], []

        reminder_time = candidate.reminder_time or preference.default_time
        plans: list[tuple[ReminderType, date, int]] = []

        if record.record_type == RecordType.PURCHASE:
            has_specific_target = (
                candidate.return_reminder_requested or candidate.warranty_reminder_requested
            )
            target_specs = [
                (
                    ReminderType.RETURN_DEADLINE,
                    record.details.get("return_deadline"),
                    candidate.return_reminder_requested,
                    candidate.return_remind_before_days,
                ),
                (
                    ReminderType.WARRANTY_DEADLINE,
                    record.details.get("warranty_deadline"),
                    candidate.warranty_reminder_requested,
                    candidate.warranty_remind_before_days,
                ),
            ]
            for reminder_type, deadline_text, specifically_requested, target_before_days in target_specs:
                if not deadline_text or (has_specific_target and not specifically_requested):
                    continue
                before_days = target_before_days
                if before_days is None:
                    before_days = candidate.remind_before_days
                if before_days is None:
                    before_days = preference.default_advance_days
                plans.append((reminder_type, date.fromisoformat(str(deadline_text)), before_days))
        elif record.deadline:
            before_days = candidate.remind_before_days
            if before_days is None:
                before_days = preference.default_advance_days
            reminder_type = (
                ReminderType.RENEWAL
                if record.record_type == RecordType.SUBSCRIPTION
                else ReminderType.BILL_DUE
            )
            plans.append((reminder_type, record.deadline, before_days))

        reminders: list[ReminderCreate] = []
        warnings: list[str] = []
        for reminder_type, deadline, before_days in plans:
            if deadline < now.date():
                warnings.append(f"{reminder_type.value} deadline has already passed; reminder was not created.")
                continue
            scheduled_at = calculate_reminder_at(
                deadline,
                before_days,
                reminder_time,
                self.settings.default_timezone,
            )
            if scheduled_at <= now:
                scheduled_at = now.replace(microsecond=0)
                warnings.append(
                    f"{reminder_type.value} advance time has passed; reminder will be scheduled immediately."
                )
            reminders.append(
                ReminderCreate(
                    record_id="__pending__",
                    scheduled_at=scheduled_at,
                    reminder_type=reminder_type,
                    message=self._build_reminder_message(record, reminder_type, before_days),
                )
            )
        return reminders, warnings

    def _build_reminder(
        self,
        candidate: ExtractedRecordCandidate,
        record: LifeRecordCreate,
        preference: UserPreference,
        now: datetime | None = None,
    ) -> ReminderCreate | None:
        reminders, _warnings = self._build_reminders(
            candidate,
            record,
            preference,
            now or now_in_timezone(self.settings.default_timezone),
        )
        return reminders[0] if reminders else None

    def _get_preferences(self) -> UserPreference:
        result = self.mcp_client.get_preferences()
        if not result.get("ok"):
            raise RuntimeError(_mcp_error_message("get_preferences", result))
        try:
            return UserPreference.model_validate(result["preference"])
        except (KeyError, TypeError, ValidationError) as exc:
            raise RuntimeError("MCP get_preferences returned an invalid response.") from exc

    def _build_reminder_message(
        self,
        record: LifeRecordCreate,
        reminder_type: ReminderType,
        before_days: int,
    ) -> str:
        if reminder_type == ReminderType.RETURN_DEADLINE:
            return f"你的「{record.title}」预计还有 {before_days} 天结束退货期。"
        if reminder_type == ReminderType.WARRANTY_DEADLINE:
            return f"你的「{record.title}」预计还有 {before_days} 天结束保修期。"
        if reminder_type == ReminderType.RENEWAL:
            return f"你的「{record.title}」预计还有 {before_days} 天续费。"
        return f"你的「{record.title}」预计还有 {before_days} 天到缴费截止日。"

    def _purchase_deadlines(
        self,
        candidate: ExtractedRecordCandidate,
        event_date: date | None,
        now: datetime,
    ) -> tuple[date | None, date | None, list[str]]:
        return_deadline = candidate.deadline_date
        return_text = candidate.return_deadline_text or candidate.deadline_text
        if return_deadline is None and return_text:
            return_deadline = parse_date_text(return_text, self.settings.default_timezone, now)
        if return_deadline is None and event_date and candidate.return_days is not None:
            return_deadline = calculate_deadline(event_date, candidate.return_days)

        explicit_warranty_deadline = parse_date_text(
            candidate.warranty_deadline_text,
            self.settings.default_timezone,
            now,
        )
        calculated_warranty_deadline = None
        if event_date and candidate.warranty_months is not None:
            calculated_warranty_deadline = calculate_calendar_month_deadline(
                event_date,
                candidate.warranty_months,
            )
        warnings: list[str] = []
        if (
            explicit_warranty_deadline
            and calculated_warranty_deadline
            and explicit_warranty_deadline != calculated_warranty_deadline
        ):
            warnings.append(
                "Explicit warranty deadline differs from the duration calculation; "
                "the explicit deadline was used."
            )
        warranty_deadline = explicit_warranty_deadline or calculated_warranty_deadline
        return return_deadline, warranty_deadline, warnings

    def _format_search_answer(self, records: list[LifeRecord]) -> str:
        if not records:
            return "没有查到匹配的真实记录。"
        lines = ["查到这些真实记录："]
        for record in records[:10]:
            deadline = record.deadline.isoformat() if record.deadline else "无截止日"
            amount = f"{record.amount:g} {record.currency}" if record.amount is not None else "金额未知"
            lines.append(f"- {record.title}：{amount}，状态 {record.status.value}，截止 {deadline}")
        return "\n".join(lines)

    def _subscription_renewal_date(
        self,
        candidate: ExtractedRecordCandidate,
        now: datetime,
    ) -> tuple[date | None, str | None]:
        if candidate.deadline_date:
            return candidate.deadline_date, "deadline_date"

        cycle = normalize_billing_cycle(candidate.billing_cycle)
        if candidate.next_renewal_text:
            renewal = parse_subscription_renewal_date(
                candidate.next_renewal_text,
                cycle,
                self.settings.default_timezone,
                now,
            )
            if renewal:
                return renewal, "next_renewal_text"

        if candidate.deadline_text:
            renewal = parse_subscription_renewal_date(
                candidate.deadline_text,
                cycle,
                self.settings.default_timezone,
                now,
            )
            if renewal:
                return renewal, "deadline_text"

        event_date = candidate.event_date or parse_date_text(
            candidate.event_date_text,
            self.settings.default_timezone,
            now,
        )
        if event_date and cycle:
            renewal = calculate_next_renewal_date(event_date, cycle, today=now.date())
            if renewal:
                source = "event_date_text+billing_cycle" if candidate.event_date_text else "event_date+billing_cycle"
                return renewal, source

        return None, None

    def _renewal_anchor_day(self, renewal: date | None, billing_cycle: str | None) -> int | str | None:
        if not renewal or not billing_cycle:
            return None
        if billing_cycle == "weekly":
            return renewal.weekday()
        if billing_cycle == "yearly":
            return f"{renewal.month:02d}-{renewal.day:02d}"
        return renewal.day


def _mcp_error_message(tool_name: str, result: dict[str, Any]) -> str:
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        code = error.get("code", "unknown_error")
        message = error.get("message", "")
        return f"MCP {tool_name} failed: {code}: {message}"
    return f"MCP {tool_name} failed."
