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


class InProcessPersonalVaultMcpClient:
    """Synchronous client facade over the in-process FastMCP server."""

    def __init__(self, settings: Settings, repository: VaultRepository | None = None):
        self._server = create_server(settings=settings, repository=repository)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = _run_coro(self._server.call_tool(name, arguments))
        return _coerce_tool_result(result)

    def find_duplicate(self, record: dict[str, Any], limit: int = 5) -> dict[str, Any]:
        return self.call_tool("find_duplicate", {"record": record, "limit": limit})

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
