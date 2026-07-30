from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from lifevault.storage.repository import VaultRepository


class StreamlitAppTest(unittest.TestCase):
    def test_dual_reminder_selection_creates_only_checked_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
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


if __name__ == "__main__":
    unittest.main()
