from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from lifevault.agent.graph_agent import GraphAgent
from lifevault.config import Settings
from lifevault.models.schemas import LifeRecordCreate
from lifevault.storage.repository import VaultRepository


class CliCorrectionTest(unittest.TestCase):
    def test_backup_create_list_inspect_and_status_commands(self) -> None:
        from lifevault.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database_path = root / "cli-backup.db"
            backup_dir = root / "backups"
            VaultRepository(database_path)
            environment = {
                "LIFEVAULT_DB": str(database_path),
                "LIFEVAULT_LANGGRAPH_DB": str(root / "graph.db"),
                "LIFEVAULT_BACKUP_DIR": str(backup_dir),
                "LIFEVAULT_USE_QWEN": "0",
            }
            output = io.StringIO()
            with (
                patch.dict(os.environ, environment),
                patch.object(sys, "argv", ["lifevault", "backup", "create"]),
                patch("getpass.getpass", side_effect=["这是一个足够长的命令行密码123"] * 2),
                redirect_stdout(output),
            ):
                main()
            backup_id = next(backup_dir.glob("*.lvbackup")).stem
            self.assertIn(backup_id, output.getvalue())

            for command in (
                ["lifevault", "backup", "list"],
                ["lifevault", "backup", "status"],
            ):
                output = io.StringIO()
                with (
                    patch.dict(os.environ, environment),
                    patch.object(sys, "argv", command),
                    redirect_stdout(output),
                ):
                    main()
                self.assertIn(backup_id if command[-1] == "list" else '"backup_format_version": 1', output.getvalue())

            output = io.StringIO()
            with (
                patch.dict(os.environ, environment),
                patch.object(sys, "argv", ["lifevault", "backup", "inspect", backup_id]),
                patch("getpass.getpass", return_value="这是一个足够长的命令行密码123"),
                redirect_stdout(output),
            ):
                main()
            self.assertIn('"integrity": "ok"', output.getvalue())

    def test_archive_and_restore_commands_preview_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "cli-lifecycle.db"
            graph_path = Path(tmp) / "cli-lifecycle-graph.db"
            repo = VaultRepository(database_path)
            record = repo.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="停车费"),
                "cli-lifecycle-record",
            )
            environment = {
                **os.environ,
                "LIFEVAULT_DB": str(database_path),
                "LIFEVAULT_LANGGRAPH_DB": str(graph_path),
                "LIFEVAULT_USE_QWEN": "0",
            }
            root = Path(__file__).resolve().parents[1]
            dry_run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "lifevault.cli",
                    "archive",
                    record.id,
                    "1",
                    "--dry-run",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            self.assertIsNone(repo.get_record("local", record.id).archived_at)

            archived = subprocess.run(
                [sys.executable, "-m", "lifevault.cli", "archive", record.id, "1", "--yes"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(archived.returncode, 0, archived.stderr)
            self.assertIn("Archived", archived.stdout)
            self.assertIsNotNone(repo.get_record("local", record.id).archived_at)

            restored = subprocess.run(
                [sys.executable, "-m", "lifevault.cli", "restore", record.id, "2", "--yes"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertIn("Restored", restored.stdout)
            self.assertIsNone(repo.get_record("local", record.id).archived_at)

    def test_natural_edit_and_status_commands_require_confirmed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "cli-natural.db"
            graph_path = Path(tmp) / "cli-natural-graph.db"
            repo = VaultRepository(database_path)
            record = repo.save_record(
                "local",
                LifeRecordCreate(
                    record_type="bill",
                    title="房租",
                    amount=3000,
                    details={"bill_name": "房租"},
                ),
                "cli-natural-record",
            )
            environment = {
                **os.environ,
                "LIFEVAULT_DB": str(database_path),
                "LIFEVAULT_LANGGRAPH_DB": str(graph_path),
                "LIFEVAULT_USE_QWEN": "0",
            }
            natural = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "lifevault.cli",
                    "edit",
                    "金额改成 3200 元",
                    "--record-id",
                    record.id,
                    "--yes",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(natural.returncode, 0, natural.stderr)
            self.assertIn("Status: completed", natural.stdout)
            self.assertEqual(repo.get_record("local", record.id).amount, 3200)

            status = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "lifevault.cli",
                    "status",
                    record.id,
                    "paid",
                    "2",
                    "--yes",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("to paid, version=3", status.stdout)
            self.assertEqual(repo.get_record("local", record.id).status.value, "paid")

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

    def test_update_supports_dry_run_and_confirmed_partial_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "cli-update.db"
            repo = VaultRepository(database_path)
            record = repo.save_record(
                "local",
                LifeRecordCreate(
                    record_type="bill",
                    title="旧房租",
                    amount=3000,
                    deadline=date(2099, 8, 1),
                    details={"bill_name": "旧房租", "billing_period": "2099-07"},
                ),
                "cli-update-record",
            )
            environment = {
                **os.environ,
                "LIFEVAULT_DB": str(database_path),
                "LIFEVAULT_USE_QWEN": "0",
            }
            base_command = [
                sys.executable,
                "-m",
                "lifevault.cli",
                "update",
                record.id,
                "1",
                "--changes-json",
                '{"title": "新房租", "due_date": "2099-08-03"}',
            ]

            preview = subprocess.run(
                [*base_command, "--dry-run"],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn("Record update preview:", preview.stdout)
            self.assertIn('"title": "新房租"', preview.stdout)
            self.assertEqual(repo.get_record("local", record.id).version, 1)

            updated = subprocess.run(
                [*base_command, "--yes"],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(updated.returncode, 0, updated.stderr)
            self.assertIn("version=2", updated.stdout)
            persisted = repo.get_record("local", record.id)
            self.assertEqual(persisted.title, "新房租")
            self.assertEqual(persisted.deadline.isoformat(), "2099-08-03")


if __name__ == "__main__":
    unittest.main()
