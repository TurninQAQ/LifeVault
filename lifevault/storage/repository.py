from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from lifevault.hooks.privacy_hooks import sanitize_audit_params, sanitize_audit_target_id
from lifevault.models.schemas import (
    AuditLog,
    DuplicateCandidate,
    LifeRecord,
    LifeRecordCreate,
    RecordLifecyclePreview,
    RecordLifecycleResult,
    RecordStatus,
    RecordStatusUpdatePreview,
    RecordStatusUpdateResult,
    RecordType,
    RecordUpdatePatch,
    RecordUpdatePreview,
    RecordUpdateResult,
    Reminder,
    ReminderBatchCreate,
    ReminderCreate,
    ReminderStatus,
    ReminderType,
    UserPreference,
    UserPreferencePatch,
    UserPreferenceUpdateResult,
)
from lifevault.records.update_planner import (
    RecordUpdateError,
    plan_record_archive,
    plan_record_restore,
    plan_record_update,
    plan_record_status_update,
    update_needs_duplicate_confirmation,
)
from lifevault.storage.database import connect, init_db
from lifevault.tools.idempotency import stable_key


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_text(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _date_to_text(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _is_clock_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M") == value
    except ValueError:
        return False


class VaultRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        init_db(database_path)

    def get_preferences(self, user_id: str) -> UserPreference:
        with connect(self.database_path) as conn:
            return self._get_preferences_conn(conn, user_id)

    def update_preferences(
        self,
        user_id: str,
        patch: UserPreferencePatch,
        actor: str = "mcp",
    ) -> UserPreferenceUpdateResult:
        with connect(self.database_path) as conn:
            existing_row = conn.execute(
                "SELECT * FROM user_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            current = self._preference_from_row(existing_row, user_id)
            updated_data = current.model_dump()
            for field in patch.model_fields_set:
                updated_data[field] = getattr(patch, field)
            updated = UserPreference.model_validate(updated_data)
            changed_fields = sorted(
                field
                for field in patch.model_fields_set
                if getattr(current, field) != getattr(updated, field)
            )
            if not changed_fields:
                return UserPreferenceUpdateResult(
                    preference=current,
                    changed=False,
                )

            if existing_row:
                assignments = ", ".join(f"{field} = ?" for field in changed_fields)
                values = [getattr(updated, field) for field in changed_fields]
                conn.execute(
                    f"UPDATE user_preferences SET {assignments} WHERE user_id = ?",
                    [*values, user_id],
                )
            else:
                conn.execute(
                    """
                    INSERT INTO user_preferences(
                        user_id, default_time, quiet_hours_start, quiet_hours_end, default_advance_days
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        updated.user_id,
                        updated.default_time,
                        updated.quiet_hours_start,
                        updated.quiet_hours_end,
                        updated.default_advance_days,
                    ),
                )
            self._audit_conn(
                conn,
                user_id=user_id,
                actor=actor,
                action="update_preferences",
                target_id=None,
                result="ok",
                params={"changed_fields": changed_fields},
            )
            return UserPreferenceUpdateResult(
                preference=updated,
                changed=True,
                changed_fields=changed_fields,
            )

    def _get_preferences_conn(self, conn: Any, user_id: str) -> UserPreference:
        row = conn.execute(
            "SELECT * FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return self._preference_from_row(row, user_id)

    def _preference_from_row(self, row: Any, user_id: str) -> UserPreference:
        if not row:
            return UserPreference(user_id=user_id)
        default_time = row["default_time"] if _is_clock_text(row["default_time"]) else "09:00"
        quiet_hours_start = row["quiet_hours_start"]
        quiet_hours_end = row["quiet_hours_end"]
        if not (_is_clock_text(quiet_hours_start) and _is_clock_text(quiet_hours_end)):
            quiet_hours_start = None
            quiet_hours_end = None
        default_advance_days = row["default_advance_days"]
        if not isinstance(default_advance_days, int) or not 0 <= default_advance_days <= 30:
            default_advance_days = 2
        return UserPreference(
            user_id=row["user_id"],
            default_time=default_time,
            quiet_hours_start=quiet_hours_start,
            quiet_hours_end=quiet_hours_end,
            default_advance_days=default_advance_days,
        )

    def save_record(
        self,
        user_id: str,
        record: LifeRecordCreate,
        idempotency_key: str,
        actor: str = "agent",
    ) -> LifeRecord:
        with connect(self.database_path) as conn:
            existing = conn.execute(
                "SELECT * FROM life_records WHERE user_id = ? AND idempotency_key = ?",
                (user_id, idempotency_key),
            ).fetchone()
            if existing:
                return self._row_to_record(existing)

            now = _utc_now().isoformat()
            record_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO life_records(
                    id, user_id, type, title, amount, currency, event_date, deadline, status,
                    version, details_json, notes, source_text_hash, source_text_preview,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    user_id,
                    record.record_type.value,
                    record.title,
                    record.amount,
                    record.currency,
                    _date_to_text(record.event_date),
                    _date_to_text(record.deadline),
                    record.status.value,
                    1,
                    json.dumps(record.details, ensure_ascii=False, sort_keys=True),
                    record.notes,
                    record.source_text_hash,
                    record.source_text_preview,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            self._audit_conn(
                conn,
                user_id=user_id,
                actor=actor,
                action="save_record",
                target_id=record_id,
                result="ok",
                params={"record_type": record.record_type.value},
            )
            row = conn.execute("SELECT * FROM life_records WHERE id = ?", (record_id,)).fetchone()
            return self._row_to_record(row)

    def get_record(self, user_id: str, record_id: str) -> LifeRecord | None:
        with connect(self.database_path) as conn:
            row = conn.execute(
                "SELECT * FROM life_records WHERE user_id = ? AND id = ?",
                (user_id, record_id),
            ).fetchone()
            return self._row_to_record(row) if row else None

    def search_records(
        self,
        user_id: str,
        query: str | None = None,
        record_types: list[RecordType] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 50,
        archive_scope: str = "active",
    ) -> list[LifeRecord]:
        if archive_scope not in {"active", "archived", "all"}:
            raise ValueError("archive_scope must be active, archived, or all.")
        sql = "SELECT * FROM life_records WHERE user_id = ?"
        params: list[Any] = [user_id]

        if archive_scope == "active":
            sql += " AND archived_at IS NULL"
        elif archive_scope == "archived":
            sql += " AND archived_at IS NOT NULL"

        if record_types:
            placeholders = ",".join("?" for _ in record_types)
            sql += f" AND type IN ({placeholders})"
            params.extend(record_type.value for record_type in record_types)
        if date_from or date_to:
            date_expressions = [
                "event_date",
                "deadline",
                "json_extract(details_json, '$.return_deadline')",
                "json_extract(details_json, '$.warranty_deadline')",
            ]
            date_conditions: list[str] = []
            for expression in date_expressions:
                bounds: list[str] = [f"{expression} IS NOT NULL"]
                if date_from:
                    bounds.append(f"{expression} >= ?")
                    params.append(date_from.isoformat())
                if date_to:
                    bounds.append(f"{expression} <= ?")
                    params.append(date_to.isoformat())
                date_conditions.append(f"({' AND '.join(bounds)})")
            sql += f" AND ({' OR '.join(date_conditions)})"
        if query:
            like = f"%{query}%"
            sql += " AND (title LIKE ? OR details_json LIKE ? OR source_text_preview LIKE ?)"
            params.extend([like, like, like])

        if archive_scope == "archived":
            sql += " ORDER BY archived_at DESC, updated_at DESC LIMIT ?"
        else:
            sql += " ORDER BY COALESCE(deadline, event_date, created_at) ASC LIMIT ?"
        params.append(limit)

        with connect(self.database_path) as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_record(row) for row in rows]

    def list_upcoming_subscriptions(
        self,
        user_id: str,
        date_from: date,
        days: int = 30,
        include_auto_renew: bool = True,
        limit: int = 20,
    ) -> list[LifeRecord]:
        if days < 0:
            raise ValueError("days must be non-negative.")
        if limit <= 0:
            raise ValueError("limit must be positive.")

        date_to = date_from + timedelta(days=days)
        with connect(self.database_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM life_records
                WHERE user_id = ?
                  AND type = ?
                  AND status != ?
                  AND archived_at IS NULL
                  AND deadline IS NOT NULL
                  AND deadline >= ?
                  AND deadline <= ?
                ORDER BY deadline ASC, created_at ASC
                """,
                (
                    user_id,
                    RecordType.SUBSCRIPTION.value,
                    RecordStatus.CANCELLED.value,
                    date_from.isoformat(),
                    date_to.isoformat(),
                ),
            ).fetchall()

        records = [self._row_to_record(row) for row in rows]
        if not include_auto_renew:
            records = [record for record in records if record.details.get("auto_renew") is not True]
        return records[:limit]

    def list_subscription_rollover_candidates(
        self,
        user_id: str,
        before: date,
        limit: int = 100,
    ) -> list[LifeRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive.")

        with connect(self.database_path) as conn:
            rows = conn.execute(
                """
                SELECT records.*
                FROM life_records AS records
                WHERE records.user_id = ?
                  AND records.type = ?
                  AND records.status = ?
                  AND records.archived_at IS NULL
                  AND records.deadline IS NOT NULL
                  AND records.deadline < ?
                  AND (
                      SELECT reminders.status
                      FROM reminders
                      WHERE reminders.record_id = records.id
                        AND reminders.reminder_type = ?
                      ORDER BY reminders.updated_at DESC, reminders.created_at DESC
                      LIMIT 1
                ) IN (?, ?)
                ORDER BY records.deadline ASC, records.created_at ASC
                """,
                (
                    user_id,
                    RecordType.SUBSCRIPTION.value,
                    RecordStatus.ACTIVE.value,
                    before.isoformat(),
                    ReminderType.RENEWAL.value,
                    ReminderStatus.SENT.value,
                    ReminderStatus.FAILED.value,
                ),
            ).fetchall()

        records = [self._row_to_record(row) for row in rows]
        return [
            record
            for record in records
            if record.details.get("auto_renew") is True
            and record.details.get("billing_cycle") in {"monthly", "yearly", "weekly"}
        ][:limit]

    def rollover_subscription(
        self,
        user_id: str,
        record_id: str,
        expected_version: int,
        next_deadline: date,
        reminder: ReminderCreate,
        idempotency_key: str,
    ) -> tuple[LifeRecord, Reminder] | None:
        if reminder.record_id != record_id:
            raise ValueError("Reminder record does not match subscription.")
        if reminder.reminder_type != ReminderType.RENEWAL:
            raise ValueError("Subscription rollover requires a renewal reminder.")

        with connect(self.database_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM life_records
                WHERE user_id = ? AND id = ? AND version = ?
                """,
                (user_id, record_id, expected_version),
            ).fetchone()
            if not row:
                return None

            record = self._row_to_record(row)
            billing_cycle = record.details.get("billing_cycle")
            if (
                record.record_type != RecordType.SUBSCRIPTION
                or record.status != RecordStatus.ACTIVE
                or record.archived_at is not None
                or record.details.get("auto_renew") is not True
                or billing_cycle not in {"monthly", "yearly", "weekly"}
                or record.deadline is None
                or next_deadline <= record.deadline
            ):
                raise ValueError("Record is not eligible for subscription rollover.")

            details = dict(record.details)
            details["previous_renewal_date"] = record.deadline.isoformat()
            details["next_renewal_source"] = "worker_rollover"
            now = _utc_now().isoformat()
            updated = conn.execute(
                """
                UPDATE life_records
                SET deadline = ?, details_json = ?, version = version + 1, updated_at = ?
                WHERE user_id = ? AND id = ? AND version = ?
                """,
                (
                    next_deadline.isoformat(),
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                    now,
                    user_id,
                    record_id,
                    expected_version,
                ),
            )
            if updated.rowcount != 1:
                return None

            reminder_row = conn.execute(
                """
                SELECT * FROM reminders
                WHERE user_id = ?
                  AND record_id = ?
                  AND reminder_type = ?
                  AND (
                      idempotency_key = ?
                      OR (scheduled_at = ? AND status IN (?, ?))
                  )
                LIMIT 1
                """,
                (
                    user_id,
                    record_id,
                    ReminderType.RENEWAL.value,
                    idempotency_key,
                    _dt_to_text(reminder.scheduled_at),
                    ReminderStatus.PENDING.value,
                    ReminderStatus.SENDING.value,
                ),
            ).fetchone()
            if not reminder_row:
                reminder_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO reminders(
                        id, record_id, user_id, scheduled_at, reminder_type, message, status,
                        parent_id, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reminder_id,
                        record_id,
                        user_id,
                        _dt_to_text(reminder.scheduled_at),
                        ReminderType.RENEWAL.value,
                        reminder.message,
                        ReminderStatus.PENDING.value,
                        reminder.parent_id,
                        idempotency_key,
                        now,
                        now,
                    ),
                )
                reminder_row = conn.execute(
                    "SELECT * FROM reminders WHERE id = ?",
                    (reminder_id,),
                ).fetchone()

            self._audit_conn(
                conn,
                user_id=user_id,
                actor="worker",
                action="rollover_subscription",
                target_id=record_id,
                result="ok",
                params={
                    "billing_cycle": billing_cycle,
                    "reminder_type": ReminderType.RENEWAL.value,
                },
            )
            record_row = conn.execute(
                "SELECT * FROM life_records WHERE id = ?",
                (record_id,),
            ).fetchone()
            return self._row_to_record(record_row), self._row_to_reminder(reminder_row)

    def find_duplicate(
        self,
        user_id: str,
        record: LifeRecordCreate,
        limit: int = 5,
        exclude_record_id: str | None = None,
    ) -> list[DuplicateCandidate]:
        with connect(self.database_path) as conn:
            return self._find_duplicate_conn(
                conn,
                user_id,
                record,
                limit=limit,
                exclude_record_id=exclude_record_id,
            )

    def _find_duplicate_conn(
        self,
        conn: Any,
        user_id: str,
        record: LifeRecordCreate,
        limit: int = 5,
        exclude_record_id: str | None = None,
    ) -> list[DuplicateCandidate]:
        sql = """
            SELECT * FROM life_records
            WHERE user_id = ? AND type = ?
        """
        params: list[Any] = [
            user_id,
            record.record_type.value,
        ]
        if exclude_record_id is not None:
            sql += " AND id != ?"
            params.append(exclude_record_id)
        sql += " ORDER BY created_at DESC LIMIT 200"
        rows = conn.execute(sql, params).fetchall()
        candidates: list[DuplicateCandidate] = []
        for row in rows:
            existing = self._row_to_record(row)
            score = 0.0
            reasons: list[str] = []
            existing_order = str(existing.details.get("order_number") or "")
            new_order = str(record.details.get("order_number") or "")
            if existing_order and new_order and existing_order == new_order:
                score += 0.75
                reasons.append("订单号相同")

            existing_merchant = str(existing.details.get("merchant") or "")
            new_merchant = str(record.details.get("merchant") or "")
            if existing_merchant and new_merchant and existing_merchant == new_merchant:
                score += 0.1
                reasons.append("商家相同")

            if existing.title == record.title:
                score += 0.15
                reasons.append("标题相同")

            if existing.amount is not None and record.amount is not None:
                if abs(existing.amount - record.amount) < 0.01:
                    score += 0.15
                    reasons.append("金额相同")

            if existing.event_date and record.event_date and existing.event_date == record.event_date:
                score += 0.1
                reasons.append("日期相同")

            if score >= 0.45:
                candidates.append(
                    DuplicateCandidate(
                        record_id=existing.id,
                        title=existing.title,
                        reason="，".join(reasons),
                        score=min(score, 1.0),
                        archived=existing.archived_at is not None,
                    )
                )

        return sorted(candidates, key=lambda item: item.score, reverse=True)[:limit]

    def preview_record_update(
        self,
        user_id: str,
        record_id: str,
        patch: RecordUpdatePatch,
        expected_version: int,
        timezone_name: str,
        now: datetime,
    ) -> RecordUpdatePreview:
        with connect(self.database_path) as conn:
            row = conn.execute(
                "SELECT * FROM life_records WHERE user_id = ? AND id = ?",
                (user_id, record_id),
            ).fetchone()
            if not row:
                raise RecordUpdateError("record_not_found", "Record not found.")
            current = self._row_to_record(row)
            if current.archived_at is not None:
                raise RecordUpdateError(
                    "record_archived",
                    "Archived records must be restored before editing.",
                    current_record=current,
                )
            if current.version != expected_version:
                raise RecordUpdateError(
                    "version_conflict",
                    "Record version conflict.",
                    current_record=current,
                )

            reminders = self._list_record_reminders_conn(conn, user_id, record_id)
            preview = plan_record_update(
                current,
                patch,
                reminders,
                timezone_name,
                now,
            )
            if update_needs_duplicate_confirmation(preview.changed_fields):
                duplicates = self._find_duplicate_conn(
                    conn,
                    user_id,
                    LifeRecordCreate.model_validate(preview.record.model_dump()),
                    exclude_record_id=record_id,
                )
                preview = preview.model_copy(
                    update={"duplicate_candidates": duplicates}
                )
            return preview

    def update_record(
        self,
        user_id: str,
        record_id: str,
        patch: RecordUpdatePatch,
        expected_version: int,
        timezone_name: str,
        now: datetime,
        idempotency_key: str,
        duplicate_confirmed: bool,
        actor: str = "mcp",
    ) -> RecordUpdateResult:
        patch_payload = patch.model_dump(
            mode="json",
            include=patch.model_fields_set,
        )
        request_hash = stable_key(
            "record-update-request",
            user_id,
            record_id,
            expected_version,
            json.dumps(patch_payload, ensure_ascii=False, sort_keys=True),
            duplicate_confirmed,
        )
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            operation = conn.execute(
                """
                SELECT request_hash, result_json
                FROM record_update_operations
                WHERE user_id = ? AND idempotency_key = ?
                """,
                (user_id, idempotency_key),
            ).fetchone()
            if operation:
                if operation["request_hash"] != request_hash:
                    raise RecordUpdateError(
                        "idempotency_conflict",
                        "idempotency_key was already used for a different record update.",
                    )
                return RecordUpdateResult.model_validate_json(operation["result_json"])

            row = conn.execute(
                "SELECT * FROM life_records WHERE user_id = ? AND id = ?",
                (user_id, record_id),
            ).fetchone()
            if not row:
                raise RecordUpdateError("record_not_found", "Record not found.")
            current = self._row_to_record(row)
            if current.archived_at is not None:
                raise RecordUpdateError(
                    "record_archived",
                    "Archived records must be restored before editing.",
                    current_record=current,
                )
            if current.version != expected_version:
                raise RecordUpdateError(
                    "version_conflict",
                    "Record version conflict.",
                    current_record=current,
                )

            reminders = self._list_record_reminders_conn(conn, user_id, record_id)
            preview = plan_record_update(
                current,
                patch,
                reminders,
                timezone_name,
                now,
            )
            duplicates: list[DuplicateCandidate] = []
            if update_needs_duplicate_confirmation(preview.changed_fields):
                duplicates = self._find_duplicate_conn(
                    conn,
                    user_id,
                    LifeRecordCreate.model_validate(preview.record.model_dump()),
                    exclude_record_id=record_id,
                )
                if duplicates and not duplicate_confirmed:
                    raise RecordUpdateError(
                        "duplicate_confirmation_required",
                        "Possible duplicate records require explicit confirmation.",
                        duplicate_candidates=duplicates,
                    )

            updated_at = preview.record.updated_at.isoformat()
            cursor = conn.execute(
                """
                UPDATE life_records
                SET title = ?, amount = ?, currency = ?, event_date = ?, deadline = ?,
                    details_json = ?, notes = ?, version = version + 1, updated_at = ?
                WHERE user_id = ? AND id = ? AND version = ?
                """,
                (
                    preview.record.title,
                    preview.record.amount,
                    preview.record.currency,
                    _date_to_text(preview.record.event_date),
                    _date_to_text(preview.record.deadline),
                    json.dumps(preview.record.details, ensure_ascii=False, sort_keys=True),
                    preview.record.notes,
                    updated_at,
                    user_id,
                    record_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                latest_row = conn.execute(
                    "SELECT * FROM life_records WHERE user_id = ? AND id = ?",
                    (user_id, record_id),
                ).fetchone()
                raise RecordUpdateError(
                    "version_conflict",
                    "Record version conflict.",
                    current_record=self._row_to_record(latest_row) if latest_row else None,
                )

            reminder_ids_to_cancel = [
                reminder.id for reminder in preview.reminders_to_cancel
            ]
            if reminder_ids_to_cancel:
                placeholders = ",".join("?" for _ in reminder_ids_to_cancel)
                conn.execute(
                    f"""
                    UPDATE reminders
                    SET status = ?, updated_at = ?
                    WHERE user_id = ? AND id IN ({placeholders}) AND status IN (?, ?)
                    """,
                    [
                        ReminderStatus.CANCELLED.value,
                        updated_at,
                        user_id,
                        *reminder_ids_to_cancel,
                        ReminderStatus.PENDING.value,
                        ReminderStatus.SNOOZED.value,
                    ],
                )

            created_rows: list[Any] = []
            for index, reminder in enumerate(preview.reminders_to_create):
                reminder_id = str(uuid4())
                reminder_key = stable_key(
                    "record-update-reminder",
                    user_id,
                    idempotency_key,
                    index,
                    reminder.parent_id,
                    reminder.reminder_type.value,
                    reminder.scheduled_at.isoformat(),
                )
                conn.execute(
                    """
                    INSERT INTO reminders(
                        id, record_id, user_id, scheduled_at, reminder_type, message, status,
                        parent_id, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reminder_id,
                        record_id,
                        user_id,
                        _dt_to_text(reminder.scheduled_at),
                        reminder.reminder_type.value,
                        reminder.message,
                        ReminderStatus.PENDING.value,
                        reminder.parent_id,
                        reminder_key,
                        updated_at,
                        updated_at,
                    ),
                )
                created_rows.append(
                    conn.execute(
                        "SELECT * FROM reminders WHERE id = ?",
                        (reminder_id,),
                    ).fetchone()
                )

            cancelled_rows: list[Any] = []
            for reminder_id in reminder_ids_to_cancel:
                cancelled_rows.append(
                    conn.execute(
                        "SELECT * FROM reminders WHERE user_id = ? AND id = ?",
                        (user_id, reminder_id),
                    ).fetchone()
                )

            self._audit_conn(
                conn,
                user_id=user_id,
                actor=actor,
                action="update_record",
                target_id=record_id,
                result="ok",
                params={
                    "changed_fields": preview.changed_fields,
                    "old_version": expected_version,
                    "new_version": expected_version + 1,
                    "cancelled_reminder_count": len(cancelled_rows),
                    "created_reminder_count": len(created_rows),
                    "reminder_types": sorted(
                        {
                            row["reminder_type"]
                            for row in [*cancelled_rows, *created_rows]
                        }
                    ),
                },
            )
            updated_row = conn.execute(
                "SELECT * FROM life_records WHERE user_id = ? AND id = ?",
                (user_id, record_id),
            ).fetchone()
            result = RecordUpdateResult(
                record=self._row_to_record(updated_row),
                changed_fields=preview.changed_fields,
                cancelled_reminders=[
                    self._row_to_reminder(row)
                    for row in cancelled_rows
                    if row is not None
                ],
                created_reminders=[
                    self._row_to_reminder(row)
                    for row in created_rows
                    if row is not None
                ],
                warnings=preview.warnings,
                duplicate_candidates=duplicates,
            )
            conn.execute(
                """
                INSERT INTO record_update_operations(
                    user_id, idempotency_key, request_hash, record_id, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    idempotency_key,
                    request_hash,
                    record_id,
                    result.model_dump_json(),
                    updated_at,
                ),
            )
            return result

    def preview_record_status_update(
        self,
        user_id: str,
        record_id: str,
        new_status: RecordStatus,
        expected_version: int,
        now: datetime,
    ) -> RecordStatusUpdatePreview:
        with connect(self.database_path) as conn:
            row = conn.execute(
                "SELECT * FROM life_records WHERE user_id = ? AND id = ?",
                (user_id, record_id),
            ).fetchone()
            if not row:
                raise RecordUpdateError("record_not_found", "Record not found.")
            current = self._row_to_record(row)
            if current.archived_at is not None:
                raise RecordUpdateError(
                    "record_archived",
                    "Archived records must be restored before editing.",
                    current_record=current,
                )
            if current.version != expected_version:
                raise RecordUpdateError(
                    "version_conflict",
                    "Record version conflict.",
                    current_record=current,
                )
            reminders = self._list_record_reminders_conn(conn, user_id, record_id)
            return plan_record_status_update(current, new_status, reminders, now)

    def update_record_status(
        self,
        user_id: str,
        record_id: str,
        new_status: RecordStatus,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
        actor: str = "user",
    ) -> RecordStatusUpdateResult:
        request_hash = stable_key(
            "record-status-update-request",
            user_id,
            record_id,
            new_status.value,
            expected_version,
        )
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            operation = conn.execute(
                """
                SELECT request_hash, result_json
                FROM record_status_update_operations
                WHERE user_id = ? AND idempotency_key = ?
                """,
                (user_id, idempotency_key),
            ).fetchone()
            if operation:
                if operation["request_hash"] != request_hash:
                    raise RecordUpdateError(
                        "idempotency_conflict",
                        "idempotency_key was already used for a different status update.",
                    )
                return RecordStatusUpdateResult.model_validate_json(operation["result_json"])

            row = conn.execute(
                "SELECT * FROM life_records WHERE user_id = ? AND id = ?",
                (user_id, record_id),
            ).fetchone()
            if not row:
                raise RecordUpdateError("record_not_found", "Record not found.")
            current = self._row_to_record(row)
            if current.archived_at is not None:
                raise RecordUpdateError(
                    "record_archived",
                    "Archived records must be restored before editing.",
                    current_record=current,
                )
            if current.version != expected_version:
                raise RecordUpdateError(
                    "version_conflict",
                    "Record version conflict.",
                    current_record=current,
                )
            reminders = self._list_record_reminders_conn(conn, user_id, record_id)
            preview = plan_record_status_update(current, new_status, reminders, now)
            updated_at = preview.record.updated_at.isoformat()
            cursor = conn.execute(
                """
                UPDATE life_records
                SET status = ?, version = version + 1, updated_at = ?
                WHERE user_id = ? AND id = ? AND version = ?
                """,
                (new_status.value, updated_at, user_id, record_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise RecordUpdateError("version_conflict", "Record version conflict.")

            reminder_ids = [reminder.id for reminder in preview.reminders_to_cancel]
            if reminder_ids:
                placeholders = ",".join("?" for _ in reminder_ids)
                conn.execute(
                    f"""
                    UPDATE reminders
                    SET status = ?, updated_at = ?
                    WHERE user_id = ? AND id IN ({placeholders}) AND status IN (?, ?)
                    """,
                    [
                        ReminderStatus.CANCELLED.value,
                        updated_at,
                        user_id,
                        *reminder_ids,
                        ReminderStatus.PENDING.value,
                        ReminderStatus.SNOOZED.value,
                    ],
                )

            cancelled_rows = [
                conn.execute(
                    "SELECT * FROM reminders WHERE user_id = ? AND id = ?",
                    (user_id, reminder_id),
                ).fetchone()
                for reminder_id in reminder_ids
            ]
            self._audit_conn(
                conn,
                user_id=user_id,
                actor=actor,
                action="update_record_status",
                target_id=record_id,
                result="ok",
                params={
                    "new_status": new_status.value,
                    "old_version": expected_version,
                    "new_version": expected_version + 1,
                    "cancelled_reminder_count": len(cancelled_rows),
                    "reminder_types": sorted(
                        {row["reminder_type"] for row in cancelled_rows if row is not None}
                    ),
                },
            )
            updated_row = conn.execute(
                "SELECT * FROM life_records WHERE user_id = ? AND id = ?",
                (user_id, record_id),
            ).fetchone()
            result = RecordStatusUpdateResult(
                record=self._row_to_record(updated_row),
                cancelled_reminders=[
                    self._row_to_reminder(row)
                    for row in cancelled_rows
                    if row is not None
                ],
                warnings=preview.warnings,
            )
            conn.execute(
                """
                INSERT INTO record_status_update_operations(
                    user_id, idempotency_key, request_hash, record_id, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    idempotency_key,
                    request_hash,
                    record_id,
                    result.model_dump_json(),
                    updated_at,
                ),
            )
            return result

    def preview_record_archive(
        self,
        user_id: str,
        record_id: str,
        expected_version: int,
        now: datetime,
    ) -> RecordLifecyclePreview:
        with connect(self.database_path) as conn:
            current = self._get_record_for_lifecycle_conn(
                conn, user_id, record_id, expected_version
            )
            reminders = self._list_record_reminders_conn(conn, user_id, record_id)
            return plan_record_archive(current, reminders, now)

    def archive_record(
        self,
        user_id: str,
        record_id: str,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
        actor: str = "user",
    ) -> RecordLifecycleResult:
        return self._update_record_lifecycle(
            user_id=user_id,
            record_id=record_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            now=now,
            operation="archive_record",
            actor=actor,
        )

    def preview_record_restore(
        self,
        user_id: str,
        record_id: str,
        expected_version: int,
        now: datetime,
    ) -> RecordLifecyclePreview:
        with connect(self.database_path) as conn:
            current = self._get_record_for_lifecycle_conn(
                conn, user_id, record_id, expected_version
            )
            return plan_record_restore(current, now)

    def restore_record(
        self,
        user_id: str,
        record_id: str,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
        actor: str = "user",
    ) -> RecordLifecycleResult:
        return self._update_record_lifecycle(
            user_id=user_id,
            record_id=record_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            now=now,
            operation="restore_record",
            actor=actor,
        )

    def _update_record_lifecycle(
        self,
        *,
        user_id: str,
        record_id: str,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
        operation: str,
        actor: str,
    ) -> RecordLifecycleResult:
        request_hash = stable_key(
            "record-lifecycle-request",
            user_id,
            record_id,
            expected_version,
            operation,
        )
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                """
                SELECT request_hash, result_json
                FROM record_lifecycle_operations
                WHERE user_id = ? AND idempotency_key = ?
                """,
                (user_id, idempotency_key),
            ).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise RecordUpdateError(
                        "idempotency_conflict",
                        "idempotency_key was already used for a different lifecycle update.",
                    )
                return RecordLifecycleResult.model_validate_json(previous["result_json"])

            current = self._get_record_for_lifecycle_conn(
                conn, user_id, record_id, expected_version
            )
            reminders = self._list_record_reminders_conn(conn, user_id, record_id)
            if operation == "archive_record":
                preview = plan_record_archive(current, reminders, now)
            elif operation == "restore_record":
                preview = plan_record_restore(current, now)
            else:
                raise ValueError("Unsupported lifecycle operation.")

            updated_at = preview.record.updated_at.isoformat()
            archived_at = _dt_to_text(preview.record.archived_at)
            cursor = conn.execute(
                """
                UPDATE life_records
                SET archived_at = ?, version = version + 1, updated_at = ?
                WHERE user_id = ? AND id = ? AND version = ?
                """,
                (archived_at, updated_at, user_id, record_id, expected_version),
            )
            if cursor.rowcount != 1:
                latest_row = conn.execute(
                    "SELECT * FROM life_records WHERE user_id = ? AND id = ?",
                    (user_id, record_id),
                ).fetchone()
                raise RecordUpdateError(
                    "version_conflict",
                    "Record version conflict.",
                    current_record=self._row_to_record(latest_row) if latest_row else None,
                )

            reminder_ids = [reminder.id for reminder in preview.reminders_to_cancel]
            if reminder_ids:
                placeholders = ",".join("?" for _ in reminder_ids)
                conn.execute(
                    f"""
                    UPDATE reminders
                    SET status = ?, updated_at = ?
                    WHERE user_id = ? AND id IN ({placeholders}) AND status IN (?, ?)
                    """,
                    [
                        ReminderStatus.CANCELLED.value,
                        updated_at,
                        user_id,
                        *reminder_ids,
                        ReminderStatus.PENDING.value,
                        ReminderStatus.SNOOZED.value,
                    ],
                )
            cancelled_rows = [
                conn.execute(
                    "SELECT * FROM reminders WHERE user_id = ? AND id = ?",
                    (user_id, reminder_id),
                ).fetchone()
                for reminder_id in reminder_ids
            ]

            params: dict[str, Any] = {
                "old_version": expected_version,
                "new_version": expected_version + 1,
            }
            if operation == "archive_record":
                params.update(
                    {
                        "cancelled_reminder_count": len(cancelled_rows),
                        "reminder_types": sorted(
                            {
                                row["reminder_type"]
                                for row in cancelled_rows
                                if row is not None
                            }
                        ),
                    }
                )
            self._audit_conn(
                conn,
                user_id=user_id,
                actor=actor,
                action=operation,
                target_id=record_id,
                result="ok",
                params=params,
            )
            updated_row = conn.execute(
                "SELECT * FROM life_records WHERE user_id = ? AND id = ?",
                (user_id, record_id),
            ).fetchone()
            result = RecordLifecycleResult(
                record=self._row_to_record(updated_row),
                operation=operation,
                cancelled_reminders=[
                    self._row_to_reminder(row)
                    for row in cancelled_rows
                    if row is not None
                ],
                warnings=preview.warnings,
            )
            conn.execute(
                """
                INSERT INTO record_lifecycle_operations(
                    user_id, idempotency_key, request_hash, operation,
                    record_id, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    idempotency_key,
                    request_hash,
                    operation,
                    record_id,
                    result.model_dump_json(),
                    updated_at,
                ),
            )
            return result

    def _get_record_for_lifecycle_conn(
        self,
        conn: Any,
        user_id: str,
        record_id: str,
        expected_version: int,
    ) -> LifeRecord:
        row = conn.execute(
            "SELECT * FROM life_records WHERE user_id = ? AND id = ?",
            (user_id, record_id),
        ).fetchone()
        if not row:
            raise RecordUpdateError("record_not_found", "Record not found.")
        current = self._row_to_record(row)
        if current.version != expected_version:
            raise RecordUpdateError(
                "version_conflict",
                "Record version conflict.",
                current_record=current,
            )
        return current

    def _list_record_reminders_conn(
        self,
        conn: Any,
        user_id: str,
        record_id: str,
    ) -> list[Reminder]:
        rows = conn.execute(
            """
            SELECT * FROM reminders
            WHERE user_id = ? AND record_id = ?
            ORDER BY created_at ASC, updated_at ASC
            """,
            (user_id, record_id),
        ).fetchall()
        return [self._row_to_reminder(row) for row in rows]

    def create_reminder(
        self,
        user_id: str,
        reminder: ReminderCreate,
        idempotency_key: str,
        actor: str = "agent",
    ) -> Reminder:
        with connect(self.database_path) as conn:
            existing = conn.execute(
                "SELECT * FROM reminders WHERE user_id = ? AND idempotency_key = ?",
                (user_id, idempotency_key),
            ).fetchone()
            if existing:
                return self._row_to_reminder(existing)

            duplicate = conn.execute(
                """
                SELECT * FROM reminders
                WHERE record_id = ? AND reminder_type = ? AND scheduled_at = ?
                  AND status IN (?, ?)
                """,
                (
                    reminder.record_id,
                    reminder.reminder_type.value,
                    _dt_to_text(reminder.scheduled_at),
                    ReminderStatus.PENDING.value,
                    ReminderStatus.SENDING.value,
                ),
            ).fetchone()
            if duplicate:
                return self._row_to_reminder(duplicate)

            now = _utc_now().isoformat()
            reminder_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO reminders(
                    id, record_id, user_id, scheduled_at, reminder_type, message, status,
                    parent_id, idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder_id,
                    reminder.record_id,
                    user_id,
                    _dt_to_text(reminder.scheduled_at),
                    reminder.reminder_type.value,
                    reminder.message,
                    ReminderStatus.PENDING.value,
                    reminder.parent_id,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            self._audit_conn(
                conn,
                user_id=user_id,
                actor=actor,
                action="create_reminder",
                target_id=reminder_id,
                result="ok",
                params={
                    "reminder_type": reminder.reminder_type.value,
                    "scheduled_at": _dt_to_text(reminder.scheduled_at),
                },
            )
            row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
            return self._row_to_reminder(row)

    def create_reminders(
        self,
        user_id: str,
        batch: ReminderBatchCreate,
        actor: str = "mcp",
    ) -> list[Reminder]:
        with connect(self.database_path) as conn:
            request_hash = stable_key(
                "reminder-batch-request",
                json.dumps(
                    [reminder.model_dump(mode="json") for reminder in batch.reminders],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            existing_batch = conn.execute(
                """
                SELECT request_hash, reminder_ids_json
                FROM reminder_batches
                WHERE user_id = ? AND idempotency_key = ?
                """,
                (user_id, batch.idempotency_key),
            ).fetchone()
            if existing_batch:
                if existing_batch["request_hash"] != request_hash:
                    raise ValueError("Batch idempotency key was already used for different reminders.")
                reminder_ids = json.loads(existing_batch["reminder_ids_json"])
                rows = [
                    conn.execute(
                        "SELECT * FROM reminders WHERE user_id = ? AND id = ?",
                        (user_id, reminder_id),
                    ).fetchone()
                    for reminder_id in reminder_ids
                ]
                if any(row is None for row in rows):
                    raise ValueError("Stored reminder batch is incomplete.")
                return [self._row_to_reminder(row) for row in rows]

            record = conn.execute(
                "SELECT id FROM life_records WHERE user_id = ? AND id = ?",
                (user_id, batch.record_id),
            ).fetchone()
            if not record:
                raise ValueError("Record not found.")

            rows: list[Any] = []
            now = _utc_now().isoformat()
            for index, reminder in enumerate(batch.reminders):
                item_key = stable_key(
                    "reminder-batch-item",
                    user_id,
                    batch.idempotency_key,
                    index,
                    reminder.record_id,
                    reminder.reminder_type.value,
                    reminder.scheduled_at.isoformat(),
                )
                row = conn.execute(
                    "SELECT * FROM reminders WHERE user_id = ? AND idempotency_key = ?",
                    (user_id, item_key),
                ).fetchone()
                if not row:
                    row = conn.execute(
                        """
                        SELECT * FROM reminders
                        WHERE user_id = ? AND record_id = ? AND reminder_type = ? AND scheduled_at = ?
                          AND status IN (?, ?)
                        """,
                        (
                            user_id,
                            reminder.record_id,
                            reminder.reminder_type.value,
                            _dt_to_text(reminder.scheduled_at),
                            ReminderStatus.PENDING.value,
                            ReminderStatus.SENDING.value,
                        ),
                    ).fetchone()
                if not row:
                    reminder_id = str(uuid4())
                    conn.execute(
                        """
                        INSERT INTO reminders(
                            id, record_id, user_id, scheduled_at, reminder_type, message, status,
                            parent_id, idempotency_key, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            reminder_id,
                            reminder.record_id,
                            user_id,
                            _dt_to_text(reminder.scheduled_at),
                            reminder.reminder_type.value,
                            reminder.message,
                            ReminderStatus.PENDING.value,
                            reminder.parent_id,
                            item_key,
                            now,
                            now,
                        ),
                    )
                    row = conn.execute(
                        "SELECT * FROM reminders WHERE id = ?",
                        (reminder_id,),
                    ).fetchone()
                rows.append(row)

            conn.execute(
                """
                INSERT INTO reminder_batches(
                    user_id, idempotency_key, request_hash, reminder_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    batch.idempotency_key,
                    request_hash,
                    json.dumps([row["id"] for row in rows]),
                    now,
                ),
            )
            self._audit_conn(
                conn,
                user_id=user_id,
                actor=actor,
                action="create_reminders",
                target_id=batch.record_id,
                result="ok",
                params={
                    "reminder_count": len(batch.reminders),
                    "reminder_types": [
                        reminder.reminder_type.value for reminder in batch.reminders
                    ],
                },
            )
            return [self._row_to_reminder(row) for row in rows]

    def list_reminders(
        self,
        user_id: str,
        status: ReminderStatus | None = None,
        limit: int = 100,
    ) -> list[Reminder]:
        sql = "SELECT * FROM reminders WHERE user_id = ?"
        params: list[Any] = [user_id]
        if status:
            sql += " AND status = ?"
            params.append(status.value)
        sql += " ORDER BY scheduled_at ASC LIMIT ?"
        params.append(limit)
        with connect(self.database_path) as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_reminder(row) for row in rows]

    def get_reminder(self, user_id: str, reminder_id: str) -> Reminder | None:
        with connect(self.database_path) as conn:
            row = conn.execute(
                "SELECT * FROM reminders WHERE user_id = ? AND id = ?",
                (user_id, reminder_id),
            ).fetchone()
            return self._row_to_reminder(row) if row else None

    def snooze_reminder(
        self,
        user_id: str,
        reminder_id: str,
        new_scheduled_at: datetime,
        idempotency_key: str,
        actor: str = "agent",
    ) -> Reminder:
        with connect(self.database_path) as conn:
            existing_new = conn.execute(
                "SELECT * FROM reminders WHERE user_id = ? AND idempotency_key = ?",
                (user_id, idempotency_key),
            ).fetchone()
            if existing_new:
                return self._row_to_reminder(existing_new)

            original = conn.execute(
                "SELECT * FROM reminders WHERE user_id = ? AND id = ?",
                (user_id, reminder_id),
            ).fetchone()
            if not original:
                raise ValueError("Reminder not found.")
            if original["status"] == ReminderStatus.CANCELLED.value:
                raise ValueError("Cancelled reminders cannot be snoozed.")
            if original["status"] in {ReminderStatus.SENT.value, ReminderStatus.FAILED.value}:
                raise ValueError("Sent or failed reminders cannot be snoozed.")

            duplicate = conn.execute(
                """
                SELECT * FROM reminders
                WHERE record_id = ? AND reminder_type = ? AND scheduled_at = ?
                  AND status IN (?, ?)
                """,
                (
                    original["record_id"],
                    original["reminder_type"],
                    _dt_to_text(new_scheduled_at),
                    ReminderStatus.PENDING.value,
                    ReminderStatus.SENDING.value,
                ),
            ).fetchone()
            if duplicate:
                return self._row_to_reminder(duplicate)

            now = _utc_now().isoformat()
            conn.execute(
                """
                UPDATE reminders
                SET status = ?, updated_at = ?
                WHERE user_id = ? AND id = ?
                """,
                (ReminderStatus.SNOOZED.value, now, user_id, reminder_id),
            )

            new_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO reminders(
                    id, record_id, user_id, scheduled_at, reminder_type, message, status,
                    parent_id, idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    original["record_id"],
                    user_id,
                    _dt_to_text(new_scheduled_at),
                    original["reminder_type"],
                    original["message"],
                    ReminderStatus.PENDING.value,
                    reminder_id,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            self._audit_conn(
                conn,
                user_id=user_id,
                actor=actor,
                action="snooze_reminder",
                target_id=reminder_id,
                result="ok",
                params={
                    "new_reminder_id": new_id,
                    "new_scheduled_at": _dt_to_text(new_scheduled_at),
                },
            )
            row = conn.execute("SELECT * FROM reminders WHERE id = ?", (new_id,)).fetchone()
            return self._row_to_reminder(row)

    def claim_due_reminders(
        self,
        user_id: str,
        now: datetime,
        limit: int = 20,
    ) -> list[Reminder]:
        with connect(self.database_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM reminders
                WHERE user_id = ? AND status = ? AND scheduled_at <= ?
                ORDER BY scheduled_at ASC
                LIMIT ?
                """,
                (user_id, ReminderStatus.PENDING.value, now.isoformat(), limit),
            ).fetchall()
            claimed: list[Reminder] = []
            for row in rows:
                updated = conn.execute(
                    """
                    UPDATE reminders
                    SET status = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (ReminderStatus.SENDING.value, _utc_now().isoformat(), row["id"], ReminderStatus.PENDING.value),
                )
                if updated.rowcount == 1:
                    refreshed = conn.execute("SELECT * FROM reminders WHERE id = ?", (row["id"],)).fetchone()
                    claimed.append(self._row_to_reminder(refreshed))
            return claimed

    def mark_reminder_sent(self, user_id: str, reminder_id: str) -> None:
        with connect(self.database_path) as conn:
            now = _utc_now().isoformat()
            conn.execute(
                """
                UPDATE reminders
                SET status = ?, sent_at = ?, updated_at = ?
                WHERE user_id = ? AND id = ?
                """,
                (ReminderStatus.SENT.value, now, now, user_id, reminder_id),
            )
            self._audit_conn(
                conn,
                user_id=user_id,
                actor="worker",
                action="send_reminder",
                target_id=reminder_id,
                result="ok",
                params={"delivery": "desktop"},
            )

    def mark_reminder_failed(self, user_id: str, reminder_id: str, error_code: str) -> None:
        with connect(self.database_path) as conn:
            now = _utc_now().isoformat()
            conn.execute(
                """
                UPDATE reminders
                SET status = ?, updated_at = ?
                WHERE user_id = ? AND id = ?
                """,
                (ReminderStatus.FAILED.value, now, user_id, reminder_id),
            )
            self._audit_conn(
                conn,
                user_id=user_id,
                actor="worker",
                action="send_reminder",
                target_id=reminder_id,
                result="failed",
                params={"error_code": error_code},
            )

    def mark_reminder_cancelled_by_worker(
        self,
        user_id: str,
        reminder_id: str,
        record_status: str,
    ) -> None:
        with connect(self.database_path) as conn:
            now = _utc_now().isoformat()
            conn.execute(
                """
                UPDATE reminders
                SET status = ?, updated_at = ?
                WHERE user_id = ? AND id = ?
                """,
                (ReminderStatus.CANCELLED.value, now, user_id, reminder_id),
            )
            self._audit_conn(
                conn,
                user_id=user_id,
                actor="worker",
                action="cancel_reminder",
                target_id=reminder_id,
                result="ok",
                params={"record_status": record_status},
            )

    def cancel_reminder(
        self,
        user_id: str,
        reminder_id: str,
        user_confirmed: bool,
        actor: str = "user",
    ) -> Reminder:
        if not user_confirmed:
            raise PermissionError("Reminder cancellation requires user confirmation.")
        with connect(self.database_path) as conn:
            now = _utc_now().isoformat()
            cursor = conn.execute(
                """
                UPDATE reminders
                SET status = ?, updated_at = ?
                WHERE user_id = ? AND id = ? AND status IN (?, ?)
                """,
                (
                    ReminderStatus.CANCELLED.value,
                    now,
                    user_id,
                    reminder_id,
                    ReminderStatus.PENDING.value,
                    ReminderStatus.SNOOZED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Reminder not found or cannot be cancelled.")
            self._audit_conn(
                conn,
                user_id=user_id,
                actor=actor,
                action="cancel_reminder",
                target_id=reminder_id,
                result="ok",
            )
            row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
            return self._row_to_reminder(row)

    def record_audit_event(
        self,
        user_id: str,
        actor: str,
        action: str,
        target_id: str | None,
        result: str,
        params: dict[str, Any] | None = None,
    ) -> AuditLog:
        with connect(self.database_path) as conn:
            cursor = self._audit_conn(
                conn,
                user_id=user_id,
                actor=actor,
                action=action,
                target_id=target_id,
                result=result,
                params=params,
            )
            row = conn.execute("SELECT * FROM audit_logs WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return self._row_to_audit_log(row)

    def list_audit_logs(
        self,
        user_id: str,
        actor: str | None = None,
        action: str | None = None,
        result: str | None = None,
        before_id: int | None = None,
        limit: int = 100,
    ) -> list[AuditLog]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200.")
        if before_id is not None and before_id <= 0:
            raise ValueError("before_id must be positive.")

        sql = "SELECT * FROM audit_logs WHERE user_id = ?"
        params: list[Any] = [user_id]
        for column, value in (("actor", actor), ("action", action), ("result", result)):
            if value:
                sql += f" AND {column} = ?"
                params.append(value)
        if before_id is not None:
            sql += " AND id < ?"
            params.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with connect(self.database_path) as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_audit_log(row) for row in rows]

    def save_checkpoint(self, thread_id: str, checkpoint_data: dict[str, Any]) -> None:
        with connect(self.database_path) as conn:
            conn.execute(
                """
                INSERT INTO graph_checkpoints(thread_id, checkpoint_data, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    checkpoint_data = excluded.checkpoint_data,
                    updated_at = excluded.updated_at
                """,
                (
                    thread_id,
                    json.dumps(checkpoint_data, ensure_ascii=False, default=str),
                    _utc_now().isoformat(),
                ),
            )

    def load_checkpoint(self, thread_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as conn:
            row = conn.execute(
                "SELECT checkpoint_data FROM graph_checkpoints WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            return json.loads(row["checkpoint_data"]) if row else None

    def _audit_conn(
        self,
        conn: Any,
        user_id: str,
        actor: str,
        action: str,
        target_id: str | None,
        result: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return conn.execute(
            """
            INSERT INTO audit_logs(user_id, actor, action, target_id, result, params_summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                actor,
                action,
                sanitize_audit_target_id(target_id),
                result,
                sanitize_audit_params(action, params),
                _utc_now().isoformat(),
            ),
        )

    def _row_to_audit_log(self, row: Any) -> AuditLog:
        return AuditLog(
            id=row["id"],
            user_id=row["user_id"],
            actor=row["actor"],
            action=row["action"],
            target_id=sanitize_audit_target_id(row["target_id"]),
            result=row["result"],
            params_summary=sanitize_audit_params(row["action"], row["params_summary"]),
            created_at=_parse_datetime(row["created_at"]),
        )

    def _row_to_record(self, row: Any) -> LifeRecord:
        return LifeRecord(
            id=row["id"],
            user_id=row["user_id"],
            record_type=RecordType(row["type"]),
            title=row["title"],
            amount=row["amount"],
            currency=row["currency"],
            event_date=_parse_date(row["event_date"]),
            deadline=_parse_date(row["deadline"]),
            status=RecordStatus(row["status"]),
            version=row["version"],
            details=json.loads(row["details_json"] or "{}"),
            notes=row["notes"],
            source_text_hash=row["source_text_hash"],
            source_text_preview=row["source_text_preview"],
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
            archived_at=_parse_datetime(row["archived_at"]),
        )

    def _row_to_reminder(self, row: Any) -> Reminder:
        return Reminder(
            id=row["id"],
            record_id=row["record_id"],
            user_id=row["user_id"],
            scheduled_at=_parse_datetime(row["scheduled_at"]),
            reminder_type=row["reminder_type"],
            message=row["message"],
            status=ReminderStatus(row["status"]),
            parent_id=row["parent_id"],
            idempotency_key=row["idempotency_key"],
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
            sent_at=_parse_datetime(row["sent_at"]),
        )
