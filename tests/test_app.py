from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from lifevault.models.schemas import LifeRecordCreate
from lifevault.storage.repository import VaultRepository


class StreamlitAppTest(unittest.TestCase):
    def test_saved_record_editor_previews_and_applies_partial_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            st.cache_resource.clear()
            database_path = Path(tmp) / "saved-editor.db"
            graph_path = Path(tmp) / "saved-editor-graph.db"
            repo = VaultRepository(database_path)
            record = repo.save_record(
                "local",
                LifeRecordCreate(
                    record_type="subscription",
                    title="旧会员",
                    amount=20,
                    deadline=date(2099, 8, 15),
                    details={
                        "service_name": "旧会员",
                        "billing_cycle": "monthly",
                        "auto_renew": True,
                    },
                ),
                "saved-editor-record",
            )
            environment = {
                "LIFEVAULT_DB": str(database_path),
                "LIFEVAULT_LANGGRAPH_DB": str(graph_path),
                "LIFEVAULT_USE_QWEN": "0",
            }
            with patch.dict(os.environ, environment):
                app = AppTest.from_file("lifevault/app/main.py").run(timeout=15)
                _button(app, "编辑").click()
                app.run(timeout=15)

                _widget(app.text_input, "记录标题").set_value("新会员")
                _widget(app.date_input, "下一续费日期").set_value(date(2099, 8, 20))
                app.run(timeout=15)
                self.assertFalse(_button(app, "预览修改").disabled)

                _button(app, "预览修改").click()
                app.run(timeout=15)
                self.assertEqual(
                    app.session_state["record_update_preview"]["preview"]["record"]["title"],
                    "新会员",
                )
                self.assertFalse(_button(app, "确认更新").disabled)

                _button(app, "确认更新").click()
                app.run(timeout=15)
                self.assertEqual(list(app.exception), [])

            updated = repo.get_record("local", record.id)
            self.assertEqual(updated.title, "新会员")
            self.assertEqual(updated.deadline.isoformat(), "2099-08-20")
            self.assertEqual(updated.version, 2)

    def test_review_forms_render_for_subscription_and_bill(self) -> None:
        cases = [
            (
                "我订阅了视频会员，每月 30 元，2099-08-15 自动续费，不提醒。",
                "下一续费日期",
            ),
            (
                "房租 3000 元，2099-08-01 前缴费，不提醒。",
                "缴费截止日期",
            ),
        ]
        for text, date_label in cases:
            with self.subTest(date_label=date_label), tempfile.TemporaryDirectory() as tmp:
                st.cache_resource.clear()
                environment = {
                    "LIFEVAULT_DB": str(Path(tmp) / "review.db"),
                    "LIFEVAULT_LANGGRAPH_DB": str(Path(tmp) / "review-graph.db"),
                    "LIFEVAULT_USE_QWEN": "0",
                }
                with patch.dict(os.environ, environment):
                    app = AppTest.from_file("lifevault/app/main.py").run(timeout=15)
                    app.text_area[0].input(text)
                    _button(app, "解析").click()
                    app.run(timeout=15)

                    self.assertEqual(list(app.exception), [])
                    self.assertEqual(
                        app.session_state["graph_turn"].interrupt_type,
                        "record_confirmation",
                    )
                    _widget(app.date_input, date_label)

    def test_record_review_applies_changes_before_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            st.cache_resource.clear()
            database_path = Path(tmp) / "review.db"
            graph_path = Path(tmp) / "review-graph.db"
            environment = {
                "LIFEVAULT_DB": str(database_path),
                "LIFEVAULT_LANGGRAPH_DB": str(graph_path),
                "LIFEVAULT_USE_QWEN": "0",
            }
            with patch.dict(os.environ, environment):
                app = AppTest.from_file("lifevault/app/main.py").run(timeout=15)
                app.text_area[0].input(
                    "我 2099-07-25 买了一个键盘，899 元，七天退货，不提醒。"
                )
                _button(app, "解析").click()
                app.run(timeout=15)

                _widget(app.number_input, "金额").set_value(999.0)
                _widget(app.number_input, "退货天数").set_value(14)
                app.run(timeout=15)
                self.assertTrue(_button(app, "确认保存").disabled)
                self.assertFalse(_button(app, "应用修改").disabled)

                _button(app, "应用修改").click()
                app.run(timeout=15)
                turn = app.session_state["graph_turn"]
                self.assertEqual(turn.record["amount"], 999.0)
                self.assertEqual(
                    turn.record["details"]["return_deadline"],
                    "2099-08-08",
                )
                self.assertFalse(_button(app, "确认保存").disabled)

                _button(app, "确认保存").click()
                app.run(timeout=15)
                self.assertEqual(app.session_state["graph_turn"].status, "completed")
                self.assertEqual(list(app.exception), [])

            records = VaultRepository(database_path).search_records("local", query="键盘")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].amount, 999.0)
            self.assertEqual(records[0].deadline.isoformat(), "2099-08-08")

    def test_dual_reminder_selection_creates_only_checked_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            st.cache_resource.clear()
            database_path = Path(tmp) / "app.db"
            graph_path = Path(tmp) / "graph.db"
            environment = {
                "LIFEVAULT_DB": str(database_path),
                "LIFEVAULT_LANGGRAPH_DB": str(graph_path),
                "LIFEVAULT_USE_QWEN": "0",
            }
            with patch.dict(os.environ, environment):
                app = AppTest.from_file("lifevault/app/main.py").run(timeout=15)
                self.assertEqual(list(app.exception), [])
                self.assertEqual(
                    [tab.label for tab in app.tabs],
                    ["添加记录", "我的记录", "提醒中心", "审计", "设置"],
                )

                app.text_area[0].input(
                    "我 2099-07-25 买了一个相机，5000 元，七天退货，保修一年，"
                    "退货前 2 天、保修到期前 30 天提醒我。"
                )
                _button(app, "解析").click()
                app.run(timeout=15)
                _button(app, "确认保存").click()
                app.run(timeout=15)

                reminder_checkboxes = [
                    checkbox
                    for checkbox in app.checkbox
                    if checkbox.label.startswith(("return_deadline", "warranty_deadline"))
                ]
                self.assertEqual(len(reminder_checkboxes), 2)
                reminder_checkboxes[0].set_value(False)
                _button(app, "创建所选提醒").click()
                app.run(timeout=15)
                self.assertEqual(list(app.exception), [])

            reminders = VaultRepository(database_path).list_reminders("local")
            self.assertEqual(
                [reminder.reminder_type.value for reminder in reminders],
                ["warranty_deadline"],
            )


def _button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


def _widget(widgets, label: str):
    return next(widget for widget in widgets if widget.label == label)


if __name__ == "__main__":
    unittest.main()
