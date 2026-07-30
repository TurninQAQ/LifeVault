from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lifevault.agent.corrections import apply_candidate_corrections
from lifevault.agent.service import LifeVaultAgent
from lifevault.config import Settings
from lifevault.models.schemas import ExtractedRecordCandidate, RecordType
from lifevault.storage.repository import VaultRepository


class CandidateCorrectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = Settings(
            database_path=Path(self.tmp.name) / "corrections.db",
            langgraph_checkpoint_path=Path(self.tmp.name) / "graph.db",
            use_qwen=False,
        )
        self.service = LifeVaultAgent(settings, VaultRepository(settings.database_path))
        self.now = datetime(2026, 7, 30, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_purchase_fields_can_be_changed_and_optional_text_cleared(self) -> None:
        base = self.purchase_candidate(merchant="京东")
        result = self.apply(base, {"amount": 5200, "merchant": None, "return_days": 14})

        self.assertEqual(result.field_errors, {})
        self.assertEqual(result.candidate.amount, 5200)
        self.assertIsNone(result.candidate.merchant)
        self.assertEqual(result.candidate.return_days, 14)

    def test_wrong_record_type_field_is_rejected_atomically(self) -> None:
        base = self.purchase_candidate()
        result = self.apply(base, {"amount": 5200, "billing_cycle": "monthly"})

        self.assertIn("billing_cycle", result.field_errors)
        self.assertEqual(result.candidate.amount, 5000)

    def test_invalid_value_rejects_the_entire_batch(self) -> None:
        base = self.purchase_candidate()
        result = self.apply(base, {"amount": 5200, "return_days": 5000})

        self.assertIn("return_days", result.field_errors)
        self.assertEqual(result.candidate.amount, 5000)
        self.assertEqual(result.candidate.return_days, 7)

    def test_numeric_and_boolean_fields_reject_string_coercion(self) -> None:
        numeric = self.apply(self.purchase_candidate(), {"return_days": "14"})
        boolean = self.apply(
            self.purchase_candidate(),
            {"return_reminder_requested": "true"},
        )

        self.assertIn("return_days", numeric.field_errors)
        self.assertIn("return_reminder_requested", boolean.field_errors)

    def test_required_value_cannot_be_cleared(self) -> None:
        result = self.apply(self.purchase_candidate(), {"title": None})

        self.assertIn("title", result.field_errors)
        self.assertEqual(result.candidate.title, "相机")

    def test_currency_is_normalized_and_negative_amount_is_rejected(self) -> None:
        normalized = self.apply(self.purchase_candidate(), {"currency": "usd"})
        rejected = self.apply(self.purchase_candidate(), {"amount": -1})

        self.assertEqual(normalized.candidate.currency, "USD")
        self.assertIn("amount", rejected.field_errors)

    def test_return_deadline_cannot_precede_purchase(self) -> None:
        result = self.apply(
            self.purchase_candidate(),
            {"return_deadline_date": "2026-07-24"},
        )

        self.assertIn("return_deadline_date", result.field_errors)

    def test_warranty_deadline_cannot_precede_purchase(self) -> None:
        result = self.apply(
            self.purchase_candidate(warranty_months=12),
            {"warranty_deadline_date": "2026-07-24"},
        )

        self.assertIn("warranty_deadline_date", result.field_errors)

    def test_subscription_renewal_cannot_precede_event_date(self) -> None:
        candidate = ExtractedRecordCandidate(
            intent="create_record",
            record_type=RecordType.SUBSCRIPTION,
            title="视频会员",
            service_name="视频会员",
            amount=30,
            event_date=date(2026, 7, 25),
            next_renewal_date=date(2026, 8, 25),
            billing_cycle="monthly",
        )
        result = self.apply(candidate, {"next_renewal_date": "2026-07-24"})

        self.assertIn("next_renewal_date", result.field_errors)

    def test_overdue_bill_is_valid(self) -> None:
        candidate = ExtractedRecordCandidate(
            intent="create_record",
            record_type=RecordType.BILL,
            title="房租",
            bill_name="房租",
            amount=3000,
            due_date=date(2026, 7, 1),
        )
        result = self.apply(candidate, {"billing_period": "2026-07"})

        self.assertEqual(result.field_errors, {})
        self.assertEqual(result.candidate.billing_period, "2026-07")

    def test_targeted_reminder_requires_its_own_deadline(self) -> None:
        base = self.purchase_candidate(return_days=None, warranty_months=12)
        result = self.apply(base, {"return_reminder_requested": True})

        self.assertIn("return_deadline_date", result.field_errors)

    def test_subscription_service_name_can_be_edited_independently(self) -> None:
        candidate = ExtractedRecordCandidate(
            intent="create_record",
            record_type=RecordType.SUBSCRIPTION,
            title="旧名称",
            service_name="旧名称",
            amount=30,
            next_renewal_date=date(2026, 8, 25),
        )
        result = self.apply(candidate, {"service_name": "新服务"})

        self.assertEqual(result.field_errors, {})
        self.assertEqual(result.candidate.title, "旧名称")
        self.assertEqual(result.candidate.service_name, "新服务")

    def test_bill_name_can_be_edited_independently(self) -> None:
        candidate = ExtractedRecordCandidate(
            intent="create_record",
            record_type=RecordType.BILL,
            title="旧账单",
            bill_name="旧账单",
            amount=100,
            due_date=date(2026, 8, 1),
        )
        result = self.apply(candidate, {"bill_name": "新账单"})

        self.assertEqual(result.field_errors, {})
        self.assertEqual(result.candidate.title, "旧账单")
        self.assertEqual(result.candidate.bill_name, "新账单")

    def test_date_correction_clears_old_relative_source(self) -> None:
        base = self.purchase_candidate(
            event_date=None,
            event_date_text="昨天",
        )
        result = self.apply(base, {"event_date": "2026-07-20"})

        self.assertEqual(result.field_errors, {})
        self.assertEqual(result.candidate.event_date, date(2026, 7, 20))
        self.assertIsNone(result.candidate.event_date_text)

    def test_legacy_relative_date_is_canonicalized_before_other_corrections(self) -> None:
        base = self.purchase_candidate(
            event_date=None,
            event_date_text="昨天",
        )
        result = self.apply(base, {"amount": 5200})

        self.assertEqual(result.field_errors, {})
        self.assertEqual(result.candidate.event_date, date(2026, 7, 29))
        self.assertEqual(result.candidate.amount, 5200)

    def purchase_candidate(self, **updates: object) -> ExtractedRecordCandidate:
        data = {
            "intent": "create_record",
            "record_type": RecordType.PURCHASE,
            "title": "相机",
            "amount": 5000,
            "currency": "CNY",
            "event_date": date(2026, 7, 25),
            "return_days": 7,
        }
        data.update(updates)
        return ExtractedRecordCandidate.model_validate(data)

    def apply(
        self,
        candidate: ExtractedRecordCandidate,
        corrections: dict[str, object],
    ):
        return apply_candidate_corrections(
            candidate,
            corrections,
            self.service,
            self.now,
        )


if __name__ == "__main__":
    unittest.main()
