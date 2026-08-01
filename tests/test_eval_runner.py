from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lifevault.config import Settings
from lifevault.eval.runner import format_summary, load_cases, run_eval, write_json_report
from lifevault.models.schemas import ExtractedRecordCandidate


class EvalRunnerTest(unittest.TestCase):
    def test_load_cases_and_run_fallback_eval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "id": "purchase_ok",
                                "text": "我昨天在京东买了一个耳机，3499 元，订单号 123456，七天无理由，退货前两天提醒我。",
                                "expected": {
                                    "intent": "create_record",
                                    "record_type": "purchase",
                                    "title": "耳机",
                                    "amount": 3499,
                                    "merchant": "京东",
                                    "order_number": "123456",
                                    "return_days": 7,
                                    "remind_before_days": 2,
                                },
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "id": "search_ok",
                                "text": "查一下耳机订单",
                                "expected": {
                                    "intent": "search_records",
                                    "record_type": "purchase",
                                    "search_query": "耳机",
                                },
                            },
                            ensure_ascii=False,
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            settings = Settings(database_path=Path(tmp) / "eval.db", use_qwen=True)

            cases = load_cases(path)
            report = run_eval(settings, examples_path=path)
            summary = format_summary(report)

            self.assertEqual(len(cases), 2)
            self.assertFalse(report["use_qwen"])
            self.assertEqual(report["summary"]["total"], 2)
            self.assertEqual(report["summary"]["intent_accuracy"], 1.0)
            self.assertIn("LifeVault extraction eval", summary)

    def test_write_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "report.json"
            report = {"summary": {"total": 0}, "cases": []}
            write_json_report(report, out)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), report)

    def test_use_qwen_flag_overrides_disabled_runtime_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "case.jsonl"
            path.write_text(
                '{"id":"qwen","text":"anything","expected":{"intent":"unknown"}}\n',
                encoding="utf-8",
            )
            settings = Settings(
                database_path=root / "eval.db",
                backup_dir=root / "custom-backups",
                use_qwen=False,
            )
            with patch("lifevault.eval.runner.Extractor") as extractor_class:
                extractor_class.return_value.extract_record.return_value = (
                    ExtractedRecordCandidate(intent="unknown"),
                    [],
                )
                report = run_eval(settings, examples_path=path, use_qwen=True)

            eval_settings = extractor_class.call_args.args[0]
            self.assertTrue(eval_settings.use_qwen)
            self.assertEqual(eval_settings.backup_dir, root / "custom-backups")
            self.assertTrue(report["use_qwen"])

    def test_invalid_case_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.jsonl"
            path.write_text('{"id":"bad","text":"missing expected"}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cases(path)


if __name__ == "__main__":
    unittest.main()
