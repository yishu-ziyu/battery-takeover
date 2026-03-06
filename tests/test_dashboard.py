from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from battery_takeover.config import (
    AppConfig,
    ControlConfig,
    ExecutorConfig,
    NotifyConfig,
    PathsConfig,
    PolicyConfig,
    SamplingConfig,
)
from battery_takeover.dashboard import _build_history, _build_overview
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


if __name__ == "__main__":
    unittest.main()
