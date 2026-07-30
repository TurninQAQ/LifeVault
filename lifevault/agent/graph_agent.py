from __future__ import annotations

import sqlite3
from typing import Any, Literal, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from lifevault.agent.service import LifeVaultAgent
from lifevault.config import Settings
from lifevault.hooks.privacy_hooks import sanitize_input
from lifevault.models.schemas import (
    ExtractedRecordCandidate,
    GraphTurn,
    LifeRecordCreate,
    ReminderCreate,
)
from lifevault.mcp_server.client import InProcessPersonalVaultMcpClient, PersonalVaultMcpClient
from lifevault.storage.repository import VaultRepository
from lifevault.tools.date_tools import now_in_timezone
from lifevault.tools.idempotency import stable_key


class LifeVaultGraphState(TypedDict, total=False):
    thread_id: str
    user_id: str
    raw_input: str
    sanitized_input: str
    candidate: dict[str, Any]
    warnings: list[str]
    missing_fields: list[str]
    duplicate_candidates: list[dict[str, Any]]
    duplicate_decision: Literal["continue", "cancel"]
    record: dict[str, Any]
    reminder: dict[str, Any]
    record_confirmed: bool
    reminder_confirmed: bool
    saved_record_id: str
    saved_record: dict[str, Any]
    reminder_id: str
    saved_reminder: dict[str, Any]
    cancelled: bool
    errors: list[str]


class GraphAgent:
    """LangGraph wrapper for the create-record workflow."""

    def __init__(
        self,
        settings: Settings,
        repository: VaultRepository | None = None,
        mcp_client: PersonalVaultMcpClient | None = None,
    ):
        self.settings = settings
        self.repository = repository or VaultRepository(settings.database_path)
        self.mcp_client = mcp_client or InProcessPersonalVaultMcpClient(settings, self.repository)
        self.service = LifeVaultAgent(settings, self.repository, mcp_client=self.mcp_client)
        settings.langgraph_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint_conn = sqlite3.connect(
            settings.langgraph_checkpoint_path,
            check_same_thread=False,
        )
        self._checkpointer = SqliteSaver(self._checkpoint_conn)
        self._graph = self._build_graph()

    def start_create_record(self, text: str, thread_id: str | None = None) -> GraphTurn:
        active_thread_id = thread_id or str(uuid4())
        config = self._config(active_thread_id)
        self._graph.invoke(
            {
                "thread_id": active_thread_id,
                "user_id": self.settings.default_user_id,
                "raw_input": text,
            },
            config=config,
        )
        return self._turn_from_config(active_thread_id)

    def resume(self, thread_id: str, payload: str | dict[str, Any]) -> GraphTurn:
        config = self._config(thread_id)
        self._graph.invoke(Command(resume=payload), config=config)
        return self._turn_from_config(thread_id)

    def get_state(self, thread_id: str) -> GraphTurn | None:
        config = self._config(thread_id)
        snapshot = self._graph.get_state(config)
        if not snapshot.values and not snapshot.interrupts:
            return None
        return self._turn_from_config(thread_id)

    def close(self) -> None:
        self._checkpoint_conn.close()

    def _build_graph(self):
        builder = StateGraph(LifeVaultGraphState)
        builder.add_node("input_guard", self._input_guard_node)
        builder.add_node("extract_record", self._extract_record_node)
        builder.add_node("validate_record", self._validate_record_node)
        builder.add_node("prepare_record", self._prepare_record_node)
        builder.add_node("review_duplicate", self._review_duplicate_node)
        builder.add_node("confirm_record", self._confirm_record_node)
        builder.add_node("save_record", self._save_record_node)
        builder.add_node("confirm_reminder", self._confirm_reminder_node)
        builder.add_node("create_reminder", self._create_reminder_node)

        builder.add_edge(START, "input_guard")
        builder.add_edge("input_guard", "extract_record")
        builder.add_edge("extract_record", "validate_record")
        builder.add_conditional_edges(
            "validate_record",
            self._route_after_validate,
            {
                "validate": "validate_record",
                "prepare": "prepare_record",
                "end": END,
            },
        )
        builder.add_edge("prepare_record", "review_duplicate")
        builder.add_conditional_edges(
            "review_duplicate",
            self._route_after_duplicate,
            {
                "confirm_record": "confirm_record",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "confirm_record",
            self._route_after_record_confirmation,
            {
                "save": "save_record",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "save_record",
            self._route_after_save_record,
            {
                "confirm_reminder": "confirm_reminder",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "confirm_reminder",
            self._route_after_reminder_confirmation,
            {
                "create": "create_reminder",
                "end": END,
            },
        )
        builder.add_edge("create_reminder", END)
        return builder.compile(checkpointer=self._checkpointer)

    def _input_guard_node(self, state: LifeVaultGraphState) -> dict[str, Any]:
        return {
            "sanitized_input": sanitize_input(
                state.get("raw_input", ""),
                self.settings.input_max_chars,
            ),
            "user_id": state.get("user_id") or self.settings.default_user_id,
            "warnings": state.get("warnings", []),
            "errors": state.get("errors", []),
        }

    def _extract_record_node(self, state: LifeVaultGraphState) -> dict[str, Any]:
        now = now_in_timezone(self.settings.default_timezone)
        candidate, warnings = self.service.extractor.extract_record(state["sanitized_input"], now)
        return {
            "candidate": candidate.model_dump(mode="json"),
            "warnings": [*state.get("warnings", []), *warnings],
        }

    def _validate_record_node(self, state: LifeVaultGraphState) -> dict[str, Any]:
        candidate = ExtractedRecordCandidate.model_validate(state["candidate"])
        if candidate.intent not in {"create_record", "unknown"}:
            return {
                "cancelled": True,
                "errors": [*state.get("errors", []), "Create-record graph received a non-create intent."],
            }

        now = now_in_timezone(self.settings.default_timezone)
        missing = self.service._missing_fields(candidate, now)
        if missing:
            payload = interrupt(
                {
                    "type": "missing_fields",
                    "prompt": "请补充缺失字段。",
                    "missing_fields": missing,
                    "candidate": candidate.model_dump(mode="json"),
                }
            )
            action = _payload_action(payload)
            if action == "cancel":
                return {
                    "cancelled": True,
                    "missing_fields": missing,
                    "errors": [*state.get("errors", []), "User cancelled while filling missing fields."],
                }
            supplement_text = _payload_text(payload)
            if not supplement_text:
                return {"missing_fields": missing}
            supplement, warnings = self.service.extractor.extract_record(supplement_text, now)
            candidate = self._merge_candidate(candidate, supplement, supplement_text)
            missing = self.service._missing_fields(candidate, now)
            return {
                "candidate": candidate.model_dump(mode="json"),
                "missing_fields": missing,
                "warnings": [*state.get("warnings", []), *warnings],
            }

        return {"missing_fields": []}

    def _prepare_record_node(self, state: LifeVaultGraphState) -> dict[str, Any]:
        now = now_in_timezone(self.settings.default_timezone)
        candidate = ExtractedRecordCandidate.model_validate(state["candidate"])
        try:
            preference = self.service._get_preferences()
        except RuntimeError as exc:
            return {
                "cancelled": True,
                "errors": [*state.get("errors", []), str(exc)],
            }
        record = self.service._build_record(candidate, state["sanitized_input"], now, preference)
        duplicate_result = self.mcp_client.find_duplicate(record.model_dump(mode="json"))
        if not duplicate_result.get("ok"):
            return {
                "record": record.model_dump(mode="json"),
                "cancelled": True,
                "errors": [*state.get("errors", []), _mcp_error_message("find_duplicate", duplicate_result)],
            }
        duplicates = duplicate_result.get("duplicate_candidates", [])
        reminder = self.service._build_reminder(candidate, record, preference)
        return {
            "record": record.model_dump(mode="json"),
            "duplicate_candidates": duplicates,
            "reminder": reminder.model_dump(mode="json") if reminder else {},
        }

    def _review_duplicate_node(self, state: LifeVaultGraphState) -> dict[str, Any]:
        if state.get("cancelled"):
            return {}
        duplicates = state.get("duplicate_candidates", [])
        if not duplicates:
            return {"duplicate_decision": "continue"}

        payload = interrupt(
            {
                "type": "duplicate_review",
                "prompt": "发现疑似重复记录，请选择继续保存或取消。",
                "duplicate_candidates": duplicates,
                "record": state.get("record"),
            }
        )
        action = _payload_action(payload)
        if action == "cancel":
            return {
                "cancelled": True,
                "duplicate_decision": "cancel",
                "errors": [*state.get("errors", []), "User cancelled duplicate record."],
            }
        return {"duplicate_decision": "continue"}

    def _confirm_record_node(self, state: LifeVaultGraphState) -> dict[str, Any]:
        payload = interrupt(
            {
                "type": "record_confirmation",
                "prompt": "请确认是否保存这条记录。",
                "record": state.get("record"),
                "duplicate_candidates": state.get("duplicate_candidates", []),
            }
        )
        action = _payload_action(payload)
        if action == "confirm":
            return {"record_confirmed": True}
        return {
            "record_confirmed": False,
            "cancelled": True,
            "errors": [*state.get("errors", []), "User declined record save."],
        }

    def _save_record_node(self, state: LifeVaultGraphState) -> dict[str, Any]:
        if state.get("saved_record_id") and state.get("saved_record"):
            return {}
        record = LifeRecordCreate.model_validate(state["record"])
        record_key = stable_key(
            "record",
            self.settings.default_user_id,
            record.source_text_hash,
            record.record_type.value,
            record.title,
            record.event_date,
            record.deadline,
        )
        save_result = self.mcp_client.save_record(
            record.model_dump(mode="json"),
            idempotency_key=record_key,
            user_confirmed=bool(state.get("record_confirmed")),
        )
        if not save_result.get("ok"):
            return {
                "cancelled": True,
                "errors": [*state.get("errors", []), _mcp_error_message("save_record", save_result)],
            }
        saved_record = save_result["record"]
        return {
            "saved_record_id": saved_record["id"],
            "saved_record": saved_record,
        }

    def _confirm_reminder_node(self, state: LifeVaultGraphState) -> dict[str, Any]:
        reminder_data = state.get("reminder") or {}
        if not reminder_data:
            return {"reminder_confirmed": False}

        reminder = ReminderCreate.model_validate(reminder_data)
        reminder = reminder.model_copy(update={"record_id": state["saved_record_id"]})
        reminder_preview = reminder.model_dump(mode="json")
        payload = interrupt(
            {
                "type": "reminder_confirmation",
                "prompt": "请确认是否创建这条提醒。",
                "record": state.get("saved_record") or state.get("record"),
                "reminder": reminder_preview,
            }
        )
        action = _payload_action(payload)
        if action == "confirm":
            return {
                "reminder": reminder_preview,
                "reminder_confirmed": True,
            }
        return {
            "reminder": reminder_preview,
            "reminder_confirmed": False,
        }

    def _create_reminder_node(self, state: LifeVaultGraphState) -> dict[str, Any]:
        if state.get("reminder_id") and state.get("saved_reminder"):
            return {}
        reminder = ReminderCreate.model_validate(state["reminder"])
        reminder_key = stable_key(
            "reminder",
            self.settings.default_user_id,
            reminder.record_id,
            reminder.reminder_type.value,
            reminder.scheduled_at.isoformat(),
        )
        create_result = self.mcp_client.create_reminder(
            record_id=reminder.record_id,
            scheduled_at=reminder.scheduled_at.isoformat(),
            reminder_type=reminder.reminder_type.value,
            idempotency_key=reminder_key,
            user_confirmed=bool(state.get("reminder_confirmed")),
            message=reminder.message,
            parent_id=reminder.parent_id,
        )
        if not create_result.get("ok"):
            return {
                "errors": [*state.get("errors", []), _mcp_error_message("create_reminder", create_result)],
            }
        saved_reminder = create_result["reminder"]
        return {
            "reminder_id": saved_reminder["id"],
            "saved_reminder": saved_reminder,
        }

    def _route_after_validate(self, state: LifeVaultGraphState) -> str:
        if state.get("cancelled"):
            return "end"
        if state.get("missing_fields"):
            return "validate"
        return "prepare"

    def _route_after_duplicate(self, state: LifeVaultGraphState) -> str:
        if state.get("cancelled"):
            return "end"
        return "confirm_record"

    def _route_after_record_confirmation(self, state: LifeVaultGraphState) -> str:
        if state.get("cancelled") or not state.get("record_confirmed"):
            return "end"
        return "save"

    def _route_after_save_record(self, state: LifeVaultGraphState) -> str:
        if state.get("cancelled") or not state.get("saved_record_id"):
            return "end"
        return "confirm_reminder"

    def _route_after_reminder_confirmation(self, state: LifeVaultGraphState) -> str:
        if state.get("reminder_confirmed"):
            return "create"
        return "end"

    def _merge_candidate(
        self,
        base: ExtractedRecordCandidate,
        supplement: ExtractedRecordCandidate,
        supplement_text: str,
    ) -> ExtractedRecordCandidate:
        base_data = base.model_dump(mode="python")
        supplement_data = supplement.model_dump(mode="python")
        allow_override = any(token in supplement_text for token in ["改成", "修改", "更正", "应该是", "不是"])

        for field, value in supplement_data.items():
            if field == "tool_plan":
                merged = list(dict.fromkeys([*base_data.get("tool_plan", []), *value]))
                base_data["tool_plan"] = merged
                continue
            if not _meaningful_candidate_value(field, value, supplement_text):
                continue
            existing = base_data.get(field)
            if allow_override or _is_emptyish(existing):
                base_data[field] = value

        return ExtractedRecordCandidate.model_validate(base_data)

    def _turn_from_config(self, thread_id: str) -> GraphTurn:
        config = self._config(thread_id)
        snapshot = self._graph.get_state(config)
        state: dict[str, Any] = dict(snapshot.values or {})
        interrupt_payload: dict[str, Any] | None = None
        if snapshot.interrupts:
            raw_payload = snapshot.interrupts[0].value
            interrupt_payload = raw_payload if isinstance(raw_payload, dict) else {"value": raw_payload}

        if interrupt_payload:
            status: Literal["running", "interrupted", "completed", "cancelled"] = "interrupted"
        elif state.get("cancelled"):
            status = "cancelled"
        elif state.get("saved_record_id") or state.get("reminder_id"):
            status = "completed"
        else:
            status = "running"

        return GraphTurn(
            thread_id=thread_id,
            status=status,
            interrupt_type=interrupt_payload.get("type") if interrupt_payload else None,
            prompt=interrupt_payload.get("prompt") if interrupt_payload else None,
            interrupt_payload=interrupt_payload,
            missing_fields=(interrupt_payload or {}).get("missing_fields", state.get("missing_fields", [])),
            duplicate_candidates=(interrupt_payload or {}).get(
                "duplicate_candidates",
                state.get("duplicate_candidates", []),
            ),
            candidate=(interrupt_payload or {}).get("candidate", state.get("candidate")),
            record=(interrupt_payload or {}).get("record", state.get("saved_record") or state.get("record")),
            reminder=(interrupt_payload or {}).get("reminder", state.get("saved_reminder") or state.get("reminder")),
            saved_record_id=state.get("saved_record_id"),
            reminder_id=state.get("reminder_id"),
            errors=state.get("errors", []),
        )

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}


def _payload_action(payload: str | dict[str, Any]) -> str | None:
    if isinstance(payload, dict):
        value = payload.get("action")
        return str(value).strip().lower() if value is not None else None
    return None


def _payload_text(payload: str | dict[str, Any]) -> str:
    if isinstance(payload, dict):
        value = payload.get("text") or payload.get("message") or payload.get("value")
        return str(value).strip() if value is not None else ""
    return str(payload).strip()


def _meaningful_candidate_value(field: str, value: Any, text: str) -> bool:
    if _is_emptyish(value):
        return False
    if field == "intent":
        return value != "unknown"
    if field == "record_type":
        return any(token in text for token in ["订单", "买", "购买", "订阅", "会员", "账单", "房租", "缴费"])
    if field == "currency":
        return False
    if field == "reminder_requested":
        return bool(value) or any(token in text for token in ["不提醒", "不用提醒", "取消提醒"])
    return True


def _is_emptyish(value: Any) -> bool:
    return value is None or value == "" or value == []


def _mcp_error_message(tool_name: str, result: dict[str, Any]) -> str:
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        code = error.get("code", "unknown_error")
        message = error.get("message", "")
        return f"MCP {tool_name} failed: {code}: {message}"
    return f"MCP {tool_name} failed."
