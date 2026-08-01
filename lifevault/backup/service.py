from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from cryptography import __version__ as CRYPTOGRAPHY_VERSION
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from langgraph.checkpoint.sqlite import SqliteSaver

from lifevault import __version__
from lifevault.backup.errors import BackupError, LockTimeoutError
from lifevault.backup.locking import (
    get_crypto_lock,
    get_vault_lock,
    restore_journal_path,
)
from lifevault.backup.runtime import RuntimeStateStore, _fsync_directory
from lifevault.config import PROJECT_ROOT, Settings
from lifevault.storage.database import VAULT_SCHEMA_VERSION
from lifevault.storage.repository import VaultRepository


MAGIC = b"LVBKUP01"
FORMAT_VERSION = 1
PREFIX = struct.Struct(">8sHI")
TAG_SIZE = 16
SALT_SIZE = 32
NONCE_SIZE = 12
KEY_SIZE = 32
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
MAX_BACKUP_BYTES = 512 * 1024 * 1024
MAX_DATABASE_BYTES = 1024 * 1024 * 1024
MAX_HEADER_BYTES = 4096
CHUNK_SIZE = 1024 * 1024
MANIFEST_NAME = "manifest.json"
VAULT_NAME = "vault.sqlite"
CHECKPOINT_NAME = "langgraph.sqlite"
EXPECTED_ARCHIVE_NAMES = {MANIFEST_NAME, VAULT_NAME, CHECKPOINT_NAME}
UUID_BACKUP_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.lvbackup$"
)
BUSINESS_TABLES = {
    "life_records",
    "reminders",
    "reminder_batches",
    "record_update_operations",
    "record_status_update_operations",
    "record_lifecycle_operations",
    "user_preferences",
    "graph_checkpoints",
    "audit_logs",
}
BUSINESS_INDEXES = {
    "idx_life_records_type",
    "idx_life_records_status",
    "idx_life_records_deadline",
    "idx_life_records_archive",
    "idx_reminders_due",
    "idx_reminders_record",
    "idx_reminders_active_slot",
    "idx_record_update_operations_record",
    "idx_record_status_update_operations_record",
    "idx_record_lifecycle_operations_record",
}
CHECKPOINT_TABLES = {"checkpoints", "writes"}
BUSINESS_COLUMNS = {
    "life_records": {
        "id", "user_id", "type", "title", "amount", "currency",
        "event_date", "deadline", "status", "archived_at", "version",
        "details_json", "notes", "source_text_hash", "source_text_preview",
        "idempotency_key", "created_at", "updated_at",
    },
    "reminders": {
        "id", "record_id", "user_id", "scheduled_at", "reminder_type",
        "message", "status", "parent_id", "idempotency_key", "created_at",
        "updated_at", "sent_at",
    },
    "reminder_batches": {
        "user_id", "idempotency_key", "request_hash", "reminder_ids_json",
        "created_at",
    },
    "record_update_operations": {
        "user_id", "idempotency_key", "request_hash", "record_id",
        "result_json", "created_at",
    },
    "record_status_update_operations": {
        "user_id", "idempotency_key", "request_hash", "record_id",
        "result_json", "created_at",
    },
    "record_lifecycle_operations": {
        "user_id", "idempotency_key", "request_hash", "operation",
        "record_id", "result_json", "created_at",
    },
    "user_preferences": {
        "user_id", "default_time", "quiet_hours_start", "quiet_hours_end",
        "default_advance_days",
    },
    "graph_checkpoints": {"thread_id", "checkpoint_data", "updated_at"},
    "audit_logs": {
        "id", "user_id", "actor", "action", "target_id", "result",
        "params_summary", "created_at",
    },
}
CHECKPOINT_COLUMNS = {
    "checkpoints": {
        "thread_id", "checkpoint_ns", "checkpoint_id", "parent_checkpoint_id",
        "type", "checkpoint", "metadata",
    },
    "writes": {
        "thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx",
        "channel", "type", "value",
    },
}
HEADER_FIELDS = {"cipher", "kdf", "n", "r", "p", "salt", "nonce"}
MANIFEST_FIELDS = {
    "format_version",
    "backup_id",
    "backup_kind",
    "created_at",
    "app_version",
    "vault_schema_version",
    "user_id",
    "timezone",
    "checkpoint_state",
    "source_backup_id",
    "databases",
}

_crypto_match = re.match(r"^(\d+)\.(\d+)\.(\d+)", CRYPTOGRAPHY_VERSION)
if _crypto_match is None or not (46, 0, 3) <= tuple(
    int(item) for item in _crypto_match.groups()
) < (50, 0, 0):
    raise RuntimeError(
        "LifeVault backup requires cryptography>=46.0.3,<50."
    )


@dataclass(frozen=True)
class BackupListItem:
    backup_id: str
    size: int
    modified_at: str
    status: str = "unverified"


@dataclass(frozen=True)
class BackupPreview:
    backup_id: str
    backup_kind: str
    created_at: str
    app_version: str
    vault_schema_version: int
    user_id: str
    source_timezone: str
    target_timezone: str
    timezone_warning: bool
    vault_size: int
    checkpoint_size: int
    record_count: int
    active_record_count: int
    archived_record_count: int
    reminder_count: int
    overdue_reminder_count: int
    checkpoint_state: str
    source_backup_id: str | None
    file_sha256: str
    integrity: str = "ok"


class BackupService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database_path = settings.database_path.absolute()
        self.checkpoint_path = settings.langgraph_checkpoint_path.absolute()
        self.backup_dir = settings.backup_dir.absolute()
        self.staging_root = (PROJECT_ROOT / "data" / ".backup-staging").absolute()
        self.runtime = RuntimeStateStore(self.database_path)
        self.vault_lock = get_vault_lock(self.database_path)
        self.crypto_lock = get_crypto_lock(self.database_path)

    def create_backup(
        self,
        passphrase: str,
        *,
        safety: bool = False,
        source_backup_id: str | None = None,
    ) -> dict[str, Any]:
        password = normalize_passphrase(passphrase)
        self._prepare_directories()
        with self.crypto_lock.acquire(
            "exclusive", 10.0, error_code="backup_crypto_busy"
        ):
            self._cleanup_operation_artifacts()
            operation_id = str(uuid4())
            with self._staging(operation_id) as staging:
                backup_id = str(uuid4())
                try:
                    if safety:
                        with self.vault_lock.acquire("exclusive", 30.0):
                            result = self._create_backup_locked(
                                password,
                                backup_id,
                                staging,
                                backup_kind="pre_restore_safety",
                                source_backup_id=source_backup_id,
                            )
                    else:
                        with self.vault_lock.acquire("exclusive", 10.0):
                            snapshot = self._snapshot_databases(staging)
                        result = self._package_snapshot(
                            password,
                            backup_id,
                            staging,
                            snapshot,
                            backup_kind="manual",
                            source_backup_id=None,
                        )
                    try:
                        self._audit(
                            "create_backup",
                            backup_id,
                            "ok",
                            {
                                "backup_format_version": FORMAT_VERSION,
                                "safety": safety,
                            },
                        )
                    except Exception as exc:
                        self._remove_published_backup(backup_id)
                        raise BackupError(
                            "audit_write_failed",
                            "Backup creation audit failed.",
                        ) from exc
                    return result
                except BackupError as exc:
                    self._remove_published_backup(backup_id)
                    self._audit_failure("create_backup", backup_id, exc.code, safety=safety)
                    raise
                except Exception as exc:
                    self._remove_published_backup(backup_id)
                    self._audit_failure("create_backup", backup_id, "backup_integrity_failed", safety=safety)
                    raise BackupError("backup_integrity_failed", "Backup creation failed.") from exc

    def list_backups(self) -> list[dict[str, Any]]:
        self._prepare_directories()
        items: list[BackupListItem] = []
        for path in self.backup_dir.iterdir():
            if not UUID_BACKUP_RE.fullmatch(path.name):
                continue
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                continue
            items.append(
                BackupListItem(
                    backup_id=path.stem,
                    size=info.st_size,
                    modified_at=datetime.fromtimestamp(
                        info.st_mtime, timezone.utc
                    ).isoformat(),
                )
            )
        items.sort(key=lambda item: (item.modified_at, item.backup_id), reverse=True)
        return [asdict(item) for item in items]

    def inspect_backup(self, backup_id: str, passphrase: str) -> dict[str, Any]:
        password = normalize_passphrase(passphrase)
        path = self._backup_path(backup_id)
        with self.crypto_lock.acquire(
            "exclusive", 10.0, error_code="backup_crypto_busy"
        ):
            with self._staging(str(uuid4())) as staging:
                preview = self._decrypt_validate(path, password, staging, expected_id=backup_id)
                return asdict(preview)

    def import_backup(self, source_path: Path, passphrase: str) -> dict[str, Any]:
        password = normalize_passphrase(passphrase)
        self._prepare_directories()
        backup_id: str | None = None
        with self.crypto_lock.acquire(
            "exclusive", 10.0, error_code="backup_crypto_busy"
        ):
            with self._staging(str(uuid4())) as staging:
                try:
                    source_info = _safe_regular_file(source_path, max_bytes=MAX_BACKUP_BYTES)
                    preview = self._decrypt_validate(
                        source_path,
                        password,
                        staging,
                        expected_id=None,
                    )
                    backup_id = preview.backup_id
                    destination = self._backup_destination(backup_id)
                    if destination.exists():
                        existing_info = _safe_regular_file(destination, max_bytes=MAX_BACKUP_BYTES)
                        if (
                            source_info.st_size == existing_info.st_size
                            and _sha256_file(source_path) == _sha256_file(destination)
                        ):
                            self._audit("import_backup", backup_id, "already_present", {})
                            return {
                                "backup_id": backup_id,
                                "result": "already_present",
                                "preview": asdict(preview),
                            }
                        raise BackupError("backup_id_conflict", "A different backup already uses this ID.")

                    partial = destination.with_suffix(".lvbackup.partial")
                    self._copy_file_atomic_source(source_path, partial)
                    if _sha256_file(partial) != preview.file_sha256:
                        partial.unlink(missing_ok=True)
                        raise BackupError("backup_integrity_failed", "Imported backup changed while copying.")
                    os.replace(partial, destination)
                    os.chmod(destination, 0o600)
                    _fsync_directory(self.backup_dir)
                    try:
                        self._audit("import_backup", backup_id, "ok", {})
                    except Exception:
                        destination.unlink(missing_ok=True)
                        _fsync_directory(self.backup_dir)
                        raise BackupError("audit_write_failed", "Backup import audit failed.")
                    return {
                        "backup_id": backup_id,
                        "result": "ok",
                        "preview": asdict(preview),
                    }
                except BackupError as exc:
                    self._audit_failure("import_backup", backup_id, exc.code)
                    raise
                except Exception as exc:
                    self._audit_failure("import_backup", backup_id, "backup_integrity_failed")
                    raise BackupError("backup_integrity_failed", "Backup import failed.") from exc

    def import_stream(self, source: BinaryIO, passphrase: str) -> dict[str, Any]:
        self._prepare_directories()
        upload = self.staging_root / f".upload-{uuid4()}.lvbackup"
        fd = os.open(upload, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        written = 0
        try:
            with os.fdopen(fd, "wb", closefd=True) as output:
                while True:
                    chunk = source.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_BACKUP_BYTES:
                        raise BackupError(
                            "backup_file_too_large",
                            "Uploaded backup exceeds 512 MiB.",
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            return self.import_backup(upload, passphrase)
        finally:
            upload.unlink(missing_ok=True)

    def backup_file(self, backup_id: str) -> Path:
        path = self._backup_path(backup_id)
        _safe_regular_file(path, max_bytes=MAX_BACKUP_BYTES)
        return path

    def restore_backup(
        self,
        backup_id: str,
        passphrase: str,
        *,
        expected_sha256: str,
        confirmation: str,
    ) -> dict[str, Any]:
        if confirmation != backup_id:
            raise BackupError("backup_confirmation_failed", "The backup ID confirmation did not match.")
        password = normalize_passphrase(passphrase)
        path = self._backup_path(backup_id)
        self._prepare_directories()
        with self.crypto_lock.acquire(
            "exclusive", 30.0, error_code="backup_crypto_busy"
        ):
            operation_id = str(uuid4())
            with self._staging(operation_id) as staging:
                try:
                    preview = self._decrypt_validate(path, password, staging, expected_id=backup_id)
                    if preview.file_sha256 != expected_sha256:
                        raise BackupError(
                            "backup_confirmation_failed",
                            "The backup changed after preview.",
                        )
                    with self.vault_lock.acquire("exclusive", 30.0):
                        _prepare_sqlite_for_replacement(self.database_path)
                        if self.checkpoint_path.exists():
                            _prepare_sqlite_for_replacement(self.checkpoint_path)
                        safety_staging = staging / "safety"
                        safety_staging.mkdir(mode=0o700)
                        safety_id = str(uuid4())
                        try:
                            safety_result = self._create_backup_locked(
                                password,
                                safety_id,
                                safety_staging,
                                backup_kind="pre_restore_safety",
                                source_backup_id=backup_id,
                            )
                            self._audit(
                                "create_backup",
                                safety_id,
                                "ok",
                                {
                                    "backup_format_version": FORMAT_VERSION,
                                    "safety": True,
                                },
                            )
                        except Exception as exc:
                            self._remove_published_backup(safety_id)
                            self._audit_failure(
                                "create_backup",
                                safety_id,
                                "restore_safety_backup_failed",
                                safety=True,
                            )
                            raise BackupError(
                                "restore_safety_backup_failed",
                                "The pre-restore safety backup failed.",
                            ) from exc
                        self._install_restored_databases(
                            operation_id,
                            backup_id,
                            staging / VAULT_NAME,
                            staging / CHECKPOINT_NAME,
                        )
                        try:
                            self._audit("restore_backup", backup_id, "ok", {})
                        except Exception as exc:
                            self._rollback_journal()
                            raise BackupError(
                                "audit_write_failed",
                                "Restore audit failed; the original vault was restored.",
                            ) from exc
                        self.runtime.pause_after_restore(backup_id)
                        self._finish_restore_journal()
                    return {
                        "backup_id": backup_id,
                        "safety_backup_id": safety_result["backup_id"],
                        "result": "ok",
                        "worker_paused": True,
                    }
                except BackupError as exc:
                    if restore_journal_path(self.database_path).exists():
                        if self._load_restore_journal()["phase"] == "committed":
                            raise BackupError(
                                "restore_recovery_required",
                                "Restore committed; startup cleanup is required.",
                            ) from exc
                        try:
                            self._rollback_journal()
                        except Exception as rollback_exc:
                            raise BackupError(
                                "restore_recovery_required",
                                "Automatic restore rollback requires recovery.",
                            ) from rollback_exc
                    self._audit_failure("restore_backup", backup_id, exc.code)
                    raise
                except Exception as exc:
                    if restore_journal_path(self.database_path).exists():
                        try:
                            if self._load_restore_journal()["phase"] == "committed":
                                raise BackupError(
                                    "restore_recovery_required",
                                    "Restore committed; startup cleanup is required.",
                                ) from exc
                        except BackupError:
                            raise
                    try:
                        self._rollback_journal()
                    except Exception:
                        pass
                    self._audit_failure("restore_backup", backup_id, "backup_integrity_failed")
                    raise BackupError("backup_integrity_failed", "Backup restore failed.") from exc

    def status(self) -> dict[str, Any]:
        self._prepare_directories()
        state, recovered = self.runtime.load()
        if recovered:
            try:
                self._audit("runtime_state_recovered", None, "ok", {})
            except Exception as exc:
                raise BackupError(
                    "audit_write_failed",
                    "Runtime state recovery audit failed.",
                ) from exc
        vault_busy = False
        try:
            with self.vault_lock.acquire("shared", 0.0):
                vault_integrity = (
                    _quick_check(self.database_path)
                    if self.database_path.exists()
                    else "missing"
                )
                checkpoint_integrity = (
                    _quick_check(self.checkpoint_path)
                    if self.checkpoint_path.exists()
                    else "missing"
                )
                current_counts = (
                    _preview_counts(self.database_path, self.settings.default_timezone)
                    if vault_integrity == "ok"
                    else {}
                )
        except LockTimeoutError:
            vault_busy = True
            vault_integrity = "busy"
            checkpoint_integrity = "busy"
            current_counts = {}
        crypto_busy = False
        try:
            with self.crypto_lock.acquire(
                "exclusive", 0.0, error_code="backup_crypto_busy"
            ):
                pass
        except LockTimeoutError:
            crypto_busy = True
        return {
            "app_version": __version__,
            "backup_format_version": FORMAT_VERSION,
            "vault_schema_version": _database_user_version(self.database_path),
            "backup_dir": str(self.backup_dir),
            "backup_dir_writable": os.access(self.backup_dir, os.W_OK),
            "vault_exists": self.database_path.exists(),
            "checkpoint_exists": self.checkpoint_path.exists(),
            "vault_integrity": vault_integrity,
            "checkpoint_integrity": checkpoint_integrity,
            "vault_generation": state.vault_generation,
            "worker_paused": state.worker_paused_after_restore,
            "pause_backup_id": state.pause_backup_id,
            "pause_reason": state.pause_reason,
            "runtime_state_recovered": recovered,
            "restore_recovery_pending": restore_journal_path(self.database_path).exists(),
            "vault_lock_busy": vault_busy,
            "crypto_lock_busy": crypto_busy,
            "available_space": shutil.disk_usage(self.backup_dir).free,
            "filesystem_space": {
                "backup": shutil.disk_usage(self.backup_dir).free,
                "vault": shutil.disk_usage(self.database_path.parent).free,
                "checkpoint": shutil.disk_usage(self.checkpoint_path.parent).free,
            },
            "minimum_backup_space": self._minimum_backup_space(),
            "overdue_reminder_count": current_counts.get("overdue_reminder_count", 0),
        }

    def resume_worker(self, confirmation: str) -> dict[str, Any]:
        if confirmation != "RESUME WORKER":
            raise BackupError("backup_confirmation_failed", "Worker resume confirmation did not match.")
        with self.vault_lock.acquire("exclusive", 3.0):
            state, _ = self.runtime.load()
            if not state.worker_paused_after_restore:
                return {"result": "already_running", "worker_paused": False}
            try:
                self._audit(
                    "resume_worker_after_restore",
                    state.pause_backup_id,
                    "ok",
                    {},
                )
            except Exception as exc:
                raise BackupError(
                    "audit_write_failed",
                    "Worker resume audit failed; Worker remains paused.",
                ) from exc
            self.runtime.resume_worker()
            return {"result": "ok", "worker_paused": False}

    def recover_if_needed(self) -> dict[str, Any] | None:
        journal = restore_journal_path(self.database_path)
        if not journal.exists():
            try:
                self._prepare_directories()
                with self.crypto_lock.acquire(
                    "exclusive", 0.0, error_code="backup_crypto_busy"
                ):
                    with self.vault_lock.acquire("exclusive", 0.0):
                        self._cleanup_operation_artifacts()
                        self._cleanup_orphan_restore_files()
            except LockTimeoutError:
                pass
            return None
        with self.vault_lock.acquire("exclusive", 30.0):
            data = self._load_restore_journal()
            backup_id = data["backup_id"]
            if data["phase"] == "committed":
                self._cleanup_restore_files(data)
                journal.unlink(missing_ok=True)
                _fsync_directory(journal.parent)
                return {
                    "result": "commit_cleanup_completed",
                    "backup_id": backup_id,
                    "worker_paused": True,
                }
            if data["phase"] != "rolled_back_audit_pending":
                self._rollback_journal(keep_journal=True)
                self.runtime.force_pause("restore_recovery")
                self._write_restore_journal({**data, "phase": "rolled_back_audit_pending"})
            try:
                self._audit(
                    "restore_recovery",
                    backup_id,
                    "ok",
                    {"operation_id": data["operation_id"]},
                )
            except Exception as exc:
                self.runtime.force_pause("restore_recovery_audit_pending")
                raise BackupError("audit_write_failed", "Restore recovery audit is pending.") from exc
            self._cleanup_restore_files(data)
            journal.unlink(missing_ok=True)
            _fsync_directory(journal.parent)
            return {"result": "ok", "backup_id": backup_id, "worker_paused": True}

    def _create_backup_locked(
        self,
        password: bytes,
        backup_id: str,
        staging: Path,
        *,
        backup_kind: str,
        source_backup_id: str | None,
    ) -> dict[str, Any]:
        snapshot = self._snapshot_databases(staging)
        return self._package_snapshot(
            password,
            backup_id,
            staging,
            snapshot,
            backup_kind=backup_kind,
            source_backup_id=source_backup_id,
        )

    def _snapshot_databases(self, staging: Path) -> dict[str, Any]:
        _validate_database_source(self.database_path, required=True)
        source_total = self.database_path.stat().st_size
        if self.checkpoint_path.exists():
            _validate_database_source(self.checkpoint_path, required=True)
            source_total += self.checkpoint_path.stat().st_size
        if source_total > MAX_DATABASE_BYTES:
            raise BackupError("backup_payload_too_large", "The databases exceed the backup limit.")
        self._require_space(staging, max(source_total * 3, CHUNK_SIZE))

        vault_snapshot = staging / VAULT_NAME
        checkpoint_snapshot = staging / CHECKPOINT_NAME
        _sqlite_snapshot(self.database_path, vault_snapshot)
        _validate_business_database(vault_snapshot, self.settings.default_user_id)

        checkpoint_state = "present"
        if self.checkpoint_path.exists() and self.checkpoint_path.stat().st_size:
            _sqlite_snapshot(self.checkpoint_path, checkpoint_snapshot)
            _validate_checkpoint_database(checkpoint_snapshot)
        else:
            checkpoint_state = "empty"
            _create_empty_sqlite(checkpoint_snapshot)

        return {
            "checkpoint_state": checkpoint_state,
            "vault": _database_metadata(vault_snapshot),
            "checkpoint": _database_metadata(checkpoint_snapshot),
        }

    def _package_snapshot(
        self,
        password: bytes,
        backup_id: str,
        staging: Path,
        snapshot: dict[str, Any],
        *,
        backup_kind: str,
        source_backup_id: str | None,
    ) -> dict[str, Any]:
        snapshot_bytes = int(snapshot["vault"]["size"]) + int(
            snapshot["checkpoint"]["size"]
        )
        self._require_space(self.backup_dir, max(snapshot_bytes, CHUNK_SIZE))
        manifest = {
            "format_version": FORMAT_VERSION,
            "backup_id": backup_id,
            "backup_kind": backup_kind,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "app_version": __version__,
            "vault_schema_version": VAULT_SCHEMA_VERSION,
            "user_id": self.settings.default_user_id,
            "timezone": self.settings.default_timezone,
            "checkpoint_state": snapshot["checkpoint_state"],
            "source_backup_id": source_backup_id,
            "databases": {
                VAULT_NAME: snapshot["vault"],
                CHECKPOINT_NAME: snapshot["checkpoint"],
            },
        }
        manifest_path = staging / MANIFEST_NAME
        manifest_path.write_bytes(_canonical_json(manifest))
        os.chmod(manifest_path, 0o600)
        archive = staging / "payload.zip"
        with zipfile.ZipFile(
            archive,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as bundle:
            for name in (MANIFEST_NAME, VAULT_NAME, CHECKPOINT_NAME):
                bundle.write(staging / name, arcname=name)
        destination = self._backup_destination(backup_id)
        partial = destination.with_suffix(".lvbackup.partial")
        _encrypt_container(archive, partial, password)
        if partial.stat().st_size > MAX_BACKUP_BYTES:
            partial.unlink(missing_ok=True)
            raise BackupError("backup_file_too_large", "The encrypted backup exceeds 512 MiB.")

        verified = staging / "verified.zip"
        _decrypt_container(partial, verified, password)
        if _sha256_file(verified) != _sha256_file(archive):
            partial.unlink(missing_ok=True)
            raise BackupError("backup_integrity_failed", "Generated backup verification failed.")
        os.replace(partial, destination)
        os.chmod(destination, 0o600)
        _fsync_directory(self.backup_dir)
        return {
            "backup_id": backup_id,
            "result": "ok",
            "size": destination.stat().st_size,
            "backup_kind": backup_kind,
        }

    def _decrypt_validate(
        self,
        path: Path,
        password: bytes,
        staging: Path,
        *,
        expected_id: str | None,
    ) -> BackupPreview:
        _safe_regular_file(path, max_bytes=MAX_BACKUP_BYTES)
        file_hash = _sha256_file(path)
        archive = staging / "payload.zip"
        _decrypt_container(path, archive, password)
        extracted = _extract_archive(archive, staging)
        manifest = _load_manifest(extracted[MANIFEST_NAME])
        backup_id = manifest["backup_id"]
        if expected_id is not None and backup_id != expected_id:
            raise BackupError("backup_identity_mismatch", "Backup filename and manifest ID differ.")
        _validate_manifest_compatibility(manifest, self.settings)
        database_total = sum(int(manifest["databases"][name]["size"]) for name in (VAULT_NAME, CHECKPOINT_NAME))
        if database_total > MAX_DATABASE_BYTES:
            raise BackupError("backup_payload_too_large", "The backup databases exceed 1 GiB.")
        for name in (VAULT_NAME, CHECKPOINT_NAME):
            metadata = manifest["databases"].get(name)
            if not isinstance(metadata, dict):
                raise BackupError("backup_integrity_failed", "Backup database metadata is invalid.")
            file_path = extracted[name]
            if file_path.stat().st_size != metadata.get("size"):
                raise BackupError("backup_integrity_failed", "Backup database size mismatch.")
            if _sha256_file(file_path) != metadata.get("sha256"):
                raise BackupError("backup_integrity_failed", "Backup database checksum mismatch.")
            if _schema_hash(file_path) != metadata.get("schema_sha256"):
                raise BackupError("backup_schema_invalid", "Backup schema fingerprint mismatch.")
        _validate_business_database(extracted[VAULT_NAME], self.settings.default_user_id)
        _validate_checkpoint_database(
            extracted[CHECKPOINT_NAME],
            allow_empty=manifest["checkpoint_state"] == "empty",
        )
        counts = _preview_counts(
            extracted[VAULT_NAME],
            self.settings.default_timezone,
        )
        return BackupPreview(
            backup_id=backup_id,
            backup_kind=manifest["backup_kind"],
            created_at=manifest["created_at"],
            app_version=manifest["app_version"],
            vault_schema_version=manifest["vault_schema_version"],
            user_id=manifest["user_id"],
            source_timezone=manifest["timezone"],
            target_timezone=self.settings.default_timezone,
            timezone_warning=manifest["timezone"] != self.settings.default_timezone,
            vault_size=extracted[VAULT_NAME].stat().st_size,
            checkpoint_size=extracted[CHECKPOINT_NAME].stat().st_size,
            checkpoint_state=manifest["checkpoint_state"],
            source_backup_id=manifest["source_backup_id"],
            file_sha256=file_hash,
            **counts,
        )

    def _install_restored_databases(
        self,
        operation_id: str,
        backup_id: str,
        vault_source: Path,
        checkpoint_source: Path,
    ) -> None:
        _validate_database_source(self.database_path, required=True)
        checkpoint_existed = self.checkpoint_path.exists()
        if checkpoint_existed:
            _validate_database_source(self.checkpoint_path, required=True)
        vault_candidate, vault_rollback = _restore_paths(self.database_path, operation_id)
        checkpoint_candidate, checkpoint_rollback = _restore_paths(self.checkpoint_path, operation_id)
        self._require_space(self.database_path.parent, vault_source.stat().st_size * 2)
        self._require_space(self.checkpoint_path.parent, checkpoint_source.stat().st_size * 2)
        _copy_plain_candidate(vault_source, vault_candidate)
        _copy_plain_candidate(checkpoint_source, checkpoint_candidate)
        _validate_business_database(vault_candidate, self.settings.default_user_id)
        _migrate_checkpoint_candidate(checkpoint_candidate)
        _validate_checkpoint_database(checkpoint_candidate, allow_empty=True)
        journal = {
            "version": 1,
            "operation_id": operation_id,
            "backup_id": backup_id,
            "phase": "prepared",
            "vault_original_existed": True,
            "checkpoint_original_existed": checkpoint_existed,
        }
        self._write_restore_journal(journal)
        _remove_sqlite_sidecars(self.database_path)
        _remove_sqlite_sidecars(self.checkpoint_path)
        os.replace(self.database_path, vault_rollback)
        if checkpoint_existed:
            os.replace(self.checkpoint_path, checkpoint_rollback)
        self._write_restore_journal({**journal, "phase": "originals_moved"})
        os.replace(vault_candidate, self.database_path)
        os.replace(checkpoint_candidate, self.checkpoint_path)
        os.chmod(self.database_path, 0o600)
        os.chmod(self.checkpoint_path, 0o600)
        _fsync_directory(self.database_path.parent)
        if self.checkpoint_path.parent != self.database_path.parent:
            _fsync_directory(self.checkpoint_path.parent)
        self._write_restore_journal({**journal, "phase": "replacements_installed"})
        _validate_business_database(self.database_path, self.settings.default_user_id)
        _validate_checkpoint_database(self.checkpoint_path, allow_empty=True)

    def _rollback_journal(self, *, keep_journal: bool = False) -> None:
        journal_path = restore_journal_path(self.database_path)
        if not journal_path.exists():
            return
        data = self._load_restore_journal()
        operation_id = data["operation_id"]
        for target, existed_key in (
            (self.database_path, "vault_original_existed"),
            (self.checkpoint_path, "checkpoint_original_existed"),
        ):
            candidate, rollback = _restore_paths(target, operation_id)
            if rollback.exists():
                os.replace(rollback, target)
                os.chmod(target, 0o600)
            elif not data[existed_key] and target.exists():
                target.unlink()
            candidate.unlink(missing_ok=True)
        _fsync_directory(self.database_path.parent)
        if self.checkpoint_path.parent != self.database_path.parent:
            _fsync_directory(self.checkpoint_path.parent)
        if self.database_path.exists() and _quick_check(self.database_path) != "ok":
            raise BackupError("restore_recovery_required", "The business database rollback is invalid.")
        if self.checkpoint_path.exists() and _quick_check(self.checkpoint_path) != "ok":
            raise BackupError("restore_recovery_required", "The checkpoint database rollback is invalid.")
        if not keep_journal:
            journal_path.unlink(missing_ok=True)
            _fsync_directory(journal_path.parent)

    def _finish_restore_journal(self) -> None:
        data = self._load_restore_journal()
        path = restore_journal_path(self.database_path)
        self._write_restore_journal({**data, "phase": "committed"})
        self._cleanup_restore_files(data)
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)

    def _cleanup_restore_files(self, data: dict[str, Any]) -> None:
        operation_id = data["operation_id"]
        for target in (self.database_path, self.checkpoint_path):
            candidate, rollback = _restore_paths(target, operation_id)
            candidate.unlink(missing_ok=True)
            rollback.unlink(missing_ok=True)
            Path(f"{candidate}-wal").unlink(missing_ok=True)
            Path(f"{candidate}-shm").unlink(missing_ok=True)
        _fsync_directory(self.database_path.parent)
        if self.checkpoint_path.parent != self.database_path.parent:
            _fsync_directory(self.checkpoint_path.parent)

    def _write_restore_journal(self, data: dict[str, Any]) -> None:
        path = restore_journal_path(self.database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid4()}.tmp")
        payload = _canonical_json(data)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            os.chmod(path, 0o600)
            _fsync_directory(path.parent)
        finally:
            temp.unlink(missing_ok=True)

    def _load_restore_journal(self) -> dict[str, Any]:
        path = restore_journal_path(self.database_path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            expected = {
                "version",
                "operation_id",
                "backup_id",
                "phase",
                "vault_original_existed",
                "checkpoint_original_existed",
            }
            if not isinstance(raw, dict) or set(raw) != expected or raw["version"] != 1:
                raise ValueError
            UUID(raw["operation_id"])
            UUID(raw["backup_id"])
            if raw["phase"] not in {
                "prepared",
                "originals_moved",
                "replacements_installed",
                "rolled_back_audit_pending",
                "committed",
            }:
                raise ValueError
            if not isinstance(raw["vault_original_existed"], bool) or not isinstance(
                raw["checkpoint_original_existed"], bool
            ):
                raise ValueError
            return raw
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BackupError("restore_recovery_required", "The restore journal is invalid.") from exc

    @contextmanager
    def _staging(self, operation_id: str) -> Iterator[Path]:
        UUID(operation_id)
        self.staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.staging_root, 0o700)
        path = self.staging_root / operation_id
        path.mkdir(mode=0o700)
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def _prepare_directories(self) -> None:
        _ensure_safe_directory(self.backup_dir)
        _ensure_safe_directory(self.staging_root)

    def _cleanup_operation_artifacts(self) -> None:
        for path in self.staging_root.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
        for path in self.backup_dir.glob("*.lvbackup.partial"):
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)
        for path in self.staging_root.glob(".upload-*.lvbackup"):
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)

    def _cleanup_orphan_restore_files(self) -> None:
        for target in (self.database_path, self.checkpoint_path):
            for suffix in ("candidate", "rollback"):
                pattern = f".{target.name}.*.{suffix}"
                for path in target.parent.glob(pattern):
                    operation_text = path.name.removeprefix(f".{target.name}.").removesuffix(
                        f".{suffix}"
                    )
                    try:
                        UUID(operation_text)
                    except ValueError:
                        continue
                    if path.is_file() and not path.is_symlink():
                        path.unlink(missing_ok=True)

    def _backup_path(self, backup_id: str) -> Path:
        path = self._backup_destination(backup_id)
        if not path.exists():
            raise BackupError("backup_not_found", "Backup not found.")
        return path

    def _backup_destination(self, backup_id: str) -> Path:
        try:
            normalized = str(UUID(backup_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise BackupError("backup_not_found", "Backup not found.") from exc
        path = self.backup_dir / f"{normalized}.lvbackup"
        return path

    def _remove_published_backup(self, backup_id: str) -> None:
        path = self.backup_dir / f"{backup_id}.lvbackup"
        path.unlink(missing_ok=True)
        if self.backup_dir.exists():
            _fsync_directory(self.backup_dir)

    def _copy_file_atomic_source(self, source: Path, destination: Path) -> None:
        source_fd = _open_safe_read(source, max_bytes=MAX_BACKUP_BYTES)
        try:
            output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(output_fd, "wb", closefd=True) as output:
                while True:
                    chunk = os.read(source_fd, CHUNK_SIZE)
                    if not chunk:
                        break
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        finally:
            os.close(source_fd)

    def _audit(
        self,
        action: str,
        backup_id: str | None,
        result: str,
        params: dict[str, Any],
    ) -> None:
        VaultRepository(self.database_path).record_audit_event(
            user_id=self.settings.default_user_id,
            actor="user",
            action=action,
            target_id=backup_id,
            result=result,
            params=params,
        )

    def _audit_failure(
        self,
        action: str,
        backup_id: str | None,
        error_code: str,
        *,
        safety: bool | None = None,
    ) -> None:
        params: dict[str, Any] = {"error_code": error_code}
        if safety is not None:
            params["safety"] = safety
        try:
            self._audit(action, backup_id, "failed", params)
        except Exception as exc:
            raise BackupError(
                "audit_write_failed",
                "The operation was rolled back but its failure audit could not be written.",
            ) from exc

    def _require_space(self, path: Path, required: int) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(path).free < required:
            raise BackupError("backup_insufficient_space", "There is not enough free disk space.")

    def _minimum_backup_space(self) -> int:
        total = self.database_path.stat().st_size if self.database_path.exists() else 0
        if self.checkpoint_path.exists():
            total += self.checkpoint_path.stat().st_size
        return max(total * 3, CHUNK_SIZE)


def normalize_passphrase(passphrase: str) -> bytes:
    if not isinstance(passphrase, str):
        raise BackupError("backup_authentication_failed", "Backup password is invalid.")
    normalized = unicodedata.normalize("NFC", passphrase)
    if not 12 <= len(normalized) <= 256:
        raise BackupError(
            "backup_authentication_failed",
            "Backup password must contain 12 to 256 Unicode characters.",
        )
    return normalized.encode("utf-8")


def _derive_key(password: bytes, salt: bytes) -> bytes:
    return Scrypt(
        salt=salt,
        length=KEY_SIZE,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
    ).derive(password)


def _encrypt_container(source: Path, destination: Path, password: bytes) -> None:
    salt = os.urandom(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    header = _canonical_json(
        {
            "cipher": "AES-256-GCM",
            "kdf": "scrypt",
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
        }
    )
    prefix = PREFIX.pack(MAGIC, FORMAT_VERSION, len(header)) + header
    key = _derive_key(password, salt)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(prefix)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as source_handle, os.fdopen(fd, "wb", closefd=True) as output:
            output.write(prefix)
            while True:
                chunk = source_handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                output.write(encryptor.update(chunk))
            output.write(encryptor.finalize())
            output.write(encryptor.tag)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _decrypt_container(source: Path, destination: Path, password: bytes) -> None:
    size = _safe_regular_file(source, max_bytes=MAX_BACKUP_BYTES).st_size
    minimum = PREFIX.size + 2 + TAG_SIZE
    if size < minimum:
        raise BackupError("backup_format_unsupported", "Backup container is truncated.")
    source_fd = _open_safe_read(source, max_bytes=MAX_BACKUP_BYTES)
    try:
        with os.fdopen(source_fd, "rb", closefd=True) as source_handle:
            fixed = source_handle.read(PREFIX.size)
            magic, version, header_length = PREFIX.unpack(fixed)
            if magic != MAGIC or version != FORMAT_VERSION or not 2 <= header_length <= MAX_HEADER_BYTES:
                raise BackupError("backup_format_unsupported", "Backup container format is unsupported.")
            if PREFIX.size + header_length + TAG_SIZE > size:
                raise BackupError("backup_format_unsupported", "Backup container header is invalid.")
            header_bytes = source_handle.read(header_length)
            header = _parse_header(header_bytes)
            prefix = fixed + header_bytes
            source_handle.seek(size - TAG_SIZE)
            tag = source_handle.read(TAG_SIZE)
            source_handle.seek(PREFIX.size + header_length)
            remaining = size - PREFIX.size - header_length - TAG_SIZE
            key = _derive_key(password, header["salt"])
            decryptor = Cipher(
                algorithms.AES(key),
                modes.GCM(header["nonce"], tag),
            ).decryptor()
            decryptor.authenticate_additional_data(prefix)
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=True) as output:
                    while remaining:
                        chunk = source_handle.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            raise BackupError("backup_format_unsupported", "Backup ciphertext is truncated.")
                        remaining -= len(chunk)
                        output.write(decryptor.update(chunk))
                    output.write(decryptor.finalize())
                    output.flush()
                    os.fsync(output.fileno())
            except InvalidTag as exc:
                destination.unlink(missing_ok=True)
                raise BackupError(
                    "backup_authentication_failed",
                    "Backup authentication failed.",
                ) from exc
            except Exception:
                destination.unlink(missing_ok=True)
                raise
    except InvalidTag as exc:
        destination.unlink(missing_ok=True)
        raise BackupError("backup_authentication_failed", "Backup authentication failed.") from exc


def _parse_header(payload: bytes) -> dict[str, Any]:
    try:
        raw = json.loads(payload.decode("utf-8"))
        if _canonical_json(raw) != payload or not isinstance(raw, dict) or set(raw) != HEADER_FIELDS:
            raise ValueError
        if (
            raw["cipher"] != "AES-256-GCM"
            or raw["kdf"] != "scrypt"
            or raw["n"] != SCRYPT_N
            or raw["r"] != SCRYPT_R
            or raw["p"] != SCRYPT_P
        ):
            raise BackupError("backup_format_unsupported", "Backup cryptographic profile is unsupported.")
        salt = base64.b64decode(raw["salt"], validate=True)
        nonce = base64.b64decode(raw["nonce"], validate=True)
        if len(salt) != SALT_SIZE or len(nonce) != NONCE_SIZE:
            raise ValueError
        return {**raw, "salt": salt, "nonce": nonce}
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("backup_format_unsupported", "Backup header is invalid.") from exc


def _extract_archive(archive: Path, destination: Path) -> dict[str, Path]:
    try:
        with zipfile.ZipFile(archive, "r") as bundle:
            infos = bundle.infolist()
            names = [item.filename for item in infos]
            if len(infos) != 3 or set(names) != EXPECTED_ARCHIVE_NAMES or len(set(names)) != len(names):
                raise BackupError("backup_integrity_failed", "Backup archive entries are invalid.")
            total = 0
            for info in infos:
                mode = (info.external_attr >> 16) & 0o170000
                if (
                    info.is_dir()
                    or Path(info.filename).name != info.filename
                    or info.filename.startswith(("/", "\\"))
                    or mode not in {0, stat.S_IFREG}
                ):
                    raise BackupError("backup_integrity_failed", "Backup archive contains an unsafe entry.")
                if info.filename in {VAULT_NAME, CHECKPOINT_NAME}:
                    total += info.file_size
                    if total > MAX_DATABASE_BYTES:
                        raise BackupError("backup_payload_too_large", "Backup databases exceed 1 GiB.")
            if shutil.disk_usage(destination).free < max(total * 2, CHUNK_SIZE):
                raise BackupError(
                    "backup_insufficient_space",
                    "There is not enough space to validate this backup.",
                )
            extracted: dict[str, Path] = {}
            for info in infos:
                target = destination / info.filename
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                written = 0
                with bundle.open(info, "r") as source, os.fdopen(fd, "wb", closefd=True) as output:
                    while True:
                        chunk = source.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size:
                            raise BackupError("backup_payload_too_large", "Backup entry exceeded its declared size.")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if written != info.file_size:
                    raise BackupError("backup_integrity_failed", "Backup entry size mismatch.")
                extracted[info.filename] = target
            return extracted
    except BackupError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        raise BackupError("backup_integrity_failed", "Backup archive is invalid.") from exc


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != MANIFEST_FIELDS:
            raise ValueError
        if raw["format_version"] != FORMAT_VERSION:
            raise BackupError("backup_format_unsupported", "Backup manifest format is unsupported.")
        if not isinstance(raw["app_version"], str) or not isinstance(
            raw["vault_schema_version"], int
        ) or isinstance(raw["vault_schema_version"], bool):
            raise ValueError
        if not isinstance(raw["user_id"], str) or not raw["user_id"]:
            raise ValueError
        raw["backup_id"] = str(UUID(raw["backup_id"]))
        if raw["source_backup_id"] is not None:
            raw["source_backup_id"] = str(UUID(raw["source_backup_id"]))
        if raw["backup_kind"] not in {"manual", "pre_restore_safety"}:
            raise ValueError
        if raw["checkpoint_state"] not in {"empty", "present"}:
            raise ValueError
        created_at = datetime.fromisoformat(raw["created_at"])
        if created_at.tzinfo is None:
            raise ValueError
        ZoneInfo(raw["timezone"])
        if not isinstance(raw["databases"], dict) or set(raw["databases"]) != {VAULT_NAME, CHECKPOINT_NAME}:
            raise ValueError
        for metadata in raw["databases"].values():
            if not isinstance(metadata, dict) or set(metadata) != {
                "size",
                "sha256",
                "schema_sha256",
            }:
                raise ValueError
            size = metadata["size"]
            if (
                not isinstance(size, int)
                or isinstance(size, bool)
                or not 0 <= size <= MAX_DATABASE_BYTES
            ):
                raise ValueError
            for key in ("sha256", "schema_sha256"):
                if not isinstance(metadata[key], str) or not re.fullmatch(
                    r"[0-9a-f]{64}", metadata[key]
                ):
                    raise ValueError
        if (raw["backup_kind"] == "manual") != (raw["source_backup_id"] is None):
            raise ValueError
        return raw
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("backup_integrity_failed", "Backup manifest is invalid.") from exc


def _validate_manifest_compatibility(manifest: dict[str, Any], settings: Settings) -> None:
    if _version_tuple(manifest["app_version"]) > _version_tuple(__version__):
        raise BackupError("backup_too_new", "This backup was created by a newer LifeVault version.")
    if manifest["vault_schema_version"] > VAULT_SCHEMA_VERSION:
        raise BackupError("backup_too_new", "This backup uses a newer vault schema.")
    if manifest["vault_schema_version"] != VAULT_SCHEMA_VERSION:
        raise BackupError("backup_schema_invalid", "This backup schema cannot be migrated by v0.18.")
    if manifest["user_id"] != settings.default_user_id:
        raise BackupError("user_scope_mismatch", "Backup user does not match the configured user.")


def _validate_business_database(path: Path, user_id: str) -> None:
    if _quick_check(path) != "ok":
        raise BackupError("backup_integrity_failed", "The business database failed SQLite integrity checks.")
    with _open_sqlite_readonly(path) as conn:
        objects = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {row[1] for row in objects if row[0] == "table"}
        indexes = {row[1] for row in objects if row[0] == "index" and row[2] is not None}
        forbidden = {row[0] for row in objects} - {"table", "index"}
        if tables != BUSINESS_TABLES or indexes != BUSINESS_INDEXES or forbidden:
            raise BackupError("backup_schema_invalid", "The business database schema is not allowed.")
        _validate_exact_columns(conn, BUSINESS_COLUMNS)
        if conn.execute("PRAGMA user_version").fetchone()[0] != VAULT_SCHEMA_VERSION:
            raise BackupError("backup_schema_invalid", "The business database schema version is invalid.")
        _validate_user_scope(conn, user_id)


def _validate_checkpoint_database(path: Path, *, allow_empty: bool = False) -> None:
    if _quick_check(path) != "ok":
        raise BackupError("backup_integrity_failed", "The checkpoint database failed SQLite integrity checks.")
    with _open_sqlite_readonly(path) as conn:
        objects = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {row[1] for row in objects if row[0] == "table"}
        explicit_indexes = {row[1] for row in objects if row[0] == "index" and row[2] is not None}
        forbidden = {row[0] for row in objects} - {"table", "index"}
        if allow_empty and not objects:
            return
        if tables != CHECKPOINT_TABLES or explicit_indexes or forbidden:
            raise BackupError("backup_schema_invalid", "The checkpoint database schema is not allowed.")
        _validate_exact_columns(conn, CHECKPOINT_COLUMNS)
        _validate_user_scope(conn, None)


def _validate_exact_columns(
    conn: sqlite3.Connection,
    expected: dict[str, set[str]],
) -> None:
    for table, expected_columns in expected.items():
        actual = {
            row[1]
            for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        if actual != expected_columns:
            raise BackupError(
                "backup_schema_invalid",
                "A database table has unexpected columns.",
            )


def _validate_user_scope(conn: sqlite3.Connection, user_id: str | None) -> None:
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if "user_id" not in columns:
            continue
        values = {row[0] for row in conn.execute(f'SELECT DISTINCT user_id FROM "{table}"')}
        if user_id is None and values:
            raise BackupError("vault_user_scope_invalid", "Unexpected user-scoped checkpoint data.")
        if user_id is not None and any(value != user_id for value in values):
            raise BackupError("vault_user_scope_invalid", "The vault contains data for another user.")


def _migrate_checkpoint_candidate(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        SqliteSaver(conn).setup()


def _database_metadata(path: Path) -> dict[str, Any]:
    return {
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
        "schema_sha256": _schema_hash(path),
    }


def _schema_hash(path: Path) -> str:
    with _open_sqlite_readonly(path) as conn:
        rows = conn.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    normalized = "\n".join(
        "|".join(str(value).strip() for value in row) for row in rows
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _preview_counts(path: Path, timezone_name: str) -> dict[str, int]:
    now = datetime.now(ZoneInfo(timezone_name))
    with _open_sqlite_readonly(path) as conn:
        record_count = conn.execute("SELECT COUNT(*) FROM life_records").fetchone()[0]
        active_count = conn.execute(
            "SELECT COUNT(*) FROM life_records WHERE archived_at IS NULL"
        ).fetchone()[0]
        reminder_rows = conn.execute(
            "SELECT scheduled_at FROM reminders WHERE status IN ('pending', 'snoozed')"
        ).fetchall()
        reminder_count = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
    overdue = 0
    for row in reminder_rows:
        try:
            value = datetime.fromisoformat(row[0])
            if value.tzinfo is None:
                value = value.replace(tzinfo=ZoneInfo(timezone_name))
            if value <= now:
                overdue += 1
        except (TypeError, ValueError):
            continue
    return {
        "record_count": record_count,
        "active_record_count": active_count,
        "archived_record_count": record_count - active_count,
        "reminder_count": reminder_count,
        "overdue_reminder_count": overdue,
    }


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    source_uri = f"file:{source}?mode=ro"
    source_conn = sqlite3.connect(source_uri, uri=True)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    finally:
        destination_conn.close()
        source_conn.close()
    os.chmod(destination, 0o600)


def _prepare_sqlite_for_replacement(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    try:
        with sqlite3.connect(path, timeout=30.0) as conn:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row and int(row[0]) != 0:
                raise BackupError("vault_busy", "SQLite WAL checkpoint is busy.")
    except BackupError:
        raise
    except sqlite3.Error as exc:
        raise BackupError("vault_busy", "SQLite WAL checkpoint failed.") from exc


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            info = sidecar.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise BackupError("vault_path_unsafe", "SQLite sidecar path is unsafe.")
            sidecar.unlink()


def _create_empty_sqlite(path: Path) -> None:
    sqlite3.connect(path).close()
    os.chmod(path, 0o600)


def _quick_check(path: Path) -> str:
    try:
        with _open_sqlite_readonly(path) as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
            return str(row[0]) if row else "failed"
    except sqlite3.Error:
        return "failed"


def _database_user_version(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        with _open_sqlite_readonly(path) as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error:
        return None


@contextmanager
def _open_sqlite_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        conn.close()


def _validate_database_source(path: Path, *, required: bool) -> None:
    if not path.exists():
        if required:
            raise BackupError("backup_not_found", "A required database does not exist.")
        return
    info = _safe_regular_file(path, max_bytes=MAX_DATABASE_BYTES)
    if info.st_nlink != 1:
        raise BackupError("vault_path_unsafe", "Database hard links are not supported.")
    _reject_symlink_components(path)


def _safe_regular_file(path: Path, *, max_bytes: int) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError("backup_not_found", "Backup file not found.") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise BackupError("vault_path_unsafe", "Only regular non-linked files are allowed.")
    if info.st_size > max_bytes:
        raise BackupError("backup_file_too_large", "Backup file exceeds 512 MiB.")
    return info


def _open_safe_read(path: Path, *, max_bytes: int) -> int:
    before = _safe_regular_file(path, max_bytes=max_bytes)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    after = os.fstat(fd)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
    ):
        os.close(fd)
        raise BackupError("vault_path_unsafe", "The file changed while it was opened.")
    return fd


def _ensure_safe_directory(path: Path) -> None:
    _reject_symlink_components(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise BackupError("vault_path_unsafe", "Backup directory is unsafe.")
    os.chmod(path, 0o700)


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise BackupError("vault_path_unsafe", "Symbolic links are not allowed in backup paths.")


def _copy_plain_candidate(source: Path, destination: Path) -> None:
    with source.open("rb") as input_handle:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as output:
            shutil.copyfileobj(input_handle, output, CHUNK_SIZE)
            output.flush()
            os.fsync(output.fileno())
    os.chmod(destination, 0o600)


def _restore_paths(target: Path, operation_id: str) -> tuple[Path, Path]:
    UUID(operation_id)
    return (
        target.with_name(f".{target.name}.{operation_id}.candidate"),
        target.with_name(f".{target.name}.{operation_id}.rollback"),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    fd = _open_safe_read(path, max_bytes=max(MAX_DATABASE_BYTES, MAX_BACKUP_BYTES))
    try:
        while True:
            chunk = os.read(fd, CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise BackupError("backup_integrity_failed", "Backup app version is invalid.")
    return tuple(int(item) for item in match.groups())
