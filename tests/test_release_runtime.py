from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from lifevault.config import Settings, get_settings
from lifevault.models.llm_factory import QwenClient, select_extraction_skill
from lifevault.runtime.supervisor import _start_process, build_launch_plan
from lifevault.skills import load_skill


class PackagedResourceTest(unittest.TestCase):
    def test_all_record_skills_are_packaged_and_selectively_loaded(self) -> None:
        self.assertIn("Purchase Skill", load_skill("purchase"))
        self.assertIn("Subscription Skill", load_skill("subscription"))
        self.assertIn("Bill Skill", load_skill("bill"))
        self.assertEqual(select_extraction_skill("我订阅了 Netflix"), "subscription")
        self.assertEqual(select_extraction_skill("房租下周一缴费"), "bill")
        self.assertEqual(select_extraction_skill("昨天买了一个键盘"), "purchase")
        self.assertIsNone(select_extraction_skill("查一下最近的订阅"))

    def test_qwen_prompt_receives_only_the_selected_skill(self) -> None:
        settings = Settings(use_qwen=True)
        client = QwenClient(settings)
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "intent": "create_record",
                                "record_type": "subscription",
                                "title": "Netflix",
                                "service_name": "Netflix",
                            }
                        )
                    }
                }
            ]
        }
        client.session.post = Mock(return_value=response)

        client.extract_record("我订阅了 Netflix", datetime(2026, 8, 1, 10, 0))

        prompt = client.session.post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("Subscription Skill", prompt)
        self.assertNotIn("Purchase Skill", prompt)
        self.assertNotIn("Bill Skill", prompt)

    def test_packaged_eval_data_exists(self) -> None:
        from lifevault.eval.runner import DEFAULT_EXAMPLES_PATH
        from lifevault.eval.update_runner import DEFAULT_UPDATE_EXAMPLES_PATH

        self.assertTrue(DEFAULT_EXAMPLES_PATH.is_file())
        self.assertTrue(DEFAULT_UPDATE_EXAMPLES_PATH.is_file())


class InstalledRuntimeConfigurationTest(unittest.TestCase):
    def test_lifevault_home_controls_all_default_state_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            environment = {
                "LIFEVAULT_HOME": tmp,
                "LIFEVAULT_USE_QWEN": "0",
            }
            with patch.dict(os.environ, environment):
                settings = get_settings()

            root = Path(tmp)
            self.assertEqual(settings.database_path, root / "lifevault.db")
            self.assertEqual(
                settings.langgraph_checkpoint_path,
                root / "langgraph_checkpoints.sqlite",
            )
            self.assertEqual(settings.backup_dir, root / "backups")

    def test_custom_database_keeps_other_vault_state_beside_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "custom.db"
            with patch.dict(
                os.environ,
                {"LIFEVAULT_DB": str(database_path), "LIFEVAULT_USE_QWEN": "0"},
            ):
                settings = get_settings()

            self.assertEqual(
                settings.langgraph_checkpoint_path,
                database_path.parent / "langgraph_checkpoints.sqlite",
            )
            self.assertEqual(settings.backup_dir, database_path.parent / "backups")


class LocalSupervisorTest(unittest.TestCase):
    def test_launch_plan_runs_packaged_ui_and_worker(self) -> None:
        with patch("lifevault.runtime.supervisor.find_available_port", return_value=8512):
            plan = build_launch_plan(Settings(), port=8501, worker_interval=15)

        self.assertEqual(plan.url, "http://127.0.0.1:8512")
        self.assertIn("lifevault/app/main.py", plan.streamlit_command[4])
        self.assertIn("--server.headless", plan.streamlit_command)
        self.assertEqual(
            plan.worker_command,
            (
                sys.executable,
                "-m",
                "lifevault.cli",
                "worker",
                "--interval",
                "15",
            ),
        )
        self.assertIn(("LIFEVAULT_DB", str(Settings().database_path)), plan.environment)

    def test_launch_plan_can_disable_worker(self) -> None:
        with patch("lifevault.runtime.supervisor.find_available_port", return_value=8513):
            plan = build_launch_plan(Settings(), no_worker=True)
        self.assertIsNone(plan.worker_command)

    def test_remote_binding_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            build_launch_plan(Settings(), host="0.0.0.0")

    def test_child_processes_are_isolated_from_terminal_interrupts(self) -> None:
        with (
            patch("lifevault.runtime.supervisor.subprocess.Popen") as popen,
            patch("lifevault.runtime.supervisor.os.environ.copy", return_value={"BASE": "1"}),
        ):
            _start_process(
                (sys.executable, "-V"),
                (("LIFEVAULT_DB", "/tmp/test.db"),),
            )
        popen.assert_called_once_with(
            (sys.executable, "-V"),
            env={"BASE": "1", "LIFEVAULT_DB": "/tmp/test.db"},
            start_new_session=os.name == "posix",
        )

    def test_cli_serve_dispatches_before_graph_agents_are_created(self) -> None:
        from lifevault.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            environment = {
                "LIFEVAULT_HOME": tmp,
                "LIFEVAULT_USE_QWEN": "0",
            }
            with (
                patch.dict(os.environ, environment),
                patch.object(sys, "argv", ["lifevault", "serve", "--no-worker", "--port", "0"]),
                patch("lifevault.runtime.supervisor.serve", return_value=0) as serve_mock,
                patch("lifevault.cli.GraphAgent") as graph_agent,
            ):
                main()

            serve_mock.assert_called_once()
            graph_agent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
