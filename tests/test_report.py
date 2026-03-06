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
from battery_takeover.models import BatterySample
from battery_takeover.report import generate_daily_report
from battery_takeover.storage import Storage


class ReportTests(unittest.TestCase):
    def test_generate_daily_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = AppConfig(
                config_path=base / "config.toml",
                policy=PolicyConfig(92, 88, 24, 300),
                sampling=SamplingConfig(60, "Asia/Shanghai"),
                control=ControlConfig(True, True),
                executor=ExecutorConfig(["battery", "batt"], True, 8),
                notify=NotifyConfig(True, False),
                paths=PathsConfig(base / "state.db", base / "agent.log", base / "reports"),
            )
            cfg.paths.reports_dir.mkdir(parents=True, exist_ok=True)
            st = Storage(cfg.paths.db)
            st.init_db()

            sample = BatterySample(
                ts=datetime.now(timezone.utc).isoformat(),
                on_ac=True,
                percent=80,
                charging=True,
                time_remaining_min=60,
                cycle_count=100,
                max_capacity_pct=99,
                source_raw="x",
            )
            st.insert_sample(sample)
            st.insert_action(
                ts=sample.ts,
                action_type="NOOP",
                backend="noop",
                target_percent=None,
                success=True,
                error_code=None,
                error_msg=None,
            )

            p = generate_daily_report(cfg=cfg, storage=st, date_value=None)
            self.assertTrue(p.exists())
            body = p.read_text(encoding="utf-8")
            self.assertIn("电池接管日报", body)


if __name__ == "__main__":
    unittest.main()
