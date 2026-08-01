from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Literal, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from lifevault.agent.corrections import apply_candidate_corrections
from lifevault.agent.service import LifeVaultAgent
from lifevault.backup.locking import get_vault_lock
from lifevault.backup.runtime import RuntimeStateStore
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
    extraction_warnings: list[str]
    warnings: list[str]
    field_errors: dict[str, list[str]]
    correction_count: int
    review_action: Literal["review", "validate", "duplicate", "cancel"]
    missing_fields: list[str]
    duplicate_candidates: list[dict[str, Any]]
    duplicate_decision: Literal["continue", "cancel"]
    record: dict[str, Any]
    reminders: list[dict[str, Any]]
    reminder: dict[str, Any]
    record_confirmed: bool
    reminders_confirmed: bool
    reminder_confirmed: bool
    saved_record_id: str
    saved_record: dict[str, Any]
    reminder_ids: list[str]
    reminder_id: str
    saved_reminders: list[dict[str, Any]]
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
        self._vault_lock = get_vault_lock(settings.database_path)
        self._runtime = RuntimeStateStore(settings.database_path)
        self._generation = self._runtime.generation()
        self._open_checkpoint()

    def _open_checkpoint(self) -> None:
        self.settings.langgraph_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint_conn = sqlite3.connect(
            self.settings.langgraph_checkpoint_path,
            check_same_thread=False,
        )
        self._checkpointer = SqliteSaver(self._checkpoint_conn)
        self._graph = self._build_graph()

    def start_create_record(self, text: str, thread_id: str | None = None) -> GraphTurn:
        with self._operation():
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
        with self._operation():
            config = self._config(thread_id)
            self._graph.invoke(Command(resume=payload), config=config)
            return self._turn_from_config(thread_id)

    def get_state(self, thread_id: str) -> GraphTurn | None:
        with self._operation():
            config = self._config(thread_id)
            snapshot = self._graph.get_state(config)
            if not snapshot.values and not snapshot.interrupts:
                return None
            return self._turn_from_config(thread_id)

    def close(self) -> None:
        self._checkpoint_conn.close()

    @contextmanager
    def _operation(self):
        with self._vault_lock.acquire("shared", 3.0):
            generation = self._runtime.generation()
            if generation != self._generation:
                self._checkpoint_conn.close()
                self._generation = generation
                self._open_checkpoint()
            yield

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
        builder.add_edge("prepare_record", "confirm_record")
        builder.add_conditional_edges(
            "review_duplicate",
            self._route_after_duplicate,
            {
                "save": "save_record",
                "confirm_record": "confirm_record",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "confirm_record",
            self._route_after_record_confirmation,
            {
                "validate": "validate_record",
                "review": "confirm_record",
                "duplicate": "review_duplicate",
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
            "extraction_warnings": warnings,
            "warnings": warnings,
            "field_errors": {},
            "correction_count": 0,
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
            extraction_warnings = [
                *state.get("extraction_warnings", state.get("warnings", [])),
                *warnings,
            ]
            if not missing:
                candidate = self.service._canonicalize_candidate(candidate, now)
            return {
                "candidate": candidate.model_dump(mode="json"),
                "missing_fields": missing,
                "extraction_warnings": extraction_warnings,
                "warnings": extraction_warnings,
            }

        candidate = self.service._canonicalize_candidate(candidate, now)
        return {
            "candidate": candidate.model_dump(mode="json"),
            "missing_fields": [],
        }

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
        reminders, reminder_warnings = self.service._build_reminders(
            candidate,
            record,
            preference,
            now,
        )
        _return_deadline, _warranty_deadline, deadline_warnings = (
            self.service._purchase_deadlines(candidate, record.event_date, now)
            if record.record_type.value == "purchase"
            else (None, None, [])
        )
        reminder_payloads = [reminder.model_dump(mode="json") for reminder in reminders]
        base_warnings = (
            state["extraction_warnings"]
            if "extraction_warnings" in state
            else state.get("warnings", [])
        )
        return {
            "record": record.model_dump(mode="json"),
            "duplicate_candidates": [],
            "duplicate_decision": "cancel",
            "reminders": reminder_payloads,
            "reminder": reminder_payloads[0] if reminder_payloads else {},
            "field_errors": {},
            "record_confirmed": False,
            "warnings": [
                *base_warnings,
                *deadline_warnings,
                *reminder_warnings,
            ],
        }

    def _review_duplicate_node(self, state: LifeVaultGraphState) -> dict[str, Any]:
        if state.get("cancelled"):
            return {}
        duplicate_result = self.mcp_client.find_duplicate(state["record"])
        if not duplicate_result.get("ok"):
            return {
                "cancelled": True,
                "errors": [
                    *state.get("errors", []),
                    _mcp_error_message("find_duplicate", duplicate_result),
                ],
            }
        duplicates = duplicate_result.get("duplicate_candidates", [])
        if not duplicates:
            return {
                "duplicate_candidates": [],
                "duplicate_decision": "continue",
            }

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
                "duplicate_candidates": duplicates,
                "duplicate_decision": "cancel",
                "errors": [*state.get("errors", []), "User cancelled duplicate record."],
            }
        return {
            "duplicate_candidates": duplicates,
            "duplicate_decision": "continue",
        }

    def _confirm_record_node(self, state: LifeVaultGraphState) -> dict[str, Any]:
        payload = interrupt(
            {
                "type": "record_confirmation",
                "prompt": "请校对并确认是否保存这条记录。",
                "candidate": state.get("candidate"),
                "record": state.get("record"),
                "reminders": _state_reminders(state),
                "field_errors": state.get("field_errors", {}),
                "warnings": state.get("warnings", []),
            }
        )
        action = _payload_action(payload)
        if action in {"apply", "edit"}:
            now = now_in_timezone(self.settings.default_timezone)
            candidate = ExtractedRecordCandidate.model_validate(state["candidate"])
            result = apply_candidate_corrections(
                candidate,
                payload.get("corrections") if isinstance(payload, dict) else None,
                self.service,
                now,
            )
            if result.field_errors:
                return {
                    "review_action": "review",
                    "record_confirmed": False,
                    "field_errors": result.field_errors,
                }
            return {
                "candidate": result.candidate.model_dump(mode="json"),
                "review_action": "validate",
                "record_confirmed": False,
                "duplicate_candidates": [],
                "field_errors": {},
                "correction_count": state.get("correction_count", 0) + 1,
            }
        if action == "confirm":
            return {
                "record_confirmed": True,
                "review_action": "duplicate",
                "field_errors": {},
            }
        return {
            "review_action": "cancel",
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
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            ),
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
        reminder_data = _state_reminders(state)
        if not reminder_data:
            return {
                "reminders": [],
                "reminder": {},
                "reminders_confirmed": False,
                "reminder_confirmed": False,
            }

        reminder_previews = [
            ReminderCreate.model_validate(reminder).model_copy(
                update={"record_id": state["saved_record_id"]}
            ).model_dump(mode="json")
            for reminder in reminder_data
        ]
        payload = interrupt(
            {
                "type": "reminder_confirmation",
                "prompt": "请选择并确认要创建的提醒。",
                "record": state.get("saved_record") or state.get("record"),
                "reminders": reminder_previews,
                "reminder": reminder_previews[0],
                "warnings": state.get("warnings", []),
            }
        )
        action = _payload_action(payload)
        if action == "confirm":
            selected_keys = _payload_selected_reminder_keys(payload)
            selected_types = _payload_selected_reminder_types(payload)
            if selected_keys is not None:
                selected = [
                    reminder
                    for reminder in reminder_previews
                    if _reminder_selection_key(reminder) in selected_keys
                ]
            elif selected_types is not None:
                selected = [
                    reminder
                    for reminder in reminder_previews
                    if reminder["reminder_type"] in selected_types
                ]
            else:
                selected = reminder_previews
            return {
                "reminders": selected,
                "reminder": selected[0] if selected else {},
                "reminders_confirmed": bool(selected),
                "reminder_confirmed": bool(selected),
            }
        return {
            "reminders": [],
            "reminder": {},
            "reminders_confirmed": False,
            "reminder_confirmed": False,
        }

    def _create_reminder_node(self, state: LifeVaultGraphState) -> dict[str, Any]:
        if (
            (state.get("reminder_ids") and state.get("saved_reminders"))
            or (state.get("reminder_id") and state.get("saved_reminder"))
        ):
            return {}
        reminders = [
            ReminderCreate.model_validate(reminder)
            for reminder in _state_reminders(state)
        ]
        if not reminders:
            return {}
        batch_key = stable_key(
            "reminder-batch",
            self.settings.default_user_id,
            state["saved_record_id"],
            *[
                f"{reminder.reminder_type.value}:{reminder.scheduled_at.isoformat()}"
                for reminder in reminders
            ],
        )
        create_result = self.mcp_client.create_reminders(
            [reminder.model_dump(mode="json") for reminder in reminders],
            idempotency_key=batch_key,
            user_confirmed=bool(
                state.get("reminders_confirmed") or state.get("reminder_confirmed")
            ),
        )
        if not create_result.get("ok"):
            return {
                "errors": [
                    *state.get("errors", []),
                    _mcp_error_message("create_reminders", create_result),
                ],
            }
        saved_reminders = create_result.get("reminders", [])
        reminder_ids = [reminder["id"] for reminder in saved_reminders]
        return {
            "reminder_ids": reminder_ids,
            "reminder_id": reminder_ids[0] if reminder_ids else "",
            "saved_reminders": saved_reminders,
            "saved_reminder": saved_reminders[0] if saved_reminders else {},
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
        if state.get("record_confirmed"):
            return "save"
        return "confirm_record"

    def _route_after_record_confirmation(self, state: LifeVaultGraphState) -> str:
        if state.get("cancelled"):
            return "end"
        return state.get("review_action", "review")

    def _route_after_save_record(self, state: LifeVaultGraphState) -> str:
        if state.get("cancelled") or not state.get("saved_record_id"):
            return "end"
        return "confirm_reminder"

    def _route_after_reminder_confirmation(self, state: LifeVaultGraphState) -> str:
        if state.get("reminders_confirmed") or state.get("reminder_confirmed"):
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
        elif state.get("saved_record_id") or state.get("reminder_ids") or state.get("reminder_id"):
            status = "completed"
        else:
            status = "running"

        active_reminders = _coerce_reminder_dicts(
            (interrupt_payload or {}).get("reminders")
            or (interrupt_payload or {}).get("reminder")
            or state.get("saved_reminders")
            or state.get("saved_reminder")
            or state.get("reminders")
            or state.get("reminder")
        )
        active_reminder_ids = list(state.get("reminder_ids") or [])
        if not active_reminder_ids and state.get("reminder_id"):
            active_reminder_ids = [state["reminder_id"]]

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
            reminders=active_reminders,
            reminder=active_reminders[0] if active_reminders else None,
            saved_record_id=state.get("saved_record_id"),
            reminder_ids=active_reminder_ids,
            reminder_id=active_reminder_ids[0] if active_reminder_ids else None,
            field_errors=(interrupt_payload or {}).get(
                "field_errors",
                state.get("field_errors", {}),
            ),
            warnings=(interrupt_payload or {}).get("warnings", state.get("warnings", [])),
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


def _payload_selected_reminder_types(
    payload: str | dict[str, Any],
) -> set[str] | None:
    if not isinstance(payload, dict) or "selected_reminder_types" not in payload:
        return None
    value = payload.get("selected_reminder_types")
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value}


def _payload_selected_reminder_keys(
    payload: str | dict[str, Any],
) -> set[str] | None:
    if not isinstance(payload, dict) or "selected_reminder_keys" not in payload:
        return None
    value = payload.get("selected_reminder_keys")
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value}


def _reminder_selection_key(reminder: dict[str, Any]) -> str:
    return f"{reminder.get('reminder_type', '')}|{reminder.get('scheduled_at', '')}"


def _state_reminders(state: LifeVaultGraphState) -> list[dict[str, Any]]:
    return _coerce_reminder_dicts(state.get("reminders") or state.get("reminder"))


def _coerce_reminder_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict) and item]
    return []


def _meaningful_candidate_value(field: str, value: Any, text: str) -> bool:
    if _is_emptyish(value):
        return False
    if field == "intent":
        return value != "unknown"
    if field == "record_type":
        return any(token in text for token in ["订单", "买", "购买", "订阅", "会员", "账单", "房租", "缴费"])
    if field == "currency":
        return False
    if field in {
        "reminder_requested",
        "return_reminder_requested",
        "warranty_reminder_requested",
    }:
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
