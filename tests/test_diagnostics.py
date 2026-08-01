from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from configparser import ConfigParser
from contextlib import redirect_stdout
from importlib.metadata import version as installed_version
from pathlib import Path
from unittest.mock import Mock, patch

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from lifevault.backup.runtime import RuntimeStateStore
from lifevault.config import Settings
from lifevault.diagnostics import DIRECT_REQUIREMENTS, run_diagnostics
from lifevault.storage.repository import VaultRepository


class DiagnosticsTest(unittest.TestCase):
    def test_new_install_is_ready_with_uninitialized_database_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            report = run_diagnostics(settings, check_qwen=False)

        self.assertTrue(report.ok)
        self.assertEqual(report.failed, 0)
        warning_names = {
            check.name for check in report.checks if check.status == "warning"
        }
        self.assertEqual(
            warning_names,
            {"vault_database", "checkpoint_database"},
        )

    def test_initialized_vault_passes_all_offline_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            VaultRepository(settings.database_path)
            with sqlite3.connect(settings.langgraph_checkpoint_path) as connection:
                connection.execute("CREATE TABLE checkpoint_probe (id INTEGER PRIMARY KEY)")

            report = run_diagnostics(settings, check_qwen=False)

        self.assertTrue(report.ok)
        self.assertEqual(report.warnings, 0)

    def test_corrupt_database_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.database_path.write_bytes(b"not sqlite")

            report = run_diagnostics(settings, check_qwen=False)

        check = next(check for check in report.checks if check.name == "vault_database")
        self.assertEqual(check.status, "failed")
        self.assertFalse(report.ok)

    def test_newer_database_schema_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            with sqlite3.connect(settings.database_path) as connection:
                connection.execute("PRAGMA user_version = 999")

            report = run_diagnostics(settings, check_qwen=False)

        check = next(check for check in report.checks if check.name == "vault_database")
        self.assertEqual(check.status, "failed")
        self.assertIn("newer than supported", check.detail)

    def test_paused_worker_is_reported_without_mutating_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            store = RuntimeStateStore(settings.database_path)
            original = store.force_pause("runtime_state_invalid")

            report = run_diagnostics(settings, check_qwen=False)
            after, recovered = store.load()

        check = next(check for check in report.checks if check.name == "worker_runtime")
        self.assertEqual(check.status, "warning")
        self.assertEqual(after, original)
        self.assertFalse(recovered)

    def test_qwen_model_endpoint_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), use_qwen=True)
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "data": [{"id": settings.qwen_model}],
            }
            session = Mock()
            session.get.return_value = response
            with patch("lifevault.diagnostics.requests.Session", return_value=session):
                report = run_diagnostics(settings, check_qwen=True)

        check = next(check for check in report.checks if check.name == "local_qwen")
        self.assertEqual(check.status, "ok")
        self.assertFalse(session.trust_env)

    def test_unsupported_direct_dependency_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))

            def fake_version(distribution: str) -> str:
                if distribution == "streamlit":
                    return "2.0.0"
                return installed_version(distribution)

            with patch("lifevault.diagnostics.version", side_effect=fake_version):
                report = run_diagnostics(settings, check_qwen=False)

        check = next(check for check in report.checks if check.name == "dependencies")
        self.assertEqual(check.status, "failed")
        self.assertIn("streamlit=2.0.0", check.detail)

    def test_declared_dependency_ranges_match_doctor(self) -> None:
        root = Path(__file__).resolve().parents[1]
        parser = ConfigParser()
        parser.read(root / "setup.cfg")
        setup_requirements = {
            requirement.name: str(requirement.specifier)
            for requirement in (
                Requirement(line.strip())
                for line in parser.get("options", "install_requires").splitlines()
                if line.strip()
            )
        }
        text_requirements = {
            requirement.name: str(requirement.specifier)
            for requirement in (
                Requirement(line.strip())
                for line in (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        }

        expected = {
            distribution: str(SpecifierSet(specifier))
            for distribution, specifier in DIRECT_REQUIREMENTS.items()
        }
        self.assertEqual(setup_requirements, expected)
        self.assertEqual(text_requirements, expected)

    def test_cli_json_and_strict_modes(self) -> None:
        from lifevault.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            environment = {
                "LIFEVAULT_HOME": tmp,
                "LIFEVAULT_USE_QWEN": "0",
            }
            output = io.StringIO()
            with (
                patch.dict(os.environ, environment),
                patch.object(sys, "argv", ["lifevault", "doctor", "--no-qwen", "--json"]),
                redirect_stdout(output),
            ):
                main()
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["ok"])
            self.assertGreaterEqual(payload["warnings"], 1)

            with (
                patch.dict(os.environ, environment),
                patch.object(sys, "argv", ["lifevault", "doctor", "--no-qwen", "--strict"]),
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                main()
            self.assertEqual(raised.exception.code, 1)


def make_settings(root: Path, *, use_qwen: bool = False) -> Settings:
    return Settings(
        database_path=root / "lifevault.db",
        langgraph_checkpoint_path=root / "langgraph.sqlite",
        backup_dir=root / "backups",
        use_qwen=use_qwen,
    )


if __name__ == "__main__":
    unittest.main()
