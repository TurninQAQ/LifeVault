from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lifevault.config import Settings
from lifevault.eval.update_runner import (
    DEFAULT_UPDATE_EXAMPLES_PATH,
    load_update_cases,
    run_update_eval,
    write_update_json_report,
)


class UpdateEvalRunnerTest(unittest.TestCase):
    def test_load_and_run_fallback_update_eval(self) -> None:
        cases = load_update_cases(DEFAULT_UPDATE_EXAMPLES_PATH)
        self.assertGreaterEqual(len(cases), 20)
        report = run_update_eval(Settings(use_qwen=False))
        self.assertEqual(report["summary"]["total"], len(cases))
        self.assertEqual(report["summary"]["passed_cases"], len(cases))

    def test_invalid_update_case_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.jsonl"
            path.write_text('{"id":"bad","text":"x"}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_update_cases(path)

    def test_write_update_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = run_update_eval(Settings(use_qwen=False))
            output = Path(tmp) / "nested" / "report.json"
            write_update_json_report(report, output)
            self.assertTrue(output.exists())
            self.assertIn('"passed_cases"', output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
