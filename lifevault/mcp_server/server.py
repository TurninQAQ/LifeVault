from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from lifevault.config import Settings, get_settings
from lifevault.models.schemas import (
    LifeRecordCreate,
    RecordStatus,
    RecordType,
    ReminderCreate,
    ReminderStatus,
    ReminderType,
)
from lifevault.storage.repository import VaultRepository
from lifevault.tools.date_tools import now_in_timezone
from lifevault.tools.idempotency import stable_key


def create_server(
    settings: Settings | None = None,
    repository: VaultRepository | None = None,
) -> FastMCP:
    active_settings = settings or get_settings()
    repo = repository or VaultRepository(active_settings.database_path)
    user_id = active_settings.default_user_id
    mcp = FastMCP(
        "lifevault-personal-vault",
        log_level="ERROR",
        instructions=(
            "Local-first LifeVault data boundary. Tools operate on the configured local user only. "
            "Write operations validate arguments, use idempotency keys, and return JSON objects."
        ),
    )

    def audit_mcp_failure(
        action: str,
        result: str,
        error_code: str,
        target_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        repo.record_audit_event(
            user_id=user_id,
            actor="mcp",
            action=action,
            target_id=target_id,
            result=result,
            params={**(params or {}), "error_code": error_code},
        )

    @mcp.tool(description="Save a life record after user confirmation.")
    def save_record(
        record: dict[str, Any],
        idempotency_key: str,
        user_confirmed: bool,
        source_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        raw_record_type = record.get("record_type")
        record_type = raw_record_type if raw_record_type in {item.value for item in RecordType} else None
        audit_params = {"record_type": record_type}
        if not user_confirmed:
            audit_mcp_failure("save_record", "rejected", "confirmation_required", params=audit_params)
            return _fail("confirmation_required", "Saving a record requires user_confirmed=true.")
        if not idempotency_key:
            audit_mcp_failure("save_record", "rejected", "missing_idempotency_key", params=audit_params)
            return _fail("missing_idempotency_key", "idempotency_key is required.")
        try:
            parsed = LifeRecordCreate.model_validate(record)
        except ValidationError as exc:
            audit_mcp_failure("save_record", "rejected", "invalid_record", params=audit_params)
            return _fail("save_record_failed", str(exc))
        audit_params = {"record_type": parsed.record_type.value}
        try:
            saved = repo.save_record(user_id, parsed, idempotency_key=idempotency_key, actor="mcp")
            return _ok(record=_model(saved), source_ids=source_ids or [])
        except (ValueError, PermissionError) as exc:
            audit_mcp_failure("save_record", "failed", "save_record_failed", params=audit_params)
            return _fail("save_record_failed", str(exc))
        except Exception:
            audit_mcp_failure("save_record", "failed", "internal_error", params=audit_params)
            return _fail("save_record_failed", "Tool execution failed.")

    @mcp.tool(description="Search real saved records from the local vault.")
    def search_records(
        query: str | None = None,
        record_types: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        try:
            parsed_types = [RecordType(item) for item in record_types] if record_types else None
            records = repo.search_records(
                user_id,
                query=query,
                record_types=parsed_types,
                date_from=_parse_date(date_from),
                date_to=_parse_date(date_to),
                limit=limit,
            )
            return _ok(records=[_model(record) for record in records])
        except (ValidationError, ValueError) as exc:
            return _fail("search_records_failed", str(exc))

    @mcp.tool(description="List subscription records whose renewal deadline is coming soon.")
    def list_upcoming_subscriptions(
        days: int = 30,
        include_auto_renew: bool = True,
        limit: int = 20,
    ) -> dict[str, Any]:
        try:
            date_from = now_in_timezone(active_settings.default_timezone).date()
            records = repo.list_upcoming_subscriptions(
                user_id,
                date_from=date_from,
                days=days,
                include_auto_renew=include_auto_renew,
                limit=limit,
            )
            return _ok(
                records=[_model(record) for record in records],
                date_from=date_from.isoformat(),
                date_to=(date_from + timedelta(days=days)).isoformat(),
            )
        except (ValidationError, ValueError) as exc:
            return _fail("list_upcoming_subscriptions_failed", str(exc))

    @mcp.tool(description="Get one saved life record by id.")
    def get_record(record_id: str) -> dict[str, Any]:
        try:
            record = repo.get_record(user_id, record_id)
            if not record:
                return _fail("record_not_found", "Record not found.")
            return _ok(record=_model(record))
        except ValueError as exc:
            return _fail("get_record_failed", str(exc))

    @mcp.tool(description="Find possible duplicate records.")
    def find_duplicate(
        record: dict[str, Any] | None = None,
        record_type: str = "purchase",
        title: str | None = None,
        merchant: str | None = None,
        order_number: str | None = None,
        amount: float | None = None,
        event_date: str | None = None,
        document_hash: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        try:
            if record is not None:
                parsed = LifeRecordCreate.model_validate(record)
            else:
                parsed = LifeRecordCreate(
                    record_type=RecordType(record_type),
                    title=title or "candidate",
                    amount=amount,
                    event_date=_parse_date(event_date),
                    details={
                        key: value
                        for key, value in {
                            "merchant": merchant,
                            "order_number": order_number,
                            "document_hash": document_hash,
                        }.items()
                        if value is not None
                    },
                )
            duplicates = repo.find_duplicate(user_id, parsed, limit=limit)
            return _ok(duplicate_candidates=[_model(item) for item in duplicates])
        except (ValidationError, ValueError) as exc:
            return _fail("find_duplicate_failed", str(exc))

    @mcp.tool(description="Update a record status with optimistic locking.")
    def update_record_status(record_id: str, new_status: str, expected_version: int) -> dict[str, Any]:
        try:
            parsed_status = RecordStatus(new_status)
        except ValueError as exc:
            audit_mcp_failure(
                "update_record_status",
                "rejected",
                "invalid_status",
                target_id=record_id,
            )
            return _fail("update_record_status_failed", str(exc))
        try:
            updated = repo.update_record_status(
                user_id,
                record_id,
                parsed_status,
                expected_version=expected_version,
                actor="mcp",
            )
            return _ok(record=_model(updated))
        except ValueError as exc:
            audit_mcp_failure(
                "update_record_status",
                "failed",
                "record_conflict_or_not_found",
                target_id=record_id,
                params={"new_status": new_status},
            )
            return _fail("update_record_status_failed", str(exc))
        except Exception:
            audit_mcp_failure(
                "update_record_status",
                "failed",
                "internal_error",
                target_id=record_id,
                params={"new_status": new_status},
            )
            return _fail("update_record_status_failed", "Tool execution failed.")

    @mcp.tool(description="Create a reminder task after user confirmation.")
    def create_reminder(
        record_id: str,
        scheduled_at: str,
        reminder_type: str,
        idempotency_key: str,
        user_confirmed: bool,
        message: str | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        audit_params = {
            "reminder_type": reminder_type if reminder_type in {item.value for item in ReminderType} else None
        }
        if not user_confirmed:
            audit_mcp_failure(
                "create_reminder",
                "rejected",
                "confirmation_required",
                target_id=record_id,
                params=audit_params,
            )
            return _fail("confirmation_required", "Creating a reminder requires user_confirmed=true.")
        if not idempotency_key:
            audit_mcp_failure(
                "create_reminder",
                "rejected",
                "missing_idempotency_key",
                target_id=record_id,
                params=audit_params,
            )
            return _fail("missing_idempotency_key", "idempotency_key is required.")
        try:
            reminder = ReminderCreate(
                record_id=record_id,
                scheduled_at=_parse_datetime_required(scheduled_at),
                reminder_type=ReminderType(reminder_type),
                message=message or f"LifeVault reminder for record {record_id}",
                parent_id=parent_id,
            )
        except (ValidationError, ValueError) as exc:
            audit_mcp_failure(
                "create_reminder",
                "rejected",
                "invalid_reminder",
                target_id=record_id,
                params=audit_params,
            )
            return _fail("create_reminder_failed", str(exc))
        audit_params = {
            "reminder_type": reminder.reminder_type.value,
            "scheduled_at": reminder.scheduled_at.isoformat(),
        }
        try:
            created = repo.create_reminder(user_id, reminder, idempotency_key=idempotency_key, actor="mcp")
            return _ok(reminder=_model(created))
        except ValueError as exc:
            audit_mcp_failure(
                "create_reminder",
                "failed",
                "create_reminder_failed",
                target_id=record_id,
                params=audit_params,
            )
            return _fail("create_reminder_failed", str(exc))
        except Exception:
            audit_mcp_failure(
                "create_reminder",
                "failed",
                "internal_error",
                target_id=record_id,
                params=audit_params,
            )
            return _fail("create_reminder_failed", "Tool execution failed.")

    @mcp.tool(description="List reminders by status.")
    def list_reminders(status: str | None = None, limit: int = 100) -> dict[str, Any]:
        try:
            parsed_status = ReminderStatus(status) if status else None
            reminders = repo.list_reminders(user_id, status=parsed_status, limit=limit)
            return _ok(reminders=[_model(reminder) for reminder in reminders])
        except ValueError as exc:
            return _fail("list_reminders_failed", str(exc))

    @mcp.tool(description="Snooze a reminder by marking the old one snoozed and creating a new pending task.")
    def snooze_reminder(reminder_id: str, new_scheduled_at: str) -> dict[str, Any]:
        try:
            scheduled_at = _parse_datetime_required(new_scheduled_at)
        except ValueError as exc:
            audit_mcp_failure(
                "snooze_reminder",
                "rejected",
                "invalid_scheduled_at",
                target_id=reminder_id,
            )
            return _fail("snooze_reminder_failed", str(exc))
        try:
            idempotency_key = stable_key("snooze", user_id, reminder_id, scheduled_at.isoformat())
            reminder = repo.snooze_reminder(
                user_id,
                reminder_id,
                scheduled_at,
                idempotency_key=idempotency_key,
                actor="mcp",
            )
            parent = repo.get_reminder(user_id, reminder_id)
            return _ok(reminder=_model(reminder), parent_reminder=_model(parent) if parent else None)
        except ValueError as exc:
            audit_mcp_failure(
                "snooze_reminder",
                "failed",
                "snooze_reminder_failed",
                target_id=reminder_id,
                params={"new_scheduled_at": scheduled_at.isoformat()},
            )
            return _fail("snooze_reminder_failed", str(exc))
        except Exception:
            audit_mcp_failure(
                "snooze_reminder",
                "failed",
                "internal_error",
                target_id=reminder_id,
                params={"new_scheduled_at": scheduled_at.isoformat()},
            )
            return _fail("snooze_reminder_failed", "Tool execution failed.")

    @mcp.tool(description="Cancel a reminder. Requires user_confirmed=true.")
    def cancel_reminder(reminder_id: str, user_confirmed: bool) -> dict[str, Any]:
        if not user_confirmed:
            audit_mcp_failure(
                "cancel_reminder",
                "rejected",
                "confirmation_required",
                target_id=reminder_id,
            )
            return _fail("confirmation_required", "Reminder cancellation requires user confirmation.")
        try:
            cancelled = repo.cancel_reminder(
                user_id,
                reminder_id,
                user_confirmed=user_confirmed,
                actor="mcp",
            )
            return _ok(reminder=_model(cancelled))
        except PermissionError as exc:
            audit_mcp_failure(
                "cancel_reminder",
                "rejected",
                "confirmation_required",
                target_id=reminder_id,
            )
            return _fail("confirmation_required", str(exc))
        except ValueError as exc:
            audit_mcp_failure(
                "cancel_reminder",
                "failed",
                "cancel_reminder_failed",
                target_id=reminder_id,
            )
            return _fail("cancel_reminder_failed", str(exc))
        except Exception:
            audit_mcp_failure(
                "cancel_reminder",
                "failed",
                "internal_error",
                target_id=reminder_id,
            )
            return _fail("cancel_reminder_failed", "Tool execution failed.")

    @mcp.tool(description="List immutable audit events for the configured local user.")
    def list_audit_logs(
        actor: str | None = None,
        action: str | None = None,
        result: str | None = None,
        before_id: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        try:
            logs = repo.list_audit_logs(
                user_id,
                actor=actor,
                action=action,
                result=result,
                before_id=before_id,
                limit=limit,
            )
            return _ok(
                audit_logs=[_model(log) for log in logs],
                next_before_id=logs[-1].id if len(logs) == limit else None,
            )
        except ValueError as exc:
            return _fail("list_audit_logs_failed", str(exc))

    return mcp


def main() -> None:
    create_server().run("stdio")


def _ok(**data: Any) -> dict[str, Any]:
    return {"ok": True, **data}


def _fail(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _model(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json")


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _parse_datetime_required(value: str) -> datetime:
    if not value:
        raise ValueError("datetime value is required.")
    return datetime.fromisoformat(value)


if __name__ == "__main__":
    main()
