from __future__ import annotations

from dataclasses import dataclass


class NotificationError(RuntimeError):
    pass


@dataclass
class DesktopNotificationProvider:
    app_name: str = "LifeVault"

    def send(self, title: str, message: str, record_id: str) -> None:
        try:
            from plyer import notification

            notification.notify(
                title=title,
                message=message,
                app_name=self.app_name,
                timeout=10,
            )
        except Exception as exc:
            raise NotificationError(str(exc)) from exc


@dataclass
class ConsoleNotificationProvider:
    app_name: str = "LifeVault"

    def send(self, title: str, message: str, record_id: str) -> None:
        print(f"[{self.app_name}] {title}: {message} (record_id={record_id})")
