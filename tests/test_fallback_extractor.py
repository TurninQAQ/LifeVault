from __future__ import annotations

import unittest
from datetime import datetime

from lifevault.models.llm_factory import FallbackExtractor
from lifevault.models.schemas import RecordType


class FallbackExtractorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = FallbackExtractor()
        self.now = datetime(2026, 7, 27, 9, 0, 0)

    def test_subscription_service_usd_and_renewal_text(self) -> None:
        candidate = self.extractor.extract_record(
            "我开通了 ChatGPT Plus，每月 20 美元，下个月 3 号扣款，提前 1 天提醒。",
            self.now,
        )

        self.assertEqual(candidate.record_type, RecordType.SUBSCRIPTION)
        self.assertEqual(candidate.title, "ChatGPT Plus")
        self.assertEqual(candidate.service_name, "ChatGPT Plus")
        self.assertEqual(candidate.amount, 20)
        self.assertEqual(candidate.billing_cycle, "monthly")
        self.assertEqual(candidate.next_renewal_text, "下个月3号")
        self.assertEqual(candidate.remind_before_days, 1)

    def test_bill_names_and_due_text(self) -> None:
        parking = self.extractor.extract_record("车位管理费 300 元，2026 年 9 月 30 日截止。", self.now)
        mortgage = self.extractor.extract_record("房贷这个月 8200 元，月底前还款，提前三天提醒我。", self.now)
        parking_fee = self.extractor.extract_record("停车费 80 元，今天扣款。", self.now)

        self.assertEqual(parking.record_type, RecordType.BILL)
        self.assertEqual(parking.title, "车位管理费")
        self.assertEqual(parking.bill_name, "车位管理费")
        self.assertEqual(parking.due_date_text, "2026年9月30日")

        self.assertEqual(mortgage.record_type, RecordType.BILL)
        self.assertEqual(mortgage.title, "房贷")
        self.assertEqual(mortgage.bill_name, "房贷")
        self.assertEqual(mortgage.billing_period, "monthly")
        self.assertEqual(mortgage.due_date_text, "月底")
        self.assertEqual(mortgage.remind_before_days, 3)

        self.assertEqual(parking_fee.record_type, RecordType.BILL)
        self.assertEqual(parking_fee.title, "停车费")
        self.assertEqual(parking_fee.due_date_text, "今天")

    def test_purchase_warranty_and_targeted_reminders(self) -> None:
        candidate = self.extractor.extract_record(
            "我 2026-07-25 买了一个相机，5000 元，七天退货，保修两年，"
            "退货前 2 天、保修到期前 60 天提醒我，晚上 18:30 提醒。",
            self.now,
        )

        self.assertEqual(candidate.return_days, 7)
        self.assertEqual(candidate.warranty_months, 24)
        self.assertTrue(candidate.return_reminder_requested)
        self.assertTrue(candidate.warranty_reminder_requested)
        self.assertEqual(candidate.return_remind_before_days, 2)
        self.assertEqual(candidate.warranty_remind_before_days, 60)
        self.assertEqual(candidate.reminder_time, "18:30")

    def test_purchase_merchant_and_title_with_spaces(self) -> None:
        apple = self.extractor.extract_record(
            "在 Apple Store 买了一个 iPad，3999 元，订单号 APPLE-IPAD-01，14 天可退。",
            self.now,
        )
        pdd = self.extractor.extract_record(
            "前天从拼多多购买了一台显示器，1299 元，订单号 PDD-7788，七天退货，退货前两天提醒。",
            self.now,
        )

        self.assertEqual(apple.record_type, RecordType.PURCHASE)
        self.assertEqual(apple.merchant, "Apple Store")
        self.assertEqual(apple.title, "iPad")

        self.assertEqual(pdd.record_type, RecordType.PURCHASE)
        self.assertEqual(pdd.merchant, "拼多多")
        self.assertEqual(pdd.title, "显示器")


if __name__ == "__main__":
    unittest.main()
