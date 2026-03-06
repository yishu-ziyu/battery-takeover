from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from battery_takeover.models import ActionType, BatterySample, PolicyDecision, RuntimeMode, RuntimeSnapshot
from battery_takeover.policy import PolicyEngine
from battery_takeover.storage import Storage


class PolicyEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.cfg = AppConfig(
            config_path=base / "config.toml",
            policy=PolicyConfig(
                stop_percent=92,
                resume_percent=88,
                observe_hours=24,
                min_action_interval_sec=300,
            ),
            sampling=SamplingConfig(interval_sec=60, timezone="Asia/Shanghai"),
            control=ControlConfig(enabled=True, allow_write_after_observe=True),
            executor=ExecutorConfig(preferred=["battery", "batt"], auto_fallback=True, command_timeout_sec=8),
            notify=NotifyConfig(terminal=True, macos_notification=False),
            paths=PathsConfig(db=base / "state.db", log=base / "agent.log", reports_dir=base / "reports"),
        )
        self.storage = Storage(self.cfg.paths.db)
        self.storage.init_db()
        self.engine = PolicyEngine(cfg=self.cfg, storage=self.storage)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _sample(self, percent: int, on_ac: bool = True) -> BatterySample:
        return BatterySample(
            ts=datetime.now(timezone.utc).isoformat(),
            on_ac=on_ac,
            percent=percent,
            charging=True,
            time_remaining_min=None,
            cycle_count=100,
            max_capacity_pct=99,
            source_raw="test",
        )

    def _snapshot(self, paused: bool = False, mode: RuntimeMode = RuntimeMode.ACTIVE_CONTROL) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            mode=mode,
            observe_started_at=(datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(),
            consecutive_failures=0,
            charging_paused=paused,
            last_action_at=None,
            last_backend=None,
            last_error=None,
        )

    def test_set_limit_when_above_stop(self) -> None:
        decision = self.engine.decide(sample=self._sample(93), snapshot=self._snapshot(paused=False))
        self.assertEqual(decision.action_type, ActionType.SET_LIMIT)
        self.assertEqual(decision.target_percent, 92)

    def test_clear_limit_when_below_resume(self) -> None:
        decision = self.engine.decide(sample=self._sample(87), snapshot=self._snapshot(paused=True))
        self.assertEqual(decision.action_type, ActionType.CLEAR_LIMIT)

    def test_noop_in_hysteresis_band(self) -> None:
        decision = self.engine.decide(sample=self._sample(90), snapshot=self._snapshot(paused=False))
        self.assertEqual(decision.action_type, ActionType.NOOP)

    def test_cooldown_blocks_action(self) -> None:
        now = datetime.now(timezone.utc)
        snapshot = self._snapshot(paused=False)
        snapshot.last_action_at = (now - timedelta(seconds=30)).isoformat()
        decision = self.engine.decide(sample=self._sample(95), snapshot=snapshot, now=now)
        self.assertEqual(decision.action_type, ActionType.NOOP)
        self.assertIn("cooldown", decision.reason)

    def test_observe_promotes_to_active(self) -> None:
        now = datetime.now(timezone.utc)
        snapshot = self._snapshot(mode=RuntimeMode.OBSERVE_ONLY)
        updated = self.engine.reconcile_mode(snapshot=snapshot, backend_ok=True, now=now)
        self.assertEqual(updated.mode, RuntimeMode.ACTIVE_CONTROL)

    def test_failures_degrade_after_three(self) -> None:
        snapshot = self._snapshot(mode=RuntimeMode.ACTIVE_CONTROL)
        decision = PolicyDecision(action_type=ActionType.SET_LIMIT, reason="x", target_percent=92)

        for _ in range(3):
            snapshot = self.engine.apply_result(
                snapshot=snapshot,
                decision=decision,
                success=False,
                backend="batt",
                error="boom",
            )
        self.assertEqual(snapshot.mode, RuntimeMode.DEGRADED_READONLY)


if __name__ == "__main__":
    unittest.main()
