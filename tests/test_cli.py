from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lifevault.agent.graph_agent import GraphAgent
from lifevault.config import Settings
from lifevault.storage.repository import VaultRepository


class CliCorrectionTest(unittest.TestCase):
    def test_resume_accepts_structured_corrections_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "cli.db"
            graph_path = Path(tmp) / "graph.db"
            settings = Settings(
                database_path=database_path,
                langgraph_checkpoint_path=graph_path,
                use_qwen=False,
            )
            agent = GraphAgent(settings, VaultRepository(database_path))
            turn = agent.start_create_record(
                "我 2099-07-25 买了一个键盘，899 元，七天退货，不提醒。"
            )
            agent.close()

            environment = {
                **os.environ,
                "LIFEVAULT_DB": str(database_path),
                "LIFEVAULT_LANGGRAPH_DB": str(graph_path),
                "LIFEVAULT_USE_QWEN": "0",
            }
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "lifevault.cli",
                    "resume",
                    turn.thread_id,
                    "--corrections-json",
                    '{"amount": 999, "return_days": 14}',
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                input="n\n",
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"amount": 999.0', result.stdout)
            self.assertIn('"return_deadline": "2099-08-08"', result.stdout)


if __name__ == "__main__":
    unittest.main()
