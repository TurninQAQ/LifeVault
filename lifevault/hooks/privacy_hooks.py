from __future__ import annotations

import re


CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
LONG_DIGIT_GROUP = re.compile(r"(?<!\d)(\d[ -]?){13,19}(?!\d)")
CHINESE_ID = re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])")


def sanitize_input(text: str, max_chars: int) -> str:
    cleaned = CONTROL_CHARS.sub("", text).strip()
    cleaned = CHINESE_ID.sub("[REDACTED_ID]", cleaned)
    cleaned = LONG_DIGIT_GROUP.sub("[REDACTED_NUMBER]", cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]
    return cleaned
