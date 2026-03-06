from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from battery_takeover.config import load_config, update_policy_thresholds


class ConfigUpdateTests(unittest.TestCase):
    def test_update_policy_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "default.toml"
            cfg_path.write_text(
                """
[policy]
stop_percent = 92
resume_percent = 88
observe_hours = 24
min_action_interval_sec = 300

[sampling]
interval_sec = 60
timezone = "Asia/Shanghai"

[control]
enabled = true
allow_write_after_observe = true

[executor]
preferred = ["battery", "batt"]
auto_fallback = true
command_timeout_sec = 8

[notify]
terminal = true
macos_notification = true

[paths]
db = "../state/battery.db"
log = "../logs/agent.log"
reports_dir = "../reports"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            cfg = update_policy_thresholds(cfg_path, stop_percent=94, resume_percent=90)
            self.assertEqual(cfg.policy.stop_percent, 94)
            self.assertEqual(cfg.policy.resume_percent, 90)

            cfg2 = load_config(cfg_path)
            self.assertEqual(cfg2.policy.stop_percent, 94)
            self.assertEqual(cfg2.policy.resume_percent, 90)


if __name__ == "__main__":
    unittest.main()
