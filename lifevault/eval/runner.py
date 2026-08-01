from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from lifevault.config import Settings
from lifevault.models.llm_factory import Extractor
from lifevault.models.schemas import ExtractedRecordCandidate


DEFAULT_EXAMPLES_PATH = Path(__file__).resolve().parent / "data" / "examples.jsonl"


@dataclass(frozen=True)
class EvalCase:
    id: str
    text: str
    expected: dict[str, Any]


def run_eval(
    settings: Settings,
    examples_path: Path = DEFAULT_EXAMPLES_PATH,
    use_qwen: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    cases = load_cases(examples_path)
    eval_settings = _settings_with_qwen(settings, use_qwen=use_qwen)
    extractor = Extractor(eval_settings)
    active_now = now or datetime.now(ZoneInfo(settings.default_timezone))

    results = []
    for case in cases:
        try:
            candidate, warnings = extractor.extract_record(case.text, active_now)
            actual = candidate.model_dump(mode="json")
            results.append(_evaluate_case(case, actual, warnings=warnings))
        except Exception as exc:
            results.append(
                {
                    "id": case.id,
                    "text": case.text,
                    "expected": case.expected,
                    "actual": None,
                    "matched_fields": [],
                    "mismatches": [{"field": "__error__", "expected": None, "actual": str(exc)}],
                    "warnings": [],
                    "passed": False,
                }
            )

    return _build_report(results, examples_path=examples_path, use_qwen=use_qwen)


def load_cases(path: Path) -> list[EvalCase]:
    if not path.exists():
        raise FileNotFoundError(f"Eval examples file not found: {path}")

    cases: list[EvalCase] = []
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
                raise ValueError(f"Eval case must be an object at {path}:{line_number}")
            case_id = payload.get("id")
            text = payload.get("text")
            expected = payload.get("expected")
            if not isinstance(case_id, str) or not case_id:
                raise ValueError(f"Eval case id is required at {path}:{line_number}")
            if not isinstance(text, str) or not text:
                raise ValueError(f"Eval case text is required at {path}:{line_number}")
            if not isinstance(expected, dict) or not expected:
                raise ValueError(f"Eval case expected object is required at {path}:{line_number}")
            cases.append(EvalCase(id=case_id, text=text, expected=expected))
    return cases


def format_summary(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "LifeVault extraction eval",
        f"examples: {report['examples_path']}",
        f"mode: {'qwen' if report['use_qwen'] else 'fallback'}",
        f"total: {summary['total']}",
        f"case accuracy: {_pct(summary['case_accuracy'])} ({summary['passed_cases']}/{summary['total']})",
        f"intent accuracy: {_pct(summary['intent_accuracy'])}",
        f"record_type accuracy: {_pct(summary['record_type_accuracy'])}",
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
        if len(failed) > 20:
            lines.append(f"- ... {len(failed) - 20} more")
    return "\n".join(lines)


def write_json_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _evaluate_case(case: EvalCase, actual: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    matched_fields: list[str] = []
    mismatches: list[dict[str, Any]] = []
    for field, expected_value in case.expected.items():
        actual_value = _normalize_value(actual.get(field))
        normalized_expected = _normalize_value(expected_value)
        if actual_value == normalized_expected:
            matched_fields.append(field)
        else:
            mismatches.append(
                {
                    "field": field,
                    "expected": normalized_expected,
                    "actual": actual_value,
                }
            )

    return {
        "id": case.id,
        "text": case.text,
        "expected": case.expected,
        "actual": actual,
        "matched_fields": matched_fields,
        "mismatches": mismatches,
        "warnings": warnings,
        "passed": not mismatches,
    }


def _build_report(results: list[dict[str, Any]], examples_path: Path, use_qwen: bool) -> dict[str, Any]:
    total = len(results)
    passed_cases = sum(1 for item in results if item["passed"])
    expected_fields = sum(len(item["expected"]) for item in results)
    matched_fields = sum(len(item["matched_fields"]) for item in results)
    intent_total = sum(1 for item in results if "intent" in item["expected"])
    intent_matched = sum(1 for item in results if "intent" in item["matched_fields"])
    record_type_total = sum(1 for item in results if "record_type" in item["expected"])
    record_type_matched = sum(1 for item in results if "record_type" in item["matched_fields"])
    summary = {
        "total": total,
        "passed_cases": passed_cases,
        "case_accuracy": _safe_ratio(passed_cases, total),
        "expected_fields": expected_fields,
        "matched_fields": matched_fields,
        "field_accuracy": _safe_ratio(matched_fields, expected_fields),
        "intent_accuracy": _safe_ratio(intent_matched, intent_total),
        "record_type_accuracy": _safe_ratio(record_type_matched, record_type_total),
    }
    return {
        "examples_path": str(examples_path),
        "use_qwen": use_qwen,
        "summary": summary,
        "cases": results,
    }


def _settings_with_qwen(settings: Settings, use_qwen: bool) -> Settings:
    return replace(settings, use_qwen=use_qwen)


def _normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return numerator / denominator


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"
