from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from lifevault.backup.locking import runtime_path


RUNTIME_STATE_VERSION = 1


@dataclass(frozen=True)
class RuntimeState:
    version: int
    vault_generation: str
    worker_paused_after_restore: bool
    pause_backup_id: str | None = None
    pause_reason: str | None = None


class RuntimeStateStore:
    def __init__(self, database_path: Path):
        self.path = runtime_path(database_path)

    def load(self) -> tuple[RuntimeState, bool]:
        if not self.path.exists():
            state = self._new_state(paused=False)
            self.write(state)
            return state, False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            state = self._parse(raw)
            return state, False
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            state = self._new_state(paused=True, reason="runtime_state_invalid")
            self.write(state)
            return state, True

    def generation(self) -> str:
        return self.load()[0].vault_generation

    def pause_after_restore(self, backup_id: str) -> RuntimeState:
        state = self._new_state(
            paused=True,
            backup_id=backup_id,
            reason="restored_backup",
        )
        self.write(state)
        return state

    def resume_worker(self) -> RuntimeState:
        current, _ = self.load()
        state = RuntimeState(
            version=RUNTIME_STATE_VERSION,
            vault_generation=current.vault_generation,
            worker_paused_after_restore=False,
        )
        self.write(state)
        return state

    def force_pause(self, reason: str) -> RuntimeState:
        state = self._new_state(paused=True, reason=reason)
        self.write(state)
        return state

    def write(self, state: RuntimeState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.{uuid4()}.tmp")
        payload = json.dumps(
            asdict(state),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            os.chmod(self.path, 0o600)
            _fsync_directory(self.path.parent)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _new_state(
        self,
        *,
        paused: bool,
        backup_id: str | None = None,
        reason: str | None = None,
    ) -> RuntimeState:
        return RuntimeState(
            version=RUNTIME_STATE_VERSION,
            vault_generation=str(uuid4()),
            worker_paused_after_restore=paused,
            pause_backup_id=backup_id,
            pause_reason=reason,
        )

    def _parse(self, raw: Any) -> RuntimeState:
        if not isinstance(raw, dict) or set(raw) != {
            "version",
            "vault_generation",
            "worker_paused_after_restore",
            "pause_backup_id",
            "pause_reason",
        }:
            raise ValueError("Invalid runtime state fields.")
        if raw["version"] != RUNTIME_STATE_VERSION:
            raise ValueError("Unsupported runtime state version.")
        UUID(raw["vault_generation"])
        backup_id = raw["pause_backup_id"]
        if backup_id is not None:
            UUID(backup_id)
        if not isinstance(raw["worker_paused_after_restore"], bool):
            raise ValueError("Invalid Worker pause state.")
        reason = raw["pause_reason"]
        if reason is not None and reason not in {
            "restored_backup",
            "runtime_state_invalid",
            "restore_recovery",
            "restore_recovery_audit_pending",
        }:
            raise ValueError("Invalid Worker pause reason.")
        return RuntimeState(**raw)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
