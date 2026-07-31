from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Protocol

from lifevault.config import Settings
from lifevault.mcp_server.server import create_server
from lifevault.storage.repository import VaultRepository


class PersonalVaultMcpClient(Protocol):
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    def find_duplicate(self, record: dict[str, Any], limit: int = 5) -> dict[str, Any]:
        ...

    def search_records(
        self,
        query: str | None = None,
        record_types: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        ...

    def get_record(self, record_id: str) -> dict[str, Any]:
        ...

    def preview_record_update(
        self,
        record_id: str,
        changes: dict[str, Any],
        expected_version: int,
    ) -> dict[str, Any]:
        ...

    def update_record(
        self,
        record_id: str,
        changes: dict[str, Any],
        expected_version: int,
        idempotency_key: str,
        user_confirmed: bool,
        duplicate_confirmed: bool = False,
    ) -> dict[str, Any]:
        ...

    def get_preferences(self) -> dict[str, Any]:
        ...

    def save_record(
        self,
        record: dict[str, Any],
        idempotency_key: str,
        user_confirmed: bool,
        source_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        ...

    def create_reminder(
        self,
        record_id: str,
        scheduled_at: str,
        reminder_type: str,
        idempotency_key: str,
        user_confirmed: bool,
        message: str | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        ...

    def create_reminders(
        self,
        reminders: list[dict[str, Any]],
        idempotency_key: str,
        user_confirmed: bool,
    ) -> dict[str, Any]:
        ...

    def list_upcoming_subscriptions(
        self,
        days: int = 30,
        include_auto_renew: bool = True,
        limit: int = 20,
    ) -> dict[str, Any]:
        ...

    def list_reminders(self, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        ...

    def list_audit_logs(
        self,
        actor: str | None = None,
        action: str | None = None,
        result: str | None = None,
        before_id: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        ...

    def snooze_reminder(self, reminder_id: str, new_scheduled_at: str) -> dict[str, Any]:
        ...

    def update_record_status(self, record_id: str, new_status: str, expected_version: int) -> dict[str, Any]:
        ...

    def update_preferences(
        self,
        preferences: dict[str, Any],
        user_confirmed: bool,
    ) -> dict[str, Any]:
        ...

    def cancel_reminder(self, reminder_id: str, user_confirmed: bool) -> dict[str, Any]:
        ...


class InProcessPersonalVaultMcpClient:
    """Synchronous client facade over the in-process FastMCP server."""

    def __init__(self, settings: Settings, repository: VaultRepository | None = None):
        self._server = create_server(settings=settings, repository=repository)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = _run_coro(self._server.call_tool(name, arguments))
        return _coerce_tool_result(result)

    def find_duplicate(self, record: dict[str, Any], limit: int = 5) -> dict[str, Any]:
        return self.call_tool("find_duplicate", {"record": record, "limit": limit})

    def search_records(
        self,
        query: str | None = None,
        record_types: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self.call_tool(
            "search_records",
            {
                "query": query,
                "record_types": record_types,
                "date_from": date_from,
                "date_to": date_to,
                "limit": limit,
            },
        )

    def get_record(self, record_id: str) -> dict[str, Any]:
        return self.call_tool("get_record", {"record_id": record_id})

    def preview_record_update(
        self,
        record_id: str,
        changes: dict[str, Any],
        expected_version: int,
    ) -> dict[str, Any]:
        return self.call_tool(
            "preview_record_update",
            {
                "record_id": record_id,
                "changes": changes,
                "expected_version": expected_version,
            },
        )

    def update_record(
        self,
        record_id: str,
        changes: dict[str, Any],
        expected_version: int,
        idempotency_key: str,
        user_confirmed: bool,
        duplicate_confirmed: bool = False,
    ) -> dict[str, Any]:
        return self.call_tool(
            "update_record",
            {
                "record_id": record_id,
                "changes": changes,
                "expected_version": expected_version,
                "idempotency_key": idempotency_key,
                "user_confirmed": user_confirmed,
                "duplicate_confirmed": duplicate_confirmed,
            },
        )

    def get_preferences(self) -> dict[str, Any]:
        return self.call_tool("get_preferences", {})

    def save_record(
        self,
        record: dict[str, Any],
        idempotency_key: str,
        user_confirmed: bool,
        source_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.call_tool(
            "save_record",
            {
                "record": record,
                "idempotency_key": idempotency_key,
                "user_confirmed": user_confirmed,
                "source_ids": source_ids or [],
            },
        )

    def create_reminder(
        self,
        record_id: str,
        scheduled_at: str,
        reminder_type: str,
        idempotency_key: str,
        user_confirmed: bool,
        message: str | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        return self.call_tool(
            "create_reminder",
            {
                "record_id": record_id,
                "scheduled_at": scheduled_at,
                "reminder_type": reminder_type,
                "idempotency_key": idempotency_key,
                "user_confirmed": user_confirmed,
                "message": message,
                "parent_id": parent_id,
            },
        )

    def create_reminders(
        self,
        reminders: list[dict[str, Any]],
        idempotency_key: str,
        user_confirmed: bool,
    ) -> dict[str, Any]:
        return self.call_tool(
            "create_reminders",
            {
                "reminders": reminders,
                "idempotency_key": idempotency_key,
                "user_confirmed": user_confirmed,
            },
        )

    def list_upcoming_subscriptions(
        self,
        days: int = 30,
        include_auto_renew: bool = True,
        limit: int = 20,
    ) -> dict[str, Any]:
        return self.call_tool(
            "list_upcoming_subscriptions",
            {
                "days": days,
                "include_auto_renew": include_auto_renew,
                "limit": limit,
            },
        )

    def list_reminders(self, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        return self.call_tool("list_reminders", {"status": status, "limit": limit})

    def list_audit_logs(
        self,
        actor: str | None = None,
        action: str | None = None,
        result: str | None = None,
        before_id: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self.call_tool(
            "list_audit_logs",
            {
                "actor": actor,
                "action": action,
                "result": result,
                "before_id": before_id,
                "limit": limit,
            },
        )

    def snooze_reminder(self, reminder_id: str, new_scheduled_at: str) -> dict[str, Any]:
        return self.call_tool(
            "snooze_reminder",
            {
                "reminder_id": reminder_id,
                "new_scheduled_at": new_scheduled_at,
            },
        )

    def update_record_status(self, record_id: str, new_status: str, expected_version: int) -> dict[str, Any]:
        return self.call_tool(
            "update_record_status",
            {
                "record_id": record_id,
                "new_status": new_status,
                "expected_version": expected_version,
            },
        )

    def update_preferences(
        self,
        preferences: dict[str, Any],
        user_confirmed: bool,
    ) -> dict[str, Any]:
        return self.call_tool(
            "update_preferences",
            {
                "preferences": preferences,
                "user_confirmed": user_confirmed,
            },
        )

    def cancel_reminder(self, reminder_id: str, user_confirmed: bool) -> dict[str, Any]:
        return self.call_tool(
            "cancel_reminder",
            {
                "reminder_id": reminder_id,
                "user_confirmed": user_confirmed,
            },
        )


def _run_coro(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _coerce_tool_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if isinstance(result, tuple) and len(result) == 2:
        _content, structured = result
        if isinstance(structured, dict):
            return structured
        result = _content
    if isinstance(result, list) and result:
        text = getattr(result[0], "text", None)
        if text is not None:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
    raise TypeError(f"Unsupported MCP tool result: {result!r}")
