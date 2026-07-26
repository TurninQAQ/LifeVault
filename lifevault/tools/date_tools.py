from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


CN_NUMBERS = {
    "零": 0,
    "一": 1,
    "两": 2,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def now_in_timezone(timezone_name: str) -> datetime:
    return datetime.now(ZoneInfo(timezone_name))


def parse_int(text: str | int | None) -> int | None:
    if text is None:
        return None
    if isinstance(text, int):
        return text
    raw = str(text).strip()
    if raw.isdigit():
        return int(raw)
    if raw in CN_NUMBERS:
        return CN_NUMBERS[raw]
    if len(raw) == 2 and raw[0] == "十" and raw[1] in CN_NUMBERS:
        return 10 + CN_NUMBERS[raw[1]]
    if len(raw) == 2 and raw[1] == "十" and raw[0] in CN_NUMBERS:
        return CN_NUMBERS[raw[0]] * 10
    if len(raw) == 3 and raw[1] == "十" and raw[0] in CN_NUMBERS and raw[2] in CN_NUMBERS:
        return CN_NUMBERS[raw[0]] * 10 + CN_NUMBERS[raw[2]]
    return None


def parse_date_text(text: str | None, timezone_name: str, now: datetime | None = None) -> date | None:
    if not text:
        return None
    base = now or now_in_timezone(timezone_name)
    today = base.date()
    raw = text.strip()

    exact = _parse_exact_date(raw, today.year)
    if exact:
        return exact

    relative_days = {
        "今天": 0,
        "今日": 0,
        "昨天": -1,
        "昨日": -1,
        "前天": -2,
        "明天": 1,
        "明日": 1,
        "后天": 2,
    }
    if raw in relative_days:
        return today + timedelta(days=relative_days[raw])

    match = re.search(r"([一二两三四五六七八九十\d]+)\s*天前", raw)
    if match:
        days = parse_int(match.group(1))
        return today - timedelta(days=days) if days is not None else None

    match = re.search(r"([一二两三四五六七八九十\d]+)\s*天后", raw)
    if match:
        days = parse_int(match.group(1))
        return today + timedelta(days=days) if days is not None else None

    if "下周" in raw:
        weekday = _parse_weekday(raw)
        if weekday is not None:
            days_until_next_week = 7 - today.weekday()
            return today + timedelta(days=days_until_next_week + weekday)

    if "本周" in raw or "这周" in raw:
        weekday = _parse_weekday(raw)
        if weekday is not None:
            delta = weekday - today.weekday()
            return today + timedelta(days=delta)

    if raw in {"月底", "本月底", "这个月底"}:
        next_month = today.replace(day=28) + timedelta(days=4)
        return next_month - timedelta(days=next_month.day)

    return None


def calculate_deadline(event_date: date, days: int) -> date:
    if days < 0:
        raise ValueError("days must be non-negative")
    return event_date + timedelta(days=days)


def calculate_reminder_at(
    deadline: date,
    before_days: int,
    reminder_time: str,
    timezone_name: str,
) -> datetime:
    if before_days < 0:
        raise ValueError("before_days must be non-negative")
    hour, minute = _parse_time(reminder_time)
    scheduled_date = deadline - timedelta(days=before_days)
    return datetime.combine(
        scheduled_date,
        time(hour=hour, minute=minute),
        tzinfo=ZoneInfo(timezone_name),
    )


def _parse_exact_date(raw: str, default_year: int) -> date | None:
    normalized = raw.replace("年", "-").replace("月", "-").replace("日", "")
    normalized = normalized.replace("/", "-").replace(".", "-")
    for pattern in ("%Y-%m-%d", "%Y-%m", "%m-%d"):
        try:
            if pattern == "%m-%d":
                parsed = datetime.strptime(normalized, pattern).date()
                return parsed.replace(year=default_year)
            if pattern == "%Y-%m":
                parsed = datetime.strptime(normalized, pattern).date()
                return parsed.replace(day=1)
            return datetime.strptime(normalized, pattern).date()
        except ValueError:
            continue
    return None


def _parse_time(raw: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{1,2}))?\s*", raw or "")
    if not match:
        raise ValueError(f"Invalid reminder time: {raw}")
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid reminder time: {raw}")
    return hour, minute


def _parse_weekday(raw: str) -> int | None:
    mapping = {
        "一": 0,
        "二": 1,
        "三": 2,
        "四": 3,
        "五": 4,
        "六": 5,
        "日": 6,
        "天": 6,
    }
    for key, value in mapping.items():
        if f"周{key}" in raw or f"星期{key}" in raw:
            return value
    return None
