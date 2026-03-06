from __future__ import annotations

from dataclasses import dataclass
import logging
import time

from .collector import CollectorError, collect_sample
from .config import AppConfig, ensure_runtime_dirs, load_config
from .models import ActionType, RuntimeMode
from .notifier import Notifier
from .policy import PolicyEngine, utc_now
from .storage import Storage
from .executors import BattExecutor, BatteryExecutor, ExecutorRouter


@dataclass(slots=True)
class CycleResult:
    ts: str
    mode: str
    backend: str
    action: str
    success: bool
    message: str


def setup_logging(log_path: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def _build_router(cfg: AppConfig) -> ExecutorRouter:
    executors = {
        "battery": BatteryExecutor(timeout_sec=cfg.executor.command_timeout_sec),
        "batt": BattExecutor(timeout_sec=cfg.executor.command_timeout_sec),
    }
    return ExecutorRouter(
        executors=executors,
        preferred=cfg.executor.preferred,
        auto_fallback=cfg.executor.auto_fallback,
    )


def run_cycle(cfg: AppConfig, storage: Storage, notifier: Notifier, dry_run: bool = False) -> CycleResult:
    now = utc_now()
    ts = now.isoformat()

    try:
        sample = collect_sample(timeout_sec=cfg.executor.command_timeout_sec)
    except CollectorError as exc:
        msg = f"collector failed: {exc}"
        logging.error(msg)
        notifier.notify("BatteryTakeover Collector Error", msg, level="ERROR")
        storage.insert_action(
            ts=ts,
            action_type=ActionType.NOOP.value,
            backend="collector",
            target_percent=None,
            success=False,
            error_code="COLLECTOR_FAILED",
            error_msg=str(exc),
        )
        return CycleResult(
            ts=ts,
            mode=RuntimeMode.DEGRADED_READONLY.value,
            backend="collector",
            action=ActionType.NOOP.value,
            success=False,
            message=msg,
        )

    storage.insert_sample(sample)

    router = _build_router(cfg)
    name, executor, status = router.choose()
    backend_ok = executor is not None and status is not None and status.available

    engine = PolicyEngine(cfg=cfg, storage=storage)
    snapshot = engine.load_runtime(now)
    prev_mode = snapshot.mode
    snapshot = engine.reconcile_mode(snapshot, backend_ok=backend_ok, now=now)

    if prev_mode != snapshot.mode and snapshot.mode == RuntimeMode.DEGRADED_READONLY:
        msg = snapshot.last_error or "mode switched to degraded"
        storage.insert_action(
            ts=ts,
            action_type=ActionType.DEGRADE.value,
            backend=name,
            target_percent=None,
            success=False,
            error_code="DEGRADED",
            error_msg=msg,
        )
        notifier.notify("BatteryTakeover Degraded", msg, level="WARNING")

    decision = engine.decide(sample=sample, snapshot=snapshot, dry_run=dry_run, now=now)

    backend_name = name
    action_success = True
    action_error = None

    if decision.action_type in {ActionType.SET_LIMIT, ActionType.CLEAR_LIMIT}:
        if executor is None:
            action_success = False
            action_error = "no available executor"
        else:
            if decision.action_type == ActionType.SET_LIMIT:
                result = executor.set_limit(decision.target_percent or cfg.policy.stop_percent)
            else:
                result = executor.clear_limit()
            backend_name = result.backend
            action_success = result.success
            action_error = result.error_msg

        snapshot = engine.apply_result(
            snapshot=snapshot,
            decision=decision,
            success=action_success,
            backend=backend_name,
            error=action_error,
            now=now,
        )

        if not action_success:
            msg = f"executor failed ({backend_name}): {action_error or 'unknown'}"
            notifier.notify("BatteryTakeover Execute Error", msg, level="ERROR")
            if snapshot.mode == RuntimeMode.DEGRADED_READONLY:
                notifier.notify("BatteryTakeover Degraded", "Too many failures, switched to read-only", level="WARNING")

    storage.insert_action(
        ts=ts,
        action_type=decision.action_type.value,
        backend=backend_name,
        target_percent=decision.target_percent,
        success=action_success,
        error_code=None if action_success else "EXEC_FAILED",
        error_msg=action_error,
    )

    engine.persist_runtime(snapshot, now)

    message = decision.reason
    if not action_success and action_error:
        message = f"{message}; {action_error}"

    return CycleResult(
        ts=ts,
        mode=snapshot.mode.value,
        backend=backend_name,
        action=decision.action_type.value,
        success=action_success,
        message=message,
    )


def run_agent(cfg: AppConfig, once: bool = False, dry_run: bool = False) -> int:
    ensure_runtime_dirs(cfg)
    setup_logging(str(cfg.paths.log))

    storage = Storage(cfg.paths.db)
    storage.init_db()
    notifier = Notifier(cfg.notify)

    worst_exit = 0
    while True:
        try:
            cfg = load_config(cfg.config_path)
            ensure_runtime_dirs(cfg)
        except Exception as exc:
            logging.warning("config reload failed, use previous config: %s", exc)
        result = run_cycle(cfg=cfg, storage=storage, notifier=notifier, dry_run=dry_run)
        level = logging.INFO if result.success else logging.WARNING
        logging.log(
            level,
            "cycle ts=%s mode=%s backend=%s action=%s success=%s msg=%s",
            result.ts,
            result.mode,
            result.backend,
            result.action,
            result.success,
            result.message,
        )

        if not result.success or result.mode == RuntimeMode.DEGRADED_READONLY.value:
            worst_exit = max(worst_exit, 2)

        if once:
            return worst_exit

        time.sleep(cfg.sampling.interval_sec)
