from __future__ import annotations

import fcntl
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

from lifevault.backup.errors import LockTimeoutError


LockMode = Literal["shared", "exclusive"]
_MANAGERS: dict[Path, "ReentrantFileLock"] = {}
_MANAGERS_GUARD = threading.Lock()


class ReentrantFileLock:
    def __init__(self, path: Path):
        self.path = path
        self._local = threading.RLock()
        self._depth = 0
        self._mode: LockMode | None = None
        self._fd: int | None = None

    @contextmanager
    def acquire(
        self,
        mode: LockMode,
        timeout: float,
        *,
        error_code: str = "vault_busy",
    ) -> Iterator[None]:
        started = time.monotonic()
        if not self._local.acquire(timeout=max(timeout, 0.0)):
            raise LockTimeoutError(error_code, "The requested LifeVault lock is busy.")
        try:
            if self._depth:
                if self._mode == "shared" and mode == "exclusive":
                    raise RuntimeError("Cannot upgrade a shared LifeVault lock in place.")
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return

            self.path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self.path, flags, 0o600)
            os.fchmod(fd, 0o600)
            operation = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
            while True:
                try:
                    fcntl.flock(fd, operation | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() - started >= timeout:
                        os.close(fd)
                        raise LockTimeoutError(
                            error_code,
                            "The requested LifeVault lock is busy.",
                        )
                    time.sleep(0.05)
            self._fd = fd
            self._mode = mode
            self._depth = 1
            try:
                yield
            finally:
                self._depth = 0
                self._mode = None
                self._fd = None
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        finally:
            self._local.release()


def vault_lock_path(database_path: Path) -> Path:
    return Path(f"{database_path}.lock")


def runtime_path(database_path: Path) -> Path:
    return Path(f"{database_path}.runtime.json")


def restore_journal_path(database_path: Path) -> Path:
    return Path(f"{database_path}.restore-journal.json")


def crypto_lock_path(database_path: Path) -> Path:
    return Path(f"{database_path}.backup-crypto.lock")


def get_file_lock(path: Path) -> ReentrantFileLock:
    resolved = path.absolute()
    with _MANAGERS_GUARD:
        manager = _MANAGERS.get(resolved)
        if manager is None:
            manager = ReentrantFileLock(resolved)
            _MANAGERS[resolved] = manager
        return manager


def get_vault_lock(database_path: Path) -> ReentrantFileLock:
    return get_file_lock(vault_lock_path(database_path))


def get_crypto_lock(database_path: Path) -> ReentrantFileLock:
    return get_file_lock(crypto_lock_path(database_path))
