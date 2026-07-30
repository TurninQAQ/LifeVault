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
    RecordStatus,
    RecordType,
    Reminder,
    ReminderCreate,
    ReminderStatus,
    UserPreference,
)
from lifevault.storage.database import connect, init_db


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


class VaultRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        init_db(database_path)

    def get_preferences(self, user_id: str) -> UserPreference:
        with connect(self.database_path) as conn:
            row = conn.execute(
                "SELECT * FROM user_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row:
                return UserPreference(
                    user_id=row["user_id"],
                    default_time=row["default_time"],
                    quiet_hours_start=row["quiet_hours_start"],
                    quiet_hours_end=row["quiet_hours_end"],
                    default_advance_days=row["default_advance_days"],
                )

            preference = UserPreference(user_id=user_id)
            conn.execute(
                """
                INSERT INTO user_preferences(
                    user_id, default_time, quiet_hours_start, quiet_hours_end, default_advance_days
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    preference.user_id,
                    preference.default_time,
                    preference.quiet_hours_start,
                    preference.quiet_hours_end,
                    preference.default_advance_days,
                ),
            )
            return preference

    def update_preferences(self, preference: UserPreference) -> UserPreference:
        with connect(self.database_path) as conn:
            conn.execute(
                """
                INSERT INTO user_preferences(
                    user_id, default_time, quiet_hours_start, quiet_hours_end, default_advance_days
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    default_time = excluded.default_time,
                    quiet_hours_start = excluded.quiet_hours_start,
                    quiet_hours_end = excluded.quiet_hours_end,
                    default_advance_days = excluded.default_advance_days
                """,
                (
                    preference.user_id,
                    preference.default_time,
                    preference.quiet_hours_start,
                    preference.quiet_hours_end,
                    preference.default_advance_days,
                ),
            )
        return preference

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
    ) -> list[LifeRecord]:
        sql = "SELECT * FROM life_records WHERE user_id = ?"
        params: list[Any] = [user_id]

        if record_types:
            placeholders = ",".join("?" for _ in record_types)
            sql += f" AND type IN ({placeholders})"
            params.extend(record_type.value for record_type in record_types)
        if date_from:
            sql += " AND (deadline IS NULL OR deadline >= ?)"
            params.append(date_from.isoformat())
        if date_to:
            sql += " AND (deadline IS NULL OR deadline <= ?)"
            params.append(date_to.isoformat())
        if query:
            like = f"%{query}%"
            sql += " AND (title LIKE ? OR details_json LIKE ? OR source_text_preview LIKE ?)"
            params.extend([like, like, like])

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

    def find_duplicate(
        self,
        user_id: str,
        record: LifeRecordCreate,
        limit: int = 5,
    ) -> list[DuplicateCandidate]:
        with connect(self.database_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM life_records
                WHERE user_id = ? AND type = ? AND status != ?
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (user_id, record.record_type.value, RecordStatus.CANCELLED.value),
            ).fetchall()

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
                    )
                )

        return sorted(candidates, key=lambda item: item.score, reverse=True)[:limit]

    def update_record_status(
        self,
        user_id: str,
        record_id: str,
        new_status: RecordStatus,
        expected_version: int,
        actor: str = "user",
    ) -> LifeRecord:
        with connect(self.database_path) as conn:
            now = _utc_now().isoformat()
            cursor = conn.execute(
                """
                UPDATE life_records
                SET status = ?, version = version + 1, updated_at = ?
                WHERE user_id = ? AND id = ? AND version = ?
                """,
                (new_status.value, now, user_id, record_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise ValueError("Record version conflict or record not found.")
            self._audit_conn(
                conn,
                user_id=user_id,
                actor=actor,
                action="update_record_status",
                target_id=record_id,
                result="ok",
                params={"new_status": new_status.value},
            )
            row = conn.execute("SELECT * FROM life_records WHERE id = ?", (record_id,)).fetchone()
            return self._row_to_record(row)

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
                """,
                (
                    reminder.record_id,
                    reminder.reminder_type.value,
                    _dt_to_text(reminder.scheduled_at),
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
                """,
                (
                    original["record_id"],
                    original["reminder_type"],
                    _dt_to_text(new_scheduled_at),
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
