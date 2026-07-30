from __future__ import annotations

import calendar
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
    raw = _compact_date_text(text)

    exact = _parse_exact_date(raw, today.year)
    if exact:
        return exact

    next_month_day = _parse_next_month_day(raw, today)
    if next_month_day:
        return next_month_day

    next_year_day = _parse_next_year_day(raw, today)
    if next_year_day:
        return next_year_day

    monthly_anchor = _parse_monthly_anchor(raw, today)
    if monthly_anchor:
        return monthly_anchor

    yearly_anchor = _parse_yearly_anchor(raw, today)
    if yearly_anchor:
        return yearly_anchor

    weekly_anchor = _parse_weekly_anchor(raw, today)
    if weekly_anchor:
        return weekly_anchor

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


def parse_subscription_renewal_date(
    text: str | None,
    billing_cycle: str | None,
    timezone_name: str,
    now: datetime | None = None,
) -> date | None:
    if not text:
        return None
    base = now or now_in_timezone(timezone_name)
    today = base.date()
    raw = _compact_date_text(text)
    cycle = normalize_billing_cycle(billing_cycle)

    if cycle == "monthly":
        day_only = _parse_day_only(raw)
        if day_only is not None:
            return _date_for_month_anchor(today, day_only)

    parsed = parse_date_text(raw, timezone_name, base)
    if parsed and parsed < today and _looks_like_month_day_without_year(raw):
        return _next_year_date_for_anchor(today, parsed.month, parsed.day)
    return parsed


def calculate_next_renewal_date(
    anchor_date: date,
    billing_cycle: str | None,
    today: date | None = None,
    renewal_anchor: int | str | None = None,
) -> date | None:
    cycle = normalize_billing_cycle(billing_cycle)
    if cycle is None:
        return None
    minimum = today or date.today()
    candidate = _advance_cycle(anchor_date, cycle, renewal_anchor)
    while candidate < minimum:
        candidate = _advance_cycle(candidate, cycle, renewal_anchor)
    return candidate


def normalize_billing_cycle(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in {"monthly", "yearly", "weekly"}:
        return normalized
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
    normalized = _compact_date_text(raw)
    normalized = normalized.replace("年", "-").replace("月", "-").replace("日", "").replace("号", "")
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


def _compact_date_text(raw: str) -> str:
    return re.sub(r"\s+", "", raw.strip())


def _parse_next_month_day(raw: str, today: date) -> date | None:
    match = re.search(r"(?:下个?月|下月)([一二两三四五六七八九十\d]+)[日号]", raw)
    if not match:
        return None
    day = _parse_day(match.group(1))
    if day is None:
        return None
    next_month = _add_months(today.replace(day=1), 1)
    return _safe_date(next_month.year, next_month.month, day)


def _parse_next_year_day(raw: str, today: date) -> date | None:
    match = re.search(r"(?:明年|下年|下一年)([一二两三四五六七八九十\d]+)月([一二两三四五六七八九十\d]+)[日号]", raw)
    if not match:
        return None
    month = _parse_month(match.group(1))
    day = _parse_day(match.group(2))
    if month is None or day is None:
        return None
    return _safe_date(today.year + 1, month, day)


def _parse_monthly_anchor(raw: str, today: date) -> date | None:
    patterns = [
        r"(?:每个?月|月付|包月|月度|按月).*?([一二两三四五六七八九十\d]+)[日号]",
        r"([一二两三四五六七八九十\d]+)[日号].*?(?:每个?月|月付|包月|月度|按月|自动续费|自动扣款|扣款|续费)",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw)
        if match:
            day = _parse_day(match.group(1))
            return _date_for_month_anchor(today, day) if day is not None else None
    return None


def _parse_yearly_anchor(raw: str, today: date) -> date | None:
    patterns = [
        r"(?:每年|每一年|年付|包年|年度|按年).*?([一二两三四五六七八九十\d]+)月([一二两三四五六七八九十\d]+)[日号]",
        r"([一二两三四五六七八九十\d]+)月([一二两三四五六七八九十\d]+)[日号].*?(?:每年|每一年|年付|包年|年度|按年)",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw)
        if match:
            month = _parse_month(match.group(1))
            day = _parse_day(match.group(2))
            if month is None or day is None:
                return None
            return _date_for_year_anchor(today, month, day)
    return None


def _parse_weekly_anchor(raw: str, today: date) -> date | None:
    if "每周" not in raw and "每星期" not in raw:
        return None
    weekday = _parse_weekday(raw)
    if weekday is None:
        return None
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def _date_for_month_anchor(today: date, day: int) -> date | None:
    current = _safe_date(today.year, today.month, day)
    if current and current >= today:
        return current
    next_month = _add_months(today.replace(day=1), 1)
    return _safe_date(next_month.year, next_month.month, day)


def _date_for_year_anchor(today: date, month: int, day: int) -> date | None:
    current = _safe_date(today.year, month, day)
    if current and current >= today:
        return current
    return _safe_date(today.year + 1, month, day)


def _next_year_date_for_anchor(today: date, month: int, day: int) -> date | None:
    current = _safe_date(today.year, month, day)
    if current and current >= today:
        return current
    return _safe_date(today.year + 1, month, day)


def _parse_day_only(raw: str) -> int | None:
    match = re.fullmatch(r"([一二两三四五六七八九十\d]+)[日号]", raw)
    return _parse_day(match.group(1)) if match else None


def _looks_like_month_day_without_year(raw: str) -> bool:
    return bool(re.fullmatch(r"[一二两三四五六七八九十\d]+月[一二两三四五六七八九十\d]+[日号]?", raw))


def _parse_day(raw: str) -> int | None:
    value = parse_int(raw)
    if value is None or not 1 <= value <= 31:
        return None
    return value


def _parse_month(raw: str) -> int | None:
    value = parse_int(raw)
    if value is None or not 1 <= value <= 12:
        return None
    return value


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _advance_cycle(value: date, cycle: str, renewal_anchor: int | str | None = None) -> date:
    if cycle == "weekly":
        return value + timedelta(days=7)
    if cycle == "monthly":
        day = renewal_anchor if isinstance(renewal_anchor, int) and 1 <= renewal_anchor <= 31 else value.day
        return _add_months(value, 1, day=day)
    if cycle == "yearly":
        month, day = _parse_yearly_renewal_anchor(renewal_anchor, value)
        return _add_months(value, 12, month=month, day=day)
    raise ValueError(f"Unsupported billing cycle: {cycle}")


def _add_months(
    value: date,
    months: int,
    *,
    month: int | None = None,
    day: int | None = None,
) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    target_month = month or month_index % 12 + 1
    target_day = min(day or value.day, calendar.monthrange(year, target_month)[1])
    return date(year, target_month, target_day)


def _parse_yearly_renewal_anchor(renewal_anchor: int | str | None, fallback: date) -> tuple[int, int]:
    if isinstance(renewal_anchor, str):
        match = re.fullmatch(r"(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])", renewal_anchor)
        if match:
            month = int(match.group(1))
            day = int(match.group(2))
            if day <= calendar.monthrange(2000, month)[1]:
                return month, day
    return fallback.month, fallback.day


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
