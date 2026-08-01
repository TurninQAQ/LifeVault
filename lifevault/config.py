from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent


def _default_data_dir() -> Path:
    # Keep source-checkout upgrades on the existing ./data directory, while an
    # installed wheel writes to a user-owned location instead of site-packages.
    if (PROJECT_ROOT / "pyproject.toml").is_file() and (PROJECT_ROOT / ".git").exists():
        return PROJECT_ROOT / "data"
    if sys.platform == "win32" and os.getenv("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "LifeVault"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LifeVault"
    return Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "lifevault"


DEFAULT_DATA_DIR = _default_data_dir()


@dataclass(frozen=True)
class Settings:
    database_path: Path = DEFAULT_DATA_DIR / "lifevault.db"
    langgraph_checkpoint_path: Path = DEFAULT_DATA_DIR / "langgraph_checkpoints.sqlite"
    backup_dir: Path = DEFAULT_DATA_DIR / "backups"
    qwen_base_url: str = "http://127.0.0.1:8008/v1"
    qwen_model: str = "qwen-enterprise-agent"
    qwen_timeout_seconds: int = 60
    default_user_id: str = "local"
    default_timezone: str = "Asia/Shanghai"
    default_reminder_time: str = "09:00"
    default_advance_days: int = 2
    input_max_chars: int = 4000
    use_qwen: bool = True


def get_settings() -> Settings:
    data_dir = Path(os.getenv("LIFEVAULT_HOME", DEFAULT_DATA_DIR)).expanduser()
    database_path = Path(
        os.getenv("LIFEVAULT_DB", data_dir / "lifevault.db")
    ).expanduser()
    return Settings(
        database_path=database_path,
        langgraph_checkpoint_path=Path(
            os.getenv(
                "LIFEVAULT_LANGGRAPH_DB",
                database_path.parent / "langgraph_checkpoints.sqlite",
            )
        ).expanduser(),
        backup_dir=Path(
            os.getenv("LIFEVAULT_BACKUP_DIR", database_path.parent / "backups")
        ).expanduser(),
        qwen_base_url=os.getenv("LIFEVAULT_QWEN_BASE_URL", "http://127.0.0.1:8008/v1"),
        qwen_model=os.getenv("LIFEVAULT_QWEN_MODEL", "qwen-enterprise-agent"),
        qwen_timeout_seconds=int(os.getenv("LIFEVAULT_QWEN_TIMEOUT", "60")),
        default_user_id=os.getenv("LIFEVAULT_USER_ID", "local"),
        default_timezone=os.getenv("LIFEVAULT_TIMEZONE", "Asia/Shanghai"),
        default_reminder_time=os.getenv("LIFEVAULT_DEFAULT_REMINDER_TIME", "09:00"),
        default_advance_days=int(os.getenv("LIFEVAULT_DEFAULT_ADVANCE_DAYS", "2")),
        input_max_chars=int(os.getenv("LIFEVAULT_INPUT_MAX_CHARS", "4000")),
        use_qwen=os.getenv("LIFEVAULT_USE_QWEN", "1") not in {"0", "false", "False"},
    )
