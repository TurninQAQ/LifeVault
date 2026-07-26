from __future__ import annotations

import hashlib


def stable_key(*parts: object) -> str:
    joined = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
