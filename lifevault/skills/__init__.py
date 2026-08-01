from __future__ import annotations

from functools import lru_cache
from importlib.resources import files


SUPPORTED_SKILLS = frozenset({"purchase", "subscription", "bill"})
MAX_SKILL_CHARS = 8_000


@lru_cache(maxsize=len(SUPPORTED_SKILLS))
def load_skill(record_type: str) -> str:
    """Load one trusted, packaged extraction skill by record type."""
    if record_type not in SUPPORTED_SKILLS:
        raise ValueError(f"Unsupported LifeVault skill: {record_type}")

    content = (
        files("lifevault.skills")
        .joinpath(record_type, "SKILL.md")
        .read_text(encoding="utf-8")
        .strip()
    )
    if not content or len(content) > MAX_SKILL_CHARS:
        raise RuntimeError(f"Invalid packaged LifeVault skill: {record_type}")
    return content
