from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import AppConfig
from .models import ActionType, BatterySample, PolicyDecision, RuntimeMode, RuntimeSnapshot
from .storage import Storage


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _to_bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.lower() in {"1", "true", "yes", "on"}


def _to_int(v: str | None, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


class PolicyEngine:
    def __init__(self, cfg: AppConfig, storage: Storage):
        self.cfg = cfg
        self.storage = storage

    def load_runtime(self, now: datetime | None = None) -> RuntimeSnapshot:
        now = now or utc_now()
        state = self.storage.get_state_map()

        mode_raw = state.get("mode", RuntimeMode.OBSERVE_ONLY.value)
        mode = RuntimeMode(mode_raw) if mode_raw in RuntimeMode._value2member_map_ else RuntimeMode.OBSERVE_ONLY

        observe_started = state.get("observe_started_at", now.isoformat())
        snapshot = RuntimeSnapshot(
            mode=mode,
            observe_started_at=observe_started,
            consecutive_failures=_to_int(state.get("consecutive_failures"), 0),
            charging_paused=_to_bool(state.get("charging_paused"), False),
            last_action_at=state.get("last_action_at"),
            last_backend=state.get("last_backend"),
            last_error=state.get("last_error"),
        )

        if not state:
            self.persist_runtime(snapshot, now)
        return snapshot

    def persist_runtime(self, snapshot: RuntimeSnapshot, now: datetime | None = None) -> None:
        now = now or utc_now()
        ts = now.isoformat()
        self.storage.set_state("mode", snapshot.mode.value, ts)
        self.storage.set_state("observe_started_at", snapshot.observe_started_at, ts)
        self.storage.set_state("consecutive_failures", str(snapshot.consecutive_failures), ts)
        self.storage.set_state("charging_paused", "1" if snapshot.charging_paused else "0", ts)
        self.storage.set_state("last_action_at", snapshot.last_action_at or "", ts)
        self.storage.set_state("last_backend", snapshot.last_backend or "", ts)
        self.storage.set_state("last_error", snapshot.last_error or "", ts)

    def reconcile_mode(self, snapshot: RuntimeSnapshot, backend_ok: bool, now: datetime | None = None) -> RuntimeSnapshot:
        now = now or utc_now()

        if snapshot.mode == RuntimeMode.OBSERVE_ONLY:
            started = parse_iso(snapshot.observe_started_at)
            elapsed = now - started
            observe_hours = timedelta(hours=self.cfg.policy.observe_hours)
            if elapsed >= observe_hours and self.cfg.control.allow_write_after_observe and self.cfg.control.enabled:
                snapshot.mode = RuntimeMode.ACTIVE_CONTROL if backend_ok else RuntimeMode.DEGRADED_READONLY
                if not backend_ok:
                    snapshot.last_error = "No available executor after observe window"

        elif snapshot.mode == RuntimeMode.DEGRADED_READONLY:
            if backend_ok and self.cfg.control.enabled:
                snapshot.mode = RuntimeMode.ACTIVE_CONTROL
                snapshot.consecutive_failures = 0
                snapshot.last_error = None

        return snapshot

    def decide(
        self,
        sample: BatterySample,
        snapshot: RuntimeSnapshot,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> PolicyDecision:
        now = now or utc_now()

        if not self.cfg.control.enabled:
            return PolicyDecision(action_type=ActionType.NOOP, reason="control disabled")

        if snapshot.mode != RuntimeMode.ACTIVE_CONTROL:
            return PolicyDecision(action_type=ActionType.NOOP, reason=f"mode={snapshot.mode.value}")

        if not sample.on_ac:
            return PolicyDecision(action_type=ActionType.NOOP, reason="not on AC power")

        candidate: Optional[PolicyDecision] = None
        if sample.percent >= self.cfg.policy.stop_percent and not snapshot.charging_paused:
            candidate = PolicyDecision(
                action_type=ActionType.SET_LIMIT,
                target_percent=self.cfg.policy.stop_percent,
                reason=f"percent={sample.percent} >= stop={self.cfg.policy.stop_percent}",
            )
        elif sample.percent <= self.cfg.policy.resume_percent and snapshot.charging_paused:
            candidate = PolicyDecision(
                action_type=ActionType.CLEAR_LIMIT,
                reason=f"percent={sample.percent} <= resume={self.cfg.policy.resume_percent}",
            )

        if candidate is None:
            return PolicyDecision(action_type=ActionType.NOOP, reason="inside hysteresis band")

        if dry_run:
            return PolicyDecision(
                action_type=ActionType.NOOP,
                target_percent=candidate.target_percent,
                reason=f"dry-run: would {candidate.action_type.value}",
            )

        if snapshot.last_action_at:
            try:
                last = parse_iso(snapshot.last_action_at)
                delta = now - last
                if delta.total_seconds() < self.cfg.policy.min_action_interval_sec:
                    return PolicyDecision(
                        action_type=ActionType.NOOP,
                        reason=(
                            f"cooldown active ({int(delta.total_seconds())}s "
                            f"< {self.cfg.policy.min_action_interval_sec}s)"
                        ),
                    )
            except ValueError:
                pass

        return candidate

    def apply_result(
        self,
        snapshot: RuntimeSnapshot,
        decision: PolicyDecision,
        success: bool,
        backend: str,
        error: str | None,
        now: datetime | None = None,
    ) -> RuntimeSnapshot:
        now = now or utc_now()

        if decision.action_type in {ActionType.SET_LIMIT, ActionType.CLEAR_LIMIT}:
            snapshot.last_action_at = now.isoformat()
            snapshot.last_backend = backend

            if success:
                snapshot.consecutive_failures = 0
                snapshot.last_error = None
                if decision.action_type == ActionType.SET_LIMIT:
                    snapshot.charging_paused = True
                elif decision.action_type == ActionType.CLEAR_LIMIT:
                    snapshot.charging_paused = False
            else:
                snapshot.consecutive_failures += 1
                snapshot.last_error = error or "executor failed"
                if snapshot.consecutive_failures >= 3:
                    snapshot.mode = RuntimeMode.DEGRADED_READONLY
        return snapshot
