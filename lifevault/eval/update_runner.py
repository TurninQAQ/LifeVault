from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from lifevault.agent.update_intent import build_record_update_changes
from lifevault.config import Settings
from lifevault.models.schemas import RecordType
from lifevault.models.update_extractor import UpdateExtractor


DEFAULT_UPDATE_EXAMPLES_PATH = Path(__file__).resolve().parent / "data" / "update_examples.jsonl"
DEFAULT_UPDATE_EVAL_NOW = datetime(
    2026,
    7,
    31,
    10,
    0,
    tzinfo=ZoneInfo("Asia/Shanghai"),
)


@dataclass(frozen=True)
class UpdateEvalCase:
    id: str
    text: str
    selected_record_type: RecordType
    expected: dict[str, Any]


def run_update_eval(
    settings: Settings,
    examples_path: Path = DEFAULT_UPDATE_EXAMPLES_PATH,
    use_qwen: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    cases = load_update_cases(examples_path)
    eval_settings = _settings_with_qwen(settings, use_qwen)
    extractor = UpdateExtractor(eval_settings)
    active_now = now or DEFAULT_UPDATE_EVAL_NOW
    results: list[dict[str, Any]] = []

    for case in cases:
        try:
            target, target_warnings = extractor.extract_target(case.text, active_now)
            update_warnings: list[str] = []
            if target.operation in {"archive_record", "restore_record"}:
                update_actual = {
                    "operation": target.operation,
                    "changes": {},
                    "target_status": None,
                    "clear_fields": [],
                    "date_sources": {},
                    "field_errors": {},
                }
            else:
                intent, update_warnings = extractor.extract_update(
                    case.text,
                    case.selected_record_type,
                    active_now,
                )
                changes, date_sources, field_errors = build_record_update_changes(
                    intent,
                    case.selected_record_type,
                    settings.default_timezone,
                    active_now,
                )
                update_actual = {
                    "operation": intent.operation,
                    "changes": changes,
                    "target_status": intent.target_status.value if intent.target_status else None,
                    "clear_fields": intent.clear_fields,
                    "date_sources": date_sources,
                    "field_errors": field_errors,
                }
            actual = {
                "target": target.model_dump(mode="json"),
                "update": update_actual,
            }
            results.append(
                _evaluate_update_case(
                    case,
                    actual,
                    [*target_warnings, *update_warnings],
                )
            )
        except Exception as exc:
            results.append(
                {
                    "id": case.id,
                    "text": case.text,
                    "expected": case.expected,
                    "actual": None,
                    "matched_fields": [],
                    "mismatches": [
                        {"field": "__error__", "expected": None, "actual": str(exc)}
                    ],
                    "warnings": [],
                    "passed": False,
                }
            )

    total = len(results)
    passed = sum(item["passed"] for item in results)
    expected_fields = sum(len(_flatten(item["expected"])) for item in results)
    matched_fields = sum(len(item["matched_fields"]) for item in results)
    return {
        "examples_path": str(examples_path),
        "use_qwen": use_qwen,
        "now": active_now.isoformat(),
        "summary": {
            "total": total,
            "passed_cases": passed,
            "case_accuracy": _safe_ratio(passed, total),
            "expected_fields": expected_fields,
            "matched_fields": matched_fields,
            "field_accuracy": _safe_ratio(matched_fields, expected_fields),
        },
        "cases": results,
    }


def load_update_cases(path: Path) -> list[UpdateEvalCase]:
    if not path.exists():
        raise FileNotFoundError(f"Update eval examples file not found: {path}")
    cases: list[UpdateEvalCase] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"Update eval case must be an object at {path}:{line_number}")
            try:
                case_id = payload["id"]
                text = payload["text"]
                selected_type = RecordType(payload["selected_record_type"])
                expected = payload["expected"]
            except (KeyError, ValueError) as exc:
                raise ValueError(f"Invalid update eval case at {path}:{line_number}") from exc
            if not isinstance(case_id, str) or not case_id:
                raise ValueError(f"Update eval case id is required at {path}:{line_number}")
            if not isinstance(text, str) or not text:
                raise ValueError(f"Update eval text is required at {path}:{line_number}")
            if not isinstance(expected, dict) or not expected:
                raise ValueError(f"Update eval expected object is required at {path}:{line_number}")
            cases.append(UpdateEvalCase(case_id, text, selected_type, expected))
    return cases


def format_update_summary(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "LifeVault natural update eval",
        f"examples: {report['examples_path']}",
        f"mode: {'qwen' if report['use_qwen'] else 'fallback'}",
        f"now: {report['now']}",
        f"total: {summary['total']}",
        f"case accuracy: {_pct(summary['case_accuracy'])} ({summary['passed_cases']}/{summary['total']})",
        f"field accuracy: {_pct(summary['field_accuracy'])} ({summary['matched_fields']}/{summary['expected_fields']})",
    ]
    failed = [item for item in report["cases"] if not item["passed"]]
    if failed:
        lines.append("failed cases:")
        for item in failed[:20]:
            mismatch_text = "; ".join(
                f"{mismatch['field']}: expected={mismatch['expected']!r}, actual={mismatch['actual']!r}"
                for mismatch in item["mismatches"][:4]
            )
            lines.append(f"- {item['id']}: {mismatch_text}")
    return "\n".join(lines)


def write_update_json_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _evaluate_update_case(
    case: UpdateEvalCase,
    actual: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    expected_fields = _flatten(case.expected)
    actual_fields = _flatten(actual)
    matched: list[str] = []
    mismatches: list[dict[str, Any]] = []
    for field, expected in expected_fields.items():
        actual_value = actual_fields.get(field)
        if _normalize(expected) == _normalize(actual_value):
            matched.append(field)
        else:
            mismatches.append(
                {
                    "field": field,
                    "expected": expected,
                    "actual": actual_value,
                }
            )
    return {
        "id": case.id,
        "text": case.text,
        "expected": case.expected,
        "actual": actual,
        "matched_fields": matched,
        "mismatches": mismatches,
        "warnings": warnings,
        "passed": not mismatches,
    }


def _flatten(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            flattened.update(_flatten(item, path))
        else:
            flattened[path] = item
    return flattened


def _settings_with_qwen(settings: Settings, use_qwen: bool) -> Settings:
    return replace(settings, use_qwen=use_qwen)


def _normalize(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _safe_ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"
