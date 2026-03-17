from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from battery_takeover.config import (
    AppConfig,
    ControlConfig,
    ExecutorConfig,
    NotifyConfig,
    PathsConfig,
    PolicyConfig,
    SamplingConfig,
)
from battery_takeover.dashboard import _build_history, _build_overview, _read_policy_config, _save_settings_and_apply
from battery_takeover.models import BatterySample
from battery_takeover.storage import Storage


class DashboardTests(unittest.TestCase):
    def test_build_overview_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = AppConfig(
                config_path=base / "config.toml",
                policy=PolicyConfig(92, 88, 24, 300),
                sampling=SamplingConfig(60, "Asia/Shanghai"),
                control=ControlConfig(True, True),
                executor=ExecutorConfig(["battery", "batt"], True, 8),
                notify=NotifyConfig(True, False),
                paths=PathsConfig(base / "state" / "battery.db", base / "logs" / "agent.log", base / "reports"),
            )
            cfg.paths.db.parent.mkdir(parents=True, exist_ok=True)
            st = Storage(cfg.paths.db)
            st.init_db()

            ts = datetime.now(timezone.utc).isoformat()
            st.insert_sample(
                BatterySample(
                    ts=ts,
                    on_ac=True,
                    percent=55,
                    charging=True,
                    time_remaining_min=90,
                    cycle_count=99,
                    max_capacity_pct=98,
                    source_raw="x",
                )
            )
            st.insert_action(
                ts=ts,
                action_type="NOOP",
                backend="batt",
                target_percent=None,
                success=True,
                error_code=None,
                error_msg=None,
            )
            st.set_state("mode", "ACTIVE_CONTROL", ts)

            overview = _build_overview(cfg)
            self.assertEqual(overview["runtime_state"]["mode"], "ACTIVE_CONTROL")
            self.assertEqual(overview["latest_sample"]["percent"], 55)

            history = _build_history(cfg, 24)
            self.assertGreaterEqual(len(history["samples"]), 1)
            self.assertGreaterEqual(len(history["actions"]), 1)

    def test_read_policy_config_includes_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg_path = base / "config.toml"
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
enabled = false
allow_write_after_observe = true

[executor]
preferred = ["battery", "batt"]
auto_fallback = true
command_timeout_sec = 8

[notify]
terminal = true
macos_notification = false

[paths]
db = "./state/battery.db"
log = "./logs/agent.log"
reports_dir = "./reports"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = AppConfig(
                config_path=cfg_path,
                policy=PolicyConfig(92, 88, 24, 300),
                sampling=SamplingConfig(60, "Asia/Shanghai"),
                control=ControlConfig(True, True),
                executor=ExecutorConfig(["battery", "batt"], True, 8),
                notify=NotifyConfig(True, False),
                paths=PathsConfig(base / "state" / "battery.db", base / "logs" / "agent.log", base / "reports"),
            )

            result = _read_policy_config(cfg)
            self.assertFalse(result["enabled"])

    def test_save_settings_and_apply_enable_calls_enforce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg_path = base / "config.toml"
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
macos_notification = false

[paths]
db = "./state/battery.db"
log = "./logs/agent.log"
reports_dir = "./reports"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = AppConfig(
                config_path=cfg_path,
                policy=PolicyConfig(92, 88, 24, 300),
                sampling=SamplingConfig(60, "Asia/Shanghai"),
                control=ControlConfig(True, True),
                executor=ExecutorConfig(["battery", "batt"], True, 8),
                notify=NotifyConfig(True, False),
                paths=PathsConfig(base / "state" / "battery.db", base / "logs" / "agent.log", base / "reports"),
            )

            with patch("battery_takeover.dashboard._enforce_once", return_value={"action": "SET_LIMIT", "success": True}) as mock_enforce:
                payload = _save_settings_and_apply(cfg, stop_percent=94, resume_percent=90, enabled=True)

            self.assertTrue(payload["policy"]["enabled"])
            self.assertEqual(payload["policy"]["stop_percent"], 94)
            mock_enforce.assert_called_once()

    def test_save_settings_and_apply_disable_clears_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg_path = base / "config.toml"
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
macos_notification = false

[paths]
db = "./state/battery.db"
log = "./logs/agent.log"
reports_dir = "./reports"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = AppConfig(
                config_path=cfg_path,
                policy=PolicyConfig(92, 88, 24, 300),
                sampling=SamplingConfig(60, "Asia/Shanghai"),
                control=ControlConfig(True, True),
                executor=ExecutorConfig(["battery", "batt"], True, 8),
                notify=NotifyConfig(True, False),
                paths=PathsConfig(base / "state" / "battery.db", base / "logs" / "agent.log", base / "reports"),
            )

            with patch("battery_takeover.dashboard._clear_limit_now", return_value={"action": "CLEAR_LIMIT", "success": True}) as mock_clear:
                payload = _save_settings_and_apply(cfg, stop_percent=100, resume_percent=95, enabled=False)

            self.assertFalse(payload["policy"]["enabled"])
            self.assertEqual(payload["policy"]["stop_percent"], 100)
            mock_clear.assert_called_once()


if __name__ == "__main__":
    unittest.main()
