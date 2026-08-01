from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from lifevault.agent.graph_agent import GraphAgent
from lifevault.backup.errors import BackupError
from lifevault.backup.service import BackupService
from lifevault.backup import service as backup_module
from lifevault.config import Settings
from lifevault.models.schemas import LifeRecordCreate
from lifevault.storage.database import connect
from lifevault.storage.repository import VaultRepository
from lifevault.worker.reminder_worker import ReminderWorker


PASSWORD = "这是一个足够长的备份密码123"


class BackupServiceTest(unittest.TestCase):
    def test_encrypted_round_trip_and_audit_are_minimal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo, service = _setup(Path(tmp))
            repo.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="房租"),
                "backup-record",
            )

            created = service.create_backup(PASSWORD)
            preview = service.inspect_backup(created["backup_id"], PASSWORD)

            self.assertEqual(preview["record_count"], 1)
            self.assertEqual(preview["checkpoint_state"], "empty")
            self.assertEqual(preview["user_id"], "local")
            self.assertEqual(len(service.list_backups()), 1)
            log = repo.list_audit_logs("local", action="create_backup")[0]
            self.assertEqual(log.target_id, created["backup_id"])
            self.assertNotIn("password", log.params_summary or "")
            self.assertNotIn(str(settings.backup_dir), log.params_summary or "")

    def test_wrong_password_and_ciphertext_tamper_share_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _settings, _repo, service = _setup(Path(tmp))
            created = service.create_backup(PASSWORD)

            with self.assertRaises(BackupError) as wrong:
                service.inspect_backup(created["backup_id"], "另一个同样足够长的错误密码456")
            self.assertEqual(wrong.exception.code, "backup_authentication_failed")

            path = service.backup_file(created["backup_id"])
            payload = bytearray(path.read_bytes())
            payload[-17] ^= 1
            path.write_bytes(payload)
            with self.assertRaises(BackupError) as tampered:
                service.inspect_backup(created["backup_id"], PASSWORD)
            self.assertEqual(tampered.exception.code, "backup_authentication_failed")

    def test_import_is_validated_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as target_tmp:
            _source_settings, _source_repo, source = _setup(Path(source_tmp))
            created = source.create_backup(PASSWORD)
            source_file = source.backup_file(created["backup_id"])
            _target_settings, target_repo, target = _setup(Path(target_tmp))

            imported = target.import_backup(source_file, PASSWORD)
            replayed = target.import_backup(source_file, PASSWORD)

            self.assertEqual(imported["result"], "ok")
            self.assertEqual(replayed["result"], "already_present")
            self.assertEqual(imported["backup_id"], created["backup_id"])
            logs = target_repo.list_audit_logs("local", action="import_backup")
            self.assertEqual([log.result for log in logs], ["already_present", "ok"])

    def test_restore_replaces_vault_creates_safety_backup_and_pauses_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo, service = _setup(Path(tmp))
            repo.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="备份前"),
                "before",
            )
            created = service.create_backup(PASSWORD)
            preview = service.inspect_backup(created["backup_id"], PASSWORD)
            repo.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="备份后"),
                "after",
            )

            restored = service.restore_backup(
                created["backup_id"],
                PASSWORD,
                expected_sha256=preview["file_sha256"],
                confirmation=created["backup_id"],
            )

            records = VaultRepository(settings.database_path).search_records("local")
            self.assertEqual([record.title for record in records], ["备份前"])
            self.assertNotEqual(restored["safety_backup_id"], created["backup_id"])
            self.assertEqual(len(service.list_backups()), 2)
            self.assertTrue(service.status()["worker_paused"])
            self.assertEqual(ReminderWorker(settings).run_once(), 0)
            resumed = service.resume_worker("RESUME WORKER")
            self.assertEqual(resumed["result"], "ok")
            self.assertFalse(service.status()["worker_paused"])

    def test_restore_confirmation_is_bound_to_preview_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _settings, _repo, service = _setup(Path(tmp))
            created = service.create_backup(PASSWORD)
            preview = service.inspect_backup(created["backup_id"], PASSWORD)

            with self.assertRaises(BackupError) as mismatch:
                service.restore_backup(
                    created["backup_id"],
                    PASSWORD,
                    expected_sha256="0" * 64,
                    confirmation=created["backup_id"],
                )
            self.assertEqual(mismatch.exception.code, "backup_confirmation_failed")
            self.assertEqual(preview["record_count"], 0)
            self.assertEqual(len(service.list_backups()), 1)

    def test_user_and_schema_boundaries_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, _repo, service = _setup(Path(tmp))
            created = service.create_backup(PASSWORD)
            other_user = BackupService(
                Settings(
                    database_path=settings.database_path,
                    langgraph_checkpoint_path=settings.langgraph_checkpoint_path,
                    backup_dir=settings.backup_dir,
                    default_user_id="other",
                    use_qwen=False,
                )
            )
            with self.assertRaises(BackupError) as mismatch:
                other_user.inspect_backup(created["backup_id"], PASSWORD)
            self.assertEqual(mismatch.exception.code, "user_scope_mismatch")

            with connect(settings.database_path) as conn:
                conn.execute(
                    "INSERT INTO user_preferences(user_id, default_time, default_advance_days) "
                    "VALUES ('other', '09:00', 2)"
                )
            with self.assertRaises(BackupError) as user_scope:
                service.create_backup(PASSWORD)
            self.assertEqual(user_scope.exception.code, "vault_user_scope_invalid")

            with connect(settings.database_path) as conn:
                conn.execute("DELETE FROM user_preferences WHERE user_id = 'other'")
                conn.execute("CREATE TABLE unexpected_table(id TEXT)")
            with self.assertRaises(BackupError) as schema:
                service.create_backup(PASSWORD)
            self.assertEqual(schema.exception.code, "backup_schema_invalid")

    def test_audit_failure_removes_new_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _settings, _repo, service = _setup(Path(tmp))
            with patch.object(service, "_audit", side_effect=RuntimeError("audit unavailable")):
                with self.assertRaises(BackupError) as failed:
                    service.create_backup(PASSWORD)
            self.assertEqual(failed.exception.code, "audit_write_failed")
            self.assertEqual(service.list_backups(), [])

    def test_stream_import_enforces_size_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _settings, _repo, service = _setup(Path(tmp))
            with patch("lifevault.backup.service.MAX_BACKUP_BYTES", 8):
                with self.assertRaises(BackupError) as failed:
                    service.import_stream(io.BytesIO(b"123456789"), PASSWORD)
            self.assertEqual(failed.exception.code, "backup_file_too_large")

    def test_second_database_validation_failure_rolls_back_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo, service = _setup(Path(tmp))
            repo.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="快照记录"),
                "snapshot",
            )
            created = service.create_backup(PASSWORD)
            preview = service.inspect_backup(created["backup_id"], PASSWORD)
            repo.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="当前记录"),
                "current",
            )
            original_validate = backup_module._validate_checkpoint_database

            def fail_installed_checkpoint(path: Path, *, allow_empty: bool = False) -> None:
                if path == settings.langgraph_checkpoint_path:
                    raise BackupError("backup_schema_invalid", "Injected checkpoint failure.")
                original_validate(path, allow_empty=allow_empty)

            with patch(
                "lifevault.backup.service._validate_checkpoint_database",
                side_effect=fail_installed_checkpoint,
            ):
                with self.assertRaises(BackupError) as failed:
                    service.restore_backup(
                        created["backup_id"],
                        PASSWORD,
                        expected_sha256=preview["file_sha256"],
                        confirmation=created["backup_id"],
                    )
            self.assertEqual(failed.exception.code, "backup_schema_invalid")
            records = VaultRepository(settings.database_path).search_records("local")
            self.assertEqual({record.title for record in records}, {"快照记录", "当前记录"})

    def test_authenticated_archive_with_extra_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _settings, _repo, service = _setup(root)
            service.backup_dir.mkdir(mode=0o700)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("manifest.json", "{}")
                bundle.writestr("vault.sqlite", b"vault")
                bundle.writestr("langgraph.sqlite", b"graph")
                bundle.writestr("../escape.txt", b"escape")
            backup_id = str(uuid4())
            destination = service.backup_dir / f"{backup_id}.lvbackup"
            backup_module._encrypt_container(
                archive,
                destination,
                backup_module.normalize_passphrase(PASSWORD),
            )

            with self.assertRaises(BackupError) as unsafe:
                service.inspect_backup(backup_id, PASSWORD)
            self.assertEqual(unsafe.exception.code, "backup_integrity_failed")
            self.assertFalse((root / "escape.txt").exists())

    def test_graph_reopens_checkpoint_after_restore_generation_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo, service = _setup(Path(tmp))
            graph = GraphAgent(settings, repo)
            try:
                before = graph.start_create_record(
                    "我 2099-07-25 买了一个键盘，899 元，七天退货，不提醒。"
                )
                created = service.create_backup(PASSWORD)
                preview = service.inspect_backup(created["backup_id"], PASSWORD)
                after = graph.start_create_record(
                    "房租 3000 元，2099-08-01 前缴费，不提醒。"
                )

                service.restore_backup(
                    created["backup_id"],
                    PASSWORD,
                    expected_sha256=preview["file_sha256"],
                    confirmation=created["backup_id"],
                )

                self.assertIsNotNone(graph.get_state(before.thread_id))
                self.assertIsNone(graph.get_state(after.thread_id))
            finally:
                graph.close()

    def test_corrupt_runtime_state_recovers_paused_and_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo, service = _setup(Path(tmp))
            service.runtime.load()
            service.runtime.path.write_text("not-json", encoding="utf-8")

            status = service.status()

            self.assertTrue(status["runtime_state_recovered"])
            self.assertTrue(status["worker_paused"])
            logs = repo.list_audit_logs("local", action="runtime_state_recovered")
            self.assertEqual(len(logs), 1)

    def test_committed_restore_journal_only_finishes_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, repo, service = _setup(Path(tmp))
            repo.save_record(
                "local",
                LifeRecordCreate(record_type="bill", title="已提交数据"),
                "committed",
            )
            operation_id = str(uuid4())
            backup_id = str(uuid4())
            vault_candidate, vault_rollback = backup_module._restore_paths(
                settings.database_path,
                operation_id,
            )
            checkpoint_candidate, checkpoint_rollback = backup_module._restore_paths(
                settings.langgraph_checkpoint_path,
                operation_id,
            )
            for path in (
                vault_candidate,
                vault_rollback,
                checkpoint_candidate,
                checkpoint_rollback,
            ):
                path.write_bytes(b"stale")
            service._write_restore_journal(
                {
                    "version": 1,
                    "operation_id": operation_id,
                    "backup_id": backup_id,
                    "phase": "committed",
                    "vault_original_existed": True,
                    "checkpoint_original_existed": False,
                }
            )

            result = service.recover_if_needed()

            self.assertEqual(result["result"], "commit_cleanup_completed")
            self.assertEqual(
                [record.title for record in VaultRepository(settings.database_path).search_records("local")],
                ["已提交数据"],
            )
            self.assertFalse(any(path.exists() for path in (
                vault_candidate,
                vault_rollback,
                checkpoint_candidate,
                checkpoint_rollback,
            )))


def _setup(root: Path) -> tuple[Settings, VaultRepository, BackupService]:
    settings = Settings(
        database_path=root / "vault.db",
        langgraph_checkpoint_path=root / "graph.db",
        backup_dir=root / "backups",
        use_qwen=False,
    )
    repo = VaultRepository(settings.database_path)
    return settings, repo, BackupService(settings)


if __name__ == "__main__":
    unittest.main()
