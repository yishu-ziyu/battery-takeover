from __future__ import annotations

from datetime import datetime, timezone
import logging
import subprocess

from .config import NotifyConfig


class Notifier:
    def __init__(self, cfg: NotifyConfig):
        self.cfg = cfg

    def notify(self, title: str, message: str, level: str = "INFO") -> None:
        ts = datetime.now(timezone.utc).isoformat()
        if self.cfg.terminal:
            logging.log(_to_level(level), "%s | %s | %s", ts, title, message)

        if self.cfg.macos_notification:
            self._notify_macos(title=title, message=message)

    def _notify_macos(self, title: str, message: str) -> None:
        safe_title = title.replace('"', "'")
        safe_message = message.replace('"', "'")
        script = f'display notification "{safe_message}" with title "{safe_title}"'
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except Exception:
            # Notification is best-effort and should never stop the main flow.
            pass


def _to_level(level: str) -> int:
    normalized = level.upper()
    if normalized == "DEBUG":
        return logging.DEBUG
    if normalized == "WARNING":
        return logging.WARNING
    if normalized == "ERROR":
        return logging.ERROR
    return logging.INFO
