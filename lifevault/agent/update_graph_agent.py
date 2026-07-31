from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Literal, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from lifevault.agent.update_intent import (
    build_record_update_changes,
    target_date_range,
    validate_structured_changes,
)
from lifevault.config import Settings
from lifevault.hooks.privacy_hooks import sanitize_input
from lifevault.mcp_server.client import (
    InProcessPersonalVaultMcpClient,
    PersonalVaultMcpClient,
)
from lifevault.models.schemas import (
    NaturalRecordUpdateIntent,
    RecordStatus,
    RecordTargetQuery,
    RecordType,
    RecordUpdateTurn,
)
from lifevault.models.update_extractor import UpdateExtractor
from lifevault.records.update_planner import ALLOWED_STATUSES_BY_TYPE
from lifevault.storage.repository import VaultRepository
from lifevault.tools.date_tools import now_in_timezone
from lifevault.tools.idempotency import stable_key


class RecordUpdateGraphState(TypedDict, total=False):
    thread_id: str
    sanitized_input: str
    preselected_record_id: str
    target_query: dict[str, Any]
    candidate_ids: list[str]
    selected_record_id: str
    selected_record_type: str
    selected_version: int
    selected_summary: dict[str, Any]
    operation: Literal[
        "content_update", "status_update", "archive_record", "restore_record"
    ]
    changes: dict[str, Any]
    target_status: str
    frozen_at: str
    date_sources: dict[str, str]
    preview_summary: dict[str, Any]
    duplicate_candidates: list[dict[str, Any]]
    field_errors: dict[str, list[str]]
    warnings: list[str]
    errors: list[str]
    stage: str
    confirmed: bool
    correction_requested: bool
    cancelled: bool
    no_changes: bool
    updated_record_id: str


class RecordUpdateGraphAgent:
    """Independent LangGraph workflow for natural-language persisted-record updates."""

    def __init__(
        self,
        settings: Settings,
        repository: VaultRepository | None = None,
        mcp_client: PersonalVaultMcpClient | None = None,
    ):
        self.settings = settings
        self.repository = repository or VaultRepository(settings.database_path)
        self.mcp_client = mcp_client or InProcessPersonalVaultMcpClient(
            settings,
            self.repository,
        )
        self.extractor = UpdateExtractor(settings)
        settings.langgraph_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint_conn = sqlite3.connect(
            settings.langgraph_checkpoint_path,
            check_same_thread=False,
        )
        self._checkpointer = SqliteSaver(self._checkpoint_conn)
        self._graph = self._build_graph()

    def start(
        self,
        text: str,
        *,
        preselected_record_id: str | None = None,
        thread_id: str | None = None,
    ) -> RecordUpdateTurn:
        active_thread_id = thread_id or f"edit-{uuid4()}"
        initial: RecordUpdateGraphState = {
            "thread_id": active_thread_id,
            "sanitized_input": sanitize_input(text, self.settings.input_max_chars),
            "warnings": [],
            "errors": [],
            "field_errors": {},
            "stage": "target_extraction",
        }
        if preselected_record_id:
            initial["preselected_record_id"] = preselected_record_id
        self._graph.invoke(initial, config=self._config(active_thread_id))
        return self._turn_from_config(active_thread_id)

    def resume(self, thread_id: str, payload: str | dict[str, Any]) -> RecordUpdateTurn:
        self._graph.invoke(Command(resume=payload), config=self._config(thread_id))
        return self._turn_from_config(thread_id)

    def get_state(self, thread_id: str) -> RecordUpdateTurn | None:
        snapshot = self._graph.get_state(self._config(thread_id))
        if not snapshot.values and not snapshot.interrupts:
            return None
        return self._turn_from_config(thread_id)

    def close(self) -> None:
        self._checkpoint_conn.close()

    def _build_graph(self):
        builder = StateGraph(RecordUpdateGraphState)
        builder.add_node("extract_target", self._extract_target_node)
        builder.add_node("search_target", self._search_target_node)
        builder.add_node("select_target", self._select_target_node)
        builder.add_node("extract_update", self._extract_update_node)
        builder.add_node("collect_update", self._collect_update_node)
        builder.add_node("preview_update", self._preview_update_node)
        builder.add_node("confirm_update", self._confirm_update_node)
        builder.add_node("apply_update", self._apply_update_node)

        builder.add_edge(START, "extract_target")
        builder.add_conditional_edges(
            "extract_target",
            self._route_after_target,
            {"search": "search_target", "end": END},
        )
        builder.add_conditional_edges(
            "search_target",
            self._route_after_search,
            {"select": "select_target", "extract": "extract_update", "end": END},
        )
        builder.add_conditional_edges(
            "select_target",
            self._route_after_selection,
            {"extract": "extract_update", "end": END},
        )
        builder.add_conditional_edges(
            "extract_update",
            self._route_after_extraction,
            {"collect": "collect_update", "preview": "preview_update", "end": END},
        )
        builder.add_conditional_edges(
            "collect_update",
            self._route_after_collection,
            {"preview": "preview_update", "end": END},
        )
        builder.add_conditional_edges(
            "preview_update",
            self._route_after_preview,
            {"confirm": "confirm_update", "end": END},
        )
        builder.add_conditional_edges(
            "confirm_update",
            self._route_after_confirmation,
            {
                "confirm": "confirm_update",
                "preview": "preview_update",
                "apply": "apply_update",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "apply_update",
            self._route_after_apply,
            {"preview": "preview_update", "end": END},
        )
        return builder.compile(checkpointer=self._checkpointer)

    def _extract_target_node(self, state: RecordUpdateGraphState) -> dict[str, Any]:
        now = now_in_timezone(self.settings.default_timezone)
        text = state["sanitized_input"]
        target, warnings = self.extractor.extract_target(text, now)
        lifecycle_operation = (
            target.operation
            if target.operation in {"archive_record", "restore_record"}
            else None
        )
        lifecycle_ambiguous = _requires_lifecycle_clarification(text, target)
        while (
            not state.get("preselected_record_id") and not _has_target_clue(target)
        ) or lifecycle_ambiguous:
            payload = interrupt(
                {
                    "type": "missing_target",
                    "prompt": (
                        "请明确是恢复归档记录、取消归档，还是其他修改，并写出记录名称。"
                        if lifecycle_ambiguous
                        else "请补充要修改的记录名称、类型或日期。"
                    ),
                }
            )
            if _payload_action(payload) == "cancel":
                return _cancelled(state, "User cancelled target identification.")
            supplement = sanitize_input(_payload_text(payload), self.settings.input_max_chars)
            if not supplement:
                continue
            target, extra_warnings = self.extractor.extract_target(supplement, now)
            target, lifecycle_operation = _preserve_lifecycle_operation(
                target,
                lifecycle_operation,
            )
            warnings.extend(extra_warnings)
            lifecycle_ambiguous = (
                lifecycle_ambiguous and target.operation == "unknown"
            ) or _requires_lifecycle_clarification(supplement, target)

        if target.operation == "external_action":
            return {
                "cancelled": True,
                "stage": "refused_external_action",
                "errors": [
                    *state.get("errors", []),
                    "LifeVault can record a completed status change, but cannot make payments, refunds, or cancel an external subscription.",
                ],
                "warnings": [*state.get("warnings", []), *warnings],
            }
        return {
            "target_query": target.model_dump(mode="json"),
            "warnings": [*state.get("warnings", []), *warnings],
            "stage": "target_search",
        }

    def _search_target_node(self, state: RecordUpdateGraphState) -> dict[str, Any]:
        preselected = state.get("preselected_record_id")
        if preselected:
            result = self.mcp_client.get_record(preselected)
            if not result.get("ok"):
                return _mcp_cancelled(state, "get_record", result)
            record = result["record"]
            return _selected_record_state(record, stage="update_extraction")

        now = now_in_timezone(self.settings.default_timezone)
        target = RecordTargetQuery.model_validate(state["target_query"])
        lifecycle_operation = (
            target.operation
            if target.operation in {"archive_record", "restore_record"}
            else None
        )
        while True:
            date_from, date_to = target_date_range(
                target.target_date_text,
                self.settings.default_timezone,
                now,
            )
            result = self.mcp_client.search_records(
                query=target.query,
                record_types=[target.record_type.value] if target.record_type else None,
                date_from=date_from,
                date_to=date_to,
                limit=10,
                archive_scope=(
                    "archived" if target.operation == "restore_record" else "active"
                ),
            )
            if not result.get("ok"):
                return _mcp_cancelled(state, "search_records", result)
            records = result.get("records") or []
            if records:
                return {
                    "target_query": target.model_dump(mode="json"),
                    "candidate_ids": [str(record["id"]) for record in records],
                    "stage": "target_selection",
                }
            payload = interrupt(
                {
                    "type": "target_not_found",
                    "prompt": "没有找到匹配记录，请补充其他名称、类型或日期。",
                    "target_query": target.model_dump(mode="json"),
                }
            )
            if _payload_action(payload) == "cancel":
                return _cancelled(state, "User cancelled after no matching record was found.")
            supplement = sanitize_input(_payload_text(payload), self.settings.input_max_chars)
            if not supplement:
                continue
            target, warnings = self.extractor.extract_target(supplement, now)
            target, lifecycle_operation = _preserve_lifecycle_operation(
                target,
                lifecycle_operation,
            )
            if target.operation == "external_action":
                return _cancelled(state, "External actions are not supported.")
            if warnings:
                state["warnings"] = [*state.get("warnings", []), *warnings]

    def _select_target_node(self, state: RecordUpdateGraphState) -> dict[str, Any]:
        candidate_ids = state.get("candidate_ids", [])
        while True:
            candidates = self._candidate_summaries(candidate_ids)
            payload = interrupt(
                {
                    "type": "target_selection",
                    "prompt": "请选择要修改的记录。",
                    "candidates": candidates,
                }
            )
            if _payload_action(payload) == "cancel":
                return _cancelled(state, "User cancelled target selection.")
            record_id = _payload_record_id(payload)
            if record_id not in candidate_ids:
                continue
            result = self.mcp_client.get_record(record_id)
            if not result.get("ok"):
                continue
            return _selected_record_state(result["record"], stage="update_extraction")

    def _extract_update_node(self, state: RecordUpdateGraphState) -> dict[str, Any]:
        frozen_at = now_in_timezone(self.settings.default_timezone)
        target = RecordTargetQuery.model_validate(state["target_query"])
        if target.operation in {"archive_record", "restore_record"}:
            return {
                "operation": target.operation,
                "changes": {},
                "frozen_at": frozen_at.isoformat(),
                "field_errors": {},
                "stage": "update_preview",
            }
        record_type = RecordType(state["selected_record_type"])
        intent, warnings = self.extractor.extract_update(
            state["sanitized_input"],
            record_type,
            frozen_at,
        )
        return self._intent_state(state, intent, frozen_at, warnings)

    def _collect_update_node(self, state: RecordUpdateGraphState) -> dict[str, Any]:
        record_type = RecordType(state["selected_record_type"])
        frozen_at = datetime.fromisoformat(state["frozen_at"])
        while True:
            payload = interrupt(
                {
                    "type": "missing_update_details",
                    "prompt": "请补充要设置的新值，或明确要清空的字段。",
                    "record": state.get("selected_summary", {}),
                    "field_errors": state.get("field_errors", {}),
                }
            )
            if _payload_action(payload) == "cancel":
                return _cancelled(state, "User cancelled while supplying update details.")
            supplement = sanitize_input(_payload_text(payload), self.settings.input_max_chars)
            if not supplement:
                continue
            intent, warnings = self.extractor.extract_update(
                supplement,
                record_type,
                frozen_at,
            )
            result = self._intent_state(state, intent, frozen_at, warnings)
            if result.get("cancelled") or result.get("changes") or result.get("target_status"):
                return result

    def _intent_state(
        self,
        state: RecordUpdateGraphState,
        intent: NaturalRecordUpdateIntent,
        frozen_at: datetime,
        warnings: list[str],
    ) -> dict[str, Any]:
        base = {
            "frozen_at": frozen_at.isoformat(),
            "warnings": [*state.get("warnings", []), *warnings],
            "field_errors": {},
        }
        if intent.operation == "external_action":
            return {
                **base,
                "cancelled": True,
                "stage": "refused_external_action",
                "errors": [
                    *state.get("errors", []),
                    "LifeVault cannot make payments, refunds, or cancel an external subscription.",
                ],
            }

        record_type = RecordType(state["selected_record_type"])
        changes, date_sources, field_errors = build_record_update_changes(
            intent,
            record_type,
            self.settings.default_timezone,
            frozen_at,
        )
        if intent.target_status is not None and changes:
            return {
                **base,
                "cancelled": True,
                "stage": "mixed_update_rejected",
                "errors": [
                    *state.get("errors", []),
                    "Content and status updates must be submitted separately.",
                ],
            }
        if intent.target_status is not None:
            if intent.target_status not in ALLOWED_STATUSES_BY_TYPE[record_type]:
                return {
                    **base,
                    "field_errors": {
                        "target_status": [
                            f"{intent.target_status.value} is not valid for {record_type.value} records."
                        ]
                    },
                    "stage": "collect_update",
                }
            return {
                **base,
                "operation": "status_update",
                "target_status": intent.target_status.value,
                "changes": {},
                "date_sources": {},
                "stage": "update_preview",
            }
        return {
            **base,
            "operation": "content_update",
            "changes": changes,
            "date_sources": date_sources,
            "field_errors": field_errors,
            "stage": "update_preview" if changes and not field_errors else "collect_update",
        }

    def _preview_update_node(self, state: RecordUpdateGraphState) -> dict[str, Any]:
        current_result = self.mcp_client.get_record(state["selected_record_id"])
        if not current_result.get("ok"):
            return _mcp_cancelled(state, "get_record", current_result)
        current = current_result["record"]
        version = int(current["version"])
        operation = state["operation"]
        if operation == "archive_record":
            result = self.mcp_client.preview_record_archive(
                state["selected_record_id"],
                version,
            )
        elif operation == "restore_record":
            result = self.mcp_client.preview_record_restore(
                state["selected_record_id"],
                version,
            )
        elif operation == "status_update":
            result = self.mcp_client.preview_record_status_update(
                state["selected_record_id"],
                state["target_status"],
                version,
            )
        else:
            result = self.mcp_client.preview_record_update(
                state["selected_record_id"],
                state.get("changes", {}),
                version,
            )
        if not result.get("ok"):
            error = result.get("error") or {}
            if error.get("code") == "no_changes":
                return {
                    "selected_version": version,
                    "selected_summary": _record_summary(current),
                    "updated_record_id": state["selected_record_id"],
                    "no_changes": True,
                    "stage": "completed",
                }
            if error.get("field_errors"):
                return {
                    "selected_version": version,
                    "selected_summary": _record_summary(current),
                    "preview_summary": {},
                    "duplicate_candidates": error.get("duplicate_candidates") or [],
                    "field_errors": error["field_errors"],
                    "confirmed": False,
                    "correction_requested": False,
                    "stage": "update_confirmation",
                }
            return {
                "cancelled": True,
                "stage": "preview_failed",
                "field_errors": error.get("field_errors") or {},
                "errors": [*state.get("errors", []), _mcp_error("preview", result)],
            }

        preview_summary = _preview_summary(result, operation)
        return {
            "selected_version": version,
            "selected_summary": _record_summary(current),
            "preview_summary": preview_summary,
            "duplicate_candidates": result.get("duplicate_candidates") or [],
            "field_errors": {},
            "confirmed": False,
            "correction_requested": False,
            "stage": "update_confirmation",
        }

    def _confirm_update_node(self, state: RecordUpdateGraphState) -> dict[str, Any]:
        payload = interrupt(
            {
                "type": "update_confirmation",
                "prompt": "请核对目标和修改内容后确认。",
                "record": state.get("selected_summary", {}),
                "operation": state.get("operation"),
                "changes": state.get("changes", {}),
                "target_status": state.get("target_status"),
                "date_sources": state.get("date_sources", {}),
                "preview": state.get("preview_summary", {}),
                "recoverable": state.get("operation") == "archive_record",
                "duplicate_candidates": state.get("duplicate_candidates", []),
                "field_errors": state.get("field_errors", {}),
            }
        )
        action = _payload_action(payload)
        if action == "cancel":
            return _cancelled(state, "User cancelled the record update.")
        if action == "confirm":
            return {"confirmed": True, "stage": "applying_update"}
        if action == "apply" and isinstance(payload, dict):
            if state.get("operation") in {"archive_record", "restore_record"}:
                return {
                    "field_errors": {"action": ["Lifecycle actions can only be confirmed or cancelled."]},
                    "correction_requested": True,
                    "stage": "update_confirmation",
                }
            if state.get("operation") == "status_update":
                try:
                    status = RecordStatus(str(payload.get("target_status", "")))
                except ValueError:
                    return {
                        "field_errors": {"target_status": ["Invalid record status."]},
                        "correction_requested": True,
                        "stage": "update_confirmation",
                    }
                record_type = RecordType(state["selected_record_type"])
                if status not in ALLOWED_STATUSES_BY_TYPE[record_type]:
                    return {
                        "field_errors": {
                            "target_status": [
                                f"{status.value} is not valid for {record_type.value} records."
                            ]
                        },
                        "correction_requested": True,
                        "stage": "update_confirmation",
                    }
                return {
                    "target_status": status.value,
                    "field_errors": {},
                    "correction_requested": True,
                    "stage": "update_preview",
                }
            raw_changes = payload.get("changes")
            if not isinstance(raw_changes, dict):
                return {
                    "field_errors": {"changes": ["A changes object is required."]},
                    "correction_requested": True,
                    "stage": "update_confirmation",
                }
            changes, errors = validate_structured_changes(
                raw_changes,
                RecordType(state["selected_record_type"]),
            )
            if errors:
                return {
                    "field_errors": errors,
                    "correction_requested": True,
                    "stage": "update_confirmation",
                }
            return {
                "changes": changes,
                "date_sources": {},
                "field_errors": {},
                "correction_requested": True,
                "stage": "update_preview",
            }
        return {
            "field_errors": {"action": ["Choose confirm, apply, or cancel."]},
            "correction_requested": True,
            "stage": "update_confirmation",
        }

    def _apply_update_node(self, state: RecordUpdateGraphState) -> dict[str, Any]:
        version = state["selected_version"]
        record_id = state["selected_record_id"]
        operation = state["operation"]
        payload_key = state.get("target_status") or json.dumps(
            state.get("changes", {}),
            ensure_ascii=False,
            sort_keys=True,
        )
        idempotency_key = stable_key(
            "natural-record-update",
            state["thread_id"],
            record_id,
            version,
            operation,
            payload_key,
        )
        if operation == "archive_record":
            result = self.mcp_client.archive_record(
                record_id,
                version,
                idempotency_key,
                user_confirmed=True,
            )
        elif operation == "restore_record":
            result = self.mcp_client.restore_record(
                record_id,
                version,
                idempotency_key,
                user_confirmed=True,
            )
        elif operation == "status_update":
            result = self.mcp_client.update_record_status(
                record_id,
                state["target_status"],
                version,
                idempotency_key,
                user_confirmed=True,
            )
        else:
            result = self.mcp_client.update_record(
                record_id,
                state.get("changes", {}),
                version,
                idempotency_key,
                user_confirmed=True,
                duplicate_confirmed=bool(state.get("duplicate_candidates")),
            )
        if result.get("ok"):
            return {
                "updated_record_id": record_id,
                "selected_version": int(result["record"]["version"]),
                "selected_summary": _record_summary(result["record"]),
                "stage": "completed",
            }
        error = result.get("error") or {}
        if error.get("code") == "no_changes":
            return {
                "updated_record_id": record_id,
                "no_changes": True,
                "stage": "completed",
            }
        if error.get("code") == "version_conflict":
            current = error.get("current_record")
            if not isinstance(current, dict):
                current_result = self.mcp_client.get_record(record_id)
                current = current_result.get("record") if current_result.get("ok") else None
            if isinstance(current, dict):
                return {
                    "selected_version": int(current["version"]),
                    "selected_summary": _record_summary(current),
                    "confirmed": False,
                    "warnings": [
                        *state.get("warnings", []),
                        "The record changed after preview; review the refreshed preview again.",
                    ],
                    "stage": "update_preview",
                }
        return {
            "cancelled": True,
            "stage": "update_failed",
            "field_errors": error.get("field_errors") or {},
            "errors": [*state.get("errors", []), _mcp_error("update", result)],
        }

    def _candidate_summaries(self, candidate_ids: list[str]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for record_id in candidate_ids:
            result = self.mcp_client.get_record(record_id)
            if result.get("ok"):
                candidates.append(_record_summary(result["record"]))
        return candidates

    def _turn_from_config(self, thread_id: str) -> RecordUpdateTurn:
        snapshot = self._graph.get_state(self._config(thread_id))
        state: dict[str, Any] = dict(snapshot.values or {})
        interrupt_payload: dict[str, Any] | None = None
        if snapshot.interrupts:
            raw = snapshot.interrupts[0].value
            interrupt_payload = raw if isinstance(raw, dict) else {"value": raw}

        if interrupt_payload:
            status: Literal["running", "interrupted", "completed", "cancelled"] = "interrupted"
        elif state.get("cancelled"):
            status = "cancelled"
        elif state.get("updated_record_id") or state.get("no_changes"):
            status = "completed"
        else:
            status = "running"

        record: dict[str, Any] | None = None
        if state.get("selected_record_id"):
            result = self.mcp_client.get_record(state["selected_record_id"])
            if result.get("ok"):
                record = result["record"]

        candidates = (interrupt_payload or {}).get("candidates") or []
        return RecordUpdateTurn(
            thread_id=thread_id,
            status=status,
            interrupt_type=(interrupt_payload or {}).get("type"),
            prompt=(interrupt_payload or {}).get("prompt"),
            interrupt_payload=interrupt_payload,
            target_query=state.get("target_query"),
            candidates=candidates,
            selected_record_id=state.get("selected_record_id"),
            record=record,
            changes=state.get("changes", {}),
            operation=state.get("operation"),
            target_status=state.get("target_status"),
            preview=state.get("preview_summary"),
            updated_record_id=state.get("updated_record_id"),
            no_changes=bool(state.get("no_changes")),
            field_errors=(interrupt_payload or {}).get(
                "field_errors",
                state.get("field_errors", {}),
            ),
            warnings=state.get("warnings", []),
            errors=state.get("errors", []),
        )

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _route_after_target(state: RecordUpdateGraphState) -> str:
        return "end" if state.get("cancelled") else "search"

    @staticmethod
    def _route_after_search(state: RecordUpdateGraphState) -> str:
        if state.get("cancelled"):
            return "end"
        return "extract" if state.get("selected_record_id") else "select"

    @staticmethod
    def _route_after_selection(state: RecordUpdateGraphState) -> str:
        return "end" if state.get("cancelled") else "extract"

    @staticmethod
    def _route_after_extraction(state: RecordUpdateGraphState) -> str:
        if state.get("cancelled"):
            return "end"
        if state.get("stage") == "collect_update":
            return "collect"
        return "preview"

    @staticmethod
    def _route_after_collection(state: RecordUpdateGraphState) -> str:
        return "end" if state.get("cancelled") else "preview"

    @staticmethod
    def _route_after_preview(state: RecordUpdateGraphState) -> str:
        return "end" if state.get("cancelled") or state.get("no_changes") else "confirm"

    @staticmethod
    def _route_after_confirmation(state: RecordUpdateGraphState) -> str:
        if state.get("cancelled"):
            return "end"
        if state.get("confirmed"):
            return "apply"
        if state.get("stage") == "update_preview":
            return "preview"
        if state.get("stage") == "update_confirmation":
            return "confirm"
        return "end"

    @staticmethod
    def _route_after_apply(state: RecordUpdateGraphState) -> str:
        if state.get("stage") == "update_preview":
            return "preview"
        return "end"


def _has_target_clue(target: RecordTargetQuery) -> bool:
    return bool(target.query or target.record_type or target.target_date_text)


def _requires_lifecycle_clarification(
    text: str,
    target: RecordTargetQuery,
) -> bool:
    return target.operation == "unknown" and bool(
        re.search(r"(?:恢复|找回|取消归档|归档|删除|删掉|移除)", text)
    )


def _preserve_lifecycle_operation(
    target: RecordTargetQuery,
    previous_operation: str | None,
) -> tuple[RecordTargetQuery, str | None]:
    if target.operation in {"archive_record", "restore_record"}:
        return target, target.operation
    if (
        target.operation == "unknown"
        and previous_operation in {"archive_record", "restore_record"}
    ):
        return target.model_copy(update={"operation": previous_operation}), previous_operation
    return target, previous_operation


def _selected_record_state(record: dict[str, Any], stage: str) -> dict[str, Any]:
    return {
        "selected_record_id": str(record["id"]),
        "selected_record_type": str(record["record_type"]),
        "selected_version": int(record["version"]),
        "selected_summary": _record_summary(record),
        "stage": stage,
    }


def _record_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "record_type": record.get("record_type"),
        "title": record.get("title"),
        "status": record.get("status"),
        "amount": record.get("amount"),
        "currency": record.get("currency"),
        "event_date": record.get("event_date"),
        "deadline": record.get("deadline"),
        "version": record.get("version"),
        "archived_at": record.get("archived_at"),
    }


def _preview_summary(result: dict[str, Any], operation: str) -> dict[str, Any]:
    record = result.get("record") or {}
    summary: dict[str, Any] = {
        "record": _record_summary(record),
        "cancelled_reminder_count": len(result.get("reminders_to_cancel") or []),
        "warnings": result.get("warnings") or [],
    }
    if operation == "content_update":
        summary.update(
            {
                "changed_fields": result.get("changed_fields") or [],
                "created_reminder_count": len(result.get("reminders_to_create") or []),
            }
        )
    elif operation in {"archive_record", "restore_record"}:
        summary["recoverable"] = bool(result.get("recoverable", True))
        summary["operation"] = operation
    return summary


def _payload_action(payload: str | dict[str, Any]) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("action")
    return str(value).strip().lower() if value is not None else None


def _payload_text(payload: str | dict[str, Any]) -> str:
    if isinstance(payload, dict):
        value = payload.get("text") or payload.get("message") or payload.get("value")
        return str(value).strip() if value is not None else ""
    return str(payload).strip()


def _payload_record_id(payload: str | dict[str, Any]) -> str:
    if isinstance(payload, dict):
        return str(payload.get("record_id") or payload.get("selected_record_id") or "")
    return str(payload).strip()


def _cancelled(state: RecordUpdateGraphState, message: str) -> dict[str, Any]:
    return {
        "cancelled": True,
        "stage": "cancelled",
        "errors": [*state.get("errors", []), message],
    }


def _mcp_cancelled(
    state: RecordUpdateGraphState,
    tool_name: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    return _cancelled(state, _mcp_error(tool_name, result))


def _mcp_error(tool_name: str, result: dict[str, Any]) -> str:
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        return f"MCP {tool_name} failed: {error.get('code', 'unknown_error')}: {error.get('message', '')}"
    return f"MCP {tool_name} failed."
