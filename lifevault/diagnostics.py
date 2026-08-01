from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import quote

import requests
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from lifevault import __version__
from lifevault.backup.locking import restore_journal_path, runtime_path
from lifevault.backup.runtime import RuntimeStateStore
from lifevault.config import Settings
from lifevault.eval.runner import DEFAULT_EXAMPLES_PATH
from lifevault.eval.update_runner import DEFAULT_UPDATE_EXAMPLES_PATH
from lifevault.skills import SUPPORTED_SKILLS, load_skill
from lifevault.storage.database import VAULT_SCHEMA_VERSION


DIRECT_REQUIREMENTS = {
    "pydantic": ">=2.7,<3",
    "requests": ">=2.31,<3",
    "streamlit": ">=1.36,<2",
    "plyer": ">=2.1,<3",
    "langgraph": ">=1.0,<2",
    "langgraph-checkpoint-sqlite": ">=3.1,<4",
    "mcp": ">=1.28,<2",
    "cryptography": ">=46.0.3,<50",
    "packaging": ">=24,<27",
}


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class DiagnosticReport:
    version: str
    checks: tuple[DiagnosticCheck, ...]

    @property
    def failed(self) -> int:
        return sum(check.status == "failed" for check in self.checks)

    @property
    def warnings(self) -> int:
        return sum(check.status == "warning" for check in self.checks)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "ok": self.ok,
            "failed": self.failed,
            "warnings": self.warnings,
            "checks": [asdict(check) for check in self.checks],
        }


def run_diagnostics(
    settings: Settings,
    *,
    check_qwen: bool = True,
) -> DiagnosticReport:
    checks = [
        _check_python(),
        _check_platform(),
        _check_dependencies(),
        _check_packaged_resources(),
        _check_writable_parent("vault_directory", settings.database_path),
        _check_writable_parent("checkpoint_directory", settings.langgraph_checkpoint_path),
        _check_writable_parent("backup_directory", settings.backup_dir / "probe.lvbackup"),
        _check_sqlite_database(settings.database_path, business=True),
        _check_sqlite_database(settings.langgraph_checkpoint_path, business=False),
        _check_restore_journal(settings.database_path),
        _check_runtime_state(settings.database_path),
        _check_qwen(settings, enabled=check_qwen),
    ]
    return DiagnosticReport(version=__version__, checks=tuple(checks))


def format_diagnostics(report: DiagnosticReport) -> str:
    labels = {"ok": "OK", "warning": "WARN", "failed": "FAIL"}
    lines = [f"LifeVault doctor {report.version}"]
    lines.extend(
        f"[{labels[check.status]}] {check.name}: {check.detail}"
        for check in report.checks
    )
    readiness = "ready" if report.ok else "not ready"
    lines.append(
        f"Result: {readiness}; failures={report.failed}; warnings={report.warnings}"
    )
    return "\n".join(lines)


def _check_python() -> DiagnosticCheck:
    current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 10):
        return DiagnosticCheck("python", "failed", f"Python {current}; 3.10+ required")
    return DiagnosticCheck("python", "ok", f"Python {current}")


def _check_platform() -> DiagnosticCheck:
    if os.name != "posix":
        return DiagnosticCheck(
            "platform",
            "failed",
            "v1 encrypted backup locking requires a POSIX platform",
        )
    return DiagnosticCheck(
        "platform",
        "ok",
        f"{platform.system()} {platform.machine()} with POSIX file locking",
    )


def _check_dependencies() -> DiagnosticCheck:
    installed: list[str] = []
    missing: list[str] = []
    unsupported: list[str] = []
    for distribution, requirement in DIRECT_REQUIREMENTS.items():
        try:
            installed_version = version(distribution)
        except PackageNotFoundError:
            missing.append(distribution)
            continue
        installed.append(f"{distribution}={installed_version}")
        try:
            supported = Version(installed_version) in SpecifierSet(requirement)
        except InvalidVersion:
            supported = False
        if not supported:
            unsupported.append(f"{distribution}={installed_version} ({requirement})")
    if missing:
        return DiagnosticCheck(
            "dependencies",
            "failed",
            f"missing direct dependencies: {', '.join(missing)}",
        )
    if unsupported:
        return DiagnosticCheck(
            "dependencies",
            "failed",
            f"unsupported direct dependencies: {', '.join(unsupported)}",
        )
    return DiagnosticCheck("dependencies", "ok", ", ".join(installed))


def _check_packaged_resources() -> DiagnosticCheck:
    try:
        for skill in sorted(SUPPORTED_SKILLS):
            load_skill(skill)
        extraction_cases = _count_jsonl(DEFAULT_EXAMPLES_PATH)
        update_cases = _count_jsonl(DEFAULT_UPDATE_EXAMPLES_PATH)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return DiagnosticCheck("packaged_resources", "failed", str(exc))
    if extraction_cases < 50 or update_cases < 1:
        return DiagnosticCheck(
            "packaged_resources",
            "failed",
            f"unexpected eval sizes: extraction={extraction_cases}, update={update_cases}",
        )
    return DiagnosticCheck(
        "packaged_resources",
        "ok",
        f"skills={len(SUPPORTED_SKILLS)}, extraction_cases={extraction_cases}, "
        f"update_cases={update_cases}",
    )


def _check_writable_parent(name: str, target: Path) -> DiagnosticCheck:
    parent = target.expanduser().absolute().parent
    existing = parent
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not existing.is_dir():
        return DiagnosticCheck(name, "failed", f"no existing parent for {parent}")
    if not os.access(existing, os.W_OK | os.X_OK):
        return DiagnosticCheck(name, "failed", f"not writable: {existing}")
    return DiagnosticCheck(name, "ok", f"writable through {existing}")


def _check_sqlite_database(path: Path, *, business: bool) -> DiagnosticCheck:
    name = "vault_database" if business else "checkpoint_database"
    if not path.exists():
        return DiagnosticCheck(name, "warning", f"not initialized: {path}")
    if not path.is_file() or path.is_symlink():
        return DiagnosticCheck(name, "failed", f"not a regular database file: {path}")
    try:
        with sqlite3.connect(_read_only_sqlite_uri(path), uri=True) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return DiagnosticCheck(name, "failed", f"cannot read database: {exc}")
    if quick_check != "ok":
        return DiagnosticCheck(name, "failed", f"SQLite quick_check={quick_check}")
    if business and user_version > VAULT_SCHEMA_VERSION:
        return DiagnosticCheck(
            name,
            "failed",
            f"schema version {user_version} is newer than supported {VAULT_SCHEMA_VERSION}",
        )
    if business and user_version < VAULT_SCHEMA_VERSION:
        return DiagnosticCheck(
            name,
            "warning",
            f"legacy schema version {user_version}; startup migration required",
        )
    detail = f"quick_check=ok, schema_version={user_version}" if business else "quick_check=ok"
    return DiagnosticCheck(name, "ok", detail)


def _check_restore_journal(database_path: Path) -> DiagnosticCheck:
    journal = restore_journal_path(database_path)
    if journal.exists():
        return DiagnosticCheck(
            "restore_recovery",
            "failed",
            f"pending restore journal requires normal LifeVault startup: {journal}",
        )
    return DiagnosticCheck("restore_recovery", "ok", "no pending restore journal")


def _check_runtime_state(database_path: Path) -> DiagnosticCheck:
    path = runtime_path(database_path)
    if not path.exists():
        return DiagnosticCheck("worker_runtime", "ok", "runtime state not created yet")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        state = RuntimeStateStore(database_path)._parse(raw)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return DiagnosticCheck("worker_runtime", "failed", f"invalid runtime state: {exc}")
    if state.worker_paused_after_restore:
        return DiagnosticCheck(
            "worker_runtime",
            "warning",
            f"Reminder Worker paused: {state.pause_reason or 'unspecified'}",
        )
    return DiagnosticCheck("worker_runtime", "ok", "Reminder Worker is enabled")


def _check_qwen(settings: Settings, *, enabled: bool) -> DiagnosticCheck:
    if not enabled:
        return DiagnosticCheck("local_qwen", "ok", "network check skipped")
    if not settings.use_qwen:
        return DiagnosticCheck("local_qwen", "warning", "disabled; fallback extractor is active")
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            f"{settings.qwen_base_url.rstrip('/')}/models",
            timeout=min(settings.qwen_timeout_seconds, 5),
        )
        response.raise_for_status()
        payload = response.json()
        model_ids = {
            str(item.get("id"))
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        }
    except (requests.RequestException, TypeError, ValueError, json.JSONDecodeError) as exc:
        return DiagnosticCheck(
            "local_qwen",
            "warning",
            f"unavailable; fallback extractor will be used: {exc}",
        )
    if settings.qwen_model not in model_ids:
        return DiagnosticCheck(
            "local_qwen",
            "warning",
            f"configured model {settings.qwen_model!r} not listed by the endpoint",
        )
    return DiagnosticCheck("local_qwen", "ok", f"model available: {settings.qwen_model}")


def _count_jsonl(path: Path) -> int:
    count = 0
    identifiers: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                raise ValueError(f"invalid JSONL case at {path}:{line_number}")
            if value["id"] in identifiers:
                raise ValueError(f"duplicate JSONL id at {path}:{line_number}")
            identifiers.add(value["id"])
            count += 1
    return count


def _read_only_sqlite_uri(path: Path) -> str:
    return f"file:{quote(str(path.expanduser().absolute()), safe='/')}?mode=ro"
