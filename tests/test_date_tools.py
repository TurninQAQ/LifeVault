from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from lifevault.tools.date_tools import calculate_deadline, calculate_reminder_at, parse_date_text


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


if __name__ == "__main__":
    unittest.main()
