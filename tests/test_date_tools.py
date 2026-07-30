from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from lifevault.tools.date_tools import (
    calculate_calendar_month_deadline,
    calculate_deadline,
    calculate_next_renewal_date,
    calculate_reminder_at,
    parse_date_text,
    parse_int,
    parse_subscription_renewal_date,
)


class DateToolsTest(unittest.TestCase):
    def test_relative_date_and_deadline(self) -> None:
        now = datetime(2026, 7, 26, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        purchase_date = parse_date_text("昨天", "Asia/Shanghai", now)
        self.assertEqual(purchase_date.isoformat(), "2026-07-25")
        self.assertEqual(calculate_deadline(purchase_date, 7).isoformat(), "2026-08-01")

    def test_reminder_time(self) -> None:
        reminder_at = calculate_reminder_at(
            deadline=parse_date_text("2026-08-01", "Asia/Shanghai"),
            before_days=2,
            reminder_time="09:00",
            timezone_name="Asia/Shanghai",
        )
        self.assertEqual(reminder_at.isoformat(), "2026-07-30T09:00:00+08:00")

    def test_subscription_renewal_date_expressions(self) -> None:
        now = datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(parse_date_text("下个月15号", "Asia/Shanghai", now).isoformat(), "2026-08-15")
        self.assertEqual(parse_date_text("每月15号", "Asia/Shanghai", now).isoformat(), "2026-08-15")
        self.assertEqual(parse_date_text("每年7月15日", "Asia/Shanghai", now).isoformat(), "2027-07-15")
        self.assertEqual(parse_date_text("明年7月15日", "Asia/Shanghai", now).isoformat(), "2027-07-15")
        self.assertEqual(parse_date_text("每周三", "Asia/Shanghai", now).isoformat(), "2026-07-29")

    def test_subscription_day_only_uses_monthly_cycle(self) -> None:
        now = datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        renewal = parse_subscription_renewal_date("15号", "monthly", "Asia/Shanghai", now)
        self.assertEqual(renewal.isoformat(), "2026-08-15")

    def test_next_renewal_from_last_payment_date(self) -> None:
        now = datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        last_payment = parse_date_text("2026-07-15", "Asia/Shanghai", now)
        renewal = calculate_next_renewal_date(last_payment, "monthly", today=now.date())
        self.assertEqual(renewal.isoformat(), "2026-08-15")

    def test_monthly_renewal_preserves_end_of_month_anchor(self) -> None:
        february = calculate_next_renewal_date(
            date(2026, 1, 31),
            "monthly",
            today=date(2026, 2, 1),
            renewal_anchor=31,
        )
        march = calculate_next_renewal_date(
            february,
            "monthly",
            today=date(2026, 3, 1),
            renewal_anchor=31,
        )

        self.assertEqual(february, date(2026, 2, 28))
        self.assertEqual(march, date(2026, 3, 31))

    def test_yearly_renewal_restores_leap_day_anchor(self) -> None:
        non_leap_year = calculate_next_renewal_date(
            date(2024, 2, 29),
            "yearly",
            today=date(2025, 1, 1),
            renewal_anchor="02-29",
        )
        leap_year = calculate_next_renewal_date(
            date(2027, 2, 28),
            "yearly",
            today=date(2028, 1, 1),
            renewal_anchor="02-29",
        )

        self.assertEqual(non_leap_year, date(2025, 2, 28))
        self.assertEqual(leap_year, date(2028, 2, 29))

    def test_calendar_month_deadline_clamps_month_end(self) -> None:
        self.assertEqual(
            calculate_calendar_month_deadline(date(2027, 1, 31), 1),
            date(2027, 2, 28),
        )
        self.assertEqual(
            calculate_calendar_month_deadline(date(2027, 2, 28), 12),
            date(2028, 2, 28),
        )

    def test_parse_chinese_integer_up_to_reminder_limit(self) -> None:
        self.assertEqual(parse_int("一百二十"), 120)
        self.assertEqual(parse_int("三百六十五"), 365)
        self.assertEqual(parse_int("一百零五"), 105)


if __name__ == "__main__":
    unittest.main()
