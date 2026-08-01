from __future__ import annotations


class BackupError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class LockTimeoutError(BackupError):
    pass
