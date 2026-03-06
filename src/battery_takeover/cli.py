from __future__ import annotations

import argparse
from datetime import datetime, timezone
import platform
import shutil
import sys

from .agent import run_agent, run_cycle, setup_logging
from .collector import collect_sample
from .config import AppConfig, ensure_runtime_dirs, load_config
from .dashboard import run_dashboard
from .executors import BattExecutor, BatteryExecutor, ExecutorRouter
from .models import RuntimeMode, RuntimeSnapshot
from .notifier import Notifier
from .policy import PolicyEngine
from .report import generate_daily_report
from .storage import Storage


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


def cmd_doctor(cfg: AppConfig, repair: bool) -> int:
    ensure_runtime_dirs(cfg)
    storage = Storage(cfg.paths.db)
    storage.init_db()

    print("[doctor] environment")
    print(f"- macOS: {platform.mac_ver()[0]}")
    print(f"- machine: {platform.machine()}")
    print(f"- python: {platform.python_version()}")

    required_bins = ["pmset", "system_profiler", "osascript"]
    hard_fail = False
    for name in required_bins:
        path = shutil.which(name)
        print(f"- {name}: {'OK' if path else 'MISSING'} {path or ''}")
        if path is None and name != "osascript":
            hard_fail = True

    router = _build_router(cfg)
    probes = router.probe_map()
    print("[doctor] executors")
    available = False
    for name in cfg.executor.preferred:
        status = probes.get(name)
        if status is None:
            continue
        print(f"- {name}: {'OK' if status.available else 'UNAVAILABLE'} ({status.detail})")
        available = available or status.available

    if repair and available:
        now = datetime.now(timezone.utc)
        engine = PolicyEngine(cfg=cfg, storage=storage)
        snapshot = engine.load_runtime(now=now)
        if snapshot.mode == RuntimeMode.DEGRADED_READONLY:
            snapshot.mode = RuntimeMode.ACTIVE_CONTROL
            snapshot.consecutive_failures = 0
            snapshot.last_error = None
            engine.persist_runtime(snapshot, now)
            print("[doctor] repair: DEGRADED_READONLY -> ACTIVE_CONTROL")

    if hard_fail:
        print("[doctor] result: FAILED")
        return 3
    if not available:
        print("[doctor] result: DEGRADED (monitoring only)")
        return 2

    print("[doctor] result: PASS")
    return 0


def cmd_init(cfg: AppConfig) -> int:
    ensure_runtime_dirs(cfg)
    storage = Storage(cfg.paths.db)
    storage.init_db()

    now = datetime.now(timezone.utc)
    ts = now.isoformat()
    engine = PolicyEngine(cfg=cfg, storage=storage)
    snapshot = RuntimeSnapshot(
        mode=RuntimeMode.OBSERVE_ONLY,
        observe_started_at=ts,
        consecutive_failures=0,
        charging_paused=False,
        last_action_at=None,
        last_backend=None,
        last_error=None,
    )
    engine.persist_runtime(snapshot, now)

    print("[init] done")
    print(f"- db: {cfg.paths.db}")
    print(f"- log: {cfg.paths.log}")
    print(f"- reports_dir: {cfg.paths.reports_dir}")
    return 0


def cmd_sample(cfg: AppConfig) -> int:
    ensure_runtime_dirs(cfg)
    storage = Storage(cfg.paths.db)
    storage.init_db()

    sample = collect_sample(timeout_sec=cfg.executor.command_timeout_sec)
    storage.insert_sample(sample)
    print(
        "[sample]"
        f" ts={sample.ts} on_ac={sample.on_ac} percent={sample.percent}"
        f" charging={sample.charging} cycle={sample.cycle_count}"
    )
    return 0


def cmd_enforce(cfg: AppConfig, dry_run: bool) -> int:
    ensure_runtime_dirs(cfg)
    setup_logging(str(cfg.paths.log))

    storage = Storage(cfg.paths.db)
    storage.init_db()
    notifier = Notifier(cfg.notify)

    result = run_cycle(cfg=cfg, storage=storage, notifier=notifier, dry_run=dry_run)
    print(
        "[enforce]"
        f" ts={result.ts} mode={result.mode} backend={result.backend}"
        f" action={result.action} success={result.success} msg={result.message}"
    )
    if not result.success or result.mode == RuntimeMode.DEGRADED_READONLY.value:
        return 2
    return 0


def cmd_agent(cfg: AppConfig, once: bool, dry_run: bool) -> int:
    return run_agent(cfg=cfg, once=once, dry_run=dry_run)


def cmd_status(cfg: AppConfig) -> int:
    ensure_runtime_dirs(cfg)
    storage = Storage(cfg.paths.db)
    storage.init_db()

    state = storage.get_state_map()
    latest_sample = storage.latest_sample()
    latest_action = storage.latest_action()

    print("[status] runtime_state")
    for key in sorted(state):
        print(f"- {key}: {state[key]}")

    print("[status] latest_sample")
    if latest_sample is None:
        print("- none")
    else:
        print(
            "- "
            f"ts={latest_sample['ts']} percent={latest_sample['percent']}"
            f" on_ac={latest_sample['on_ac']} charging={latest_sample['charging']}"
        )

    print("[status] latest_action")
    if latest_action is None:
        print("- none")
    else:
        print(
            "- "
            f"ts={latest_action['ts']} action={latest_action['action_type']}"
            f" backend={latest_action['backend']} success={latest_action['success']}"
            f" err={latest_action['error_msg'] or ''}"
        )

    return 0


def cmd_report_daily(cfg: AppConfig, date_str: str | None) -> int:
    ensure_runtime_dirs(cfg)
    storage = Storage(cfg.paths.db)
    storage.init_db()

    report_path = generate_daily_report(cfg=cfg, storage=storage, date_value=date_str)
    print(f"[report] generated: {report_path}")
    return 0


def cmd_dashboard(cfg: AppConfig, host: str, port: int, open_browser: bool) -> int:
    ensure_runtime_dirs(cfg)
    storage = Storage(cfg.paths.db)
    storage.init_db()
    return run_dashboard(cfg=cfg, host=host, port=port, open_browser=open_browser)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="btake")
    p.add_argument("--config", default=None, help="path to config TOML")

    sub = p.add_subparsers(dest="command", required=True)

    sp_doctor = sub.add_parser("doctor", help="check environment and executor availability")
    sp_doctor.add_argument("--repair", action="store_true", help="attempt runtime mode repair")

    sub.add_parser("init", help="initialize database and runtime state")
    sub.add_parser("sample", help="collect and store one battery sample")

    sp_enforce = sub.add_parser("enforce", help="collect + evaluate policy + optionally execute")
    sp_enforce.add_argument("--dry-run", action="store_true", help="evaluate policy without write actions")

    sp_agent = sub.add_parser("agent", help="run scheduler loop")
    sp_agent.add_argument("--once", action="store_true", help="run one cycle and exit")
    sp_agent.add_argument("--dry-run", action="store_true", help="dry-run policy")

    sub.add_parser("status", help="show runtime state and latest records")

    sp_report = sub.add_parser("report", help="generate reports")
    report_sub = sp_report.add_subparsers(dest="report_cmd", required=True)
    daily = report_sub.add_parser("daily", help="generate daily markdown report")
    daily.add_argument("--date", default=None, help="YYYY-MM-DD")

    sp_dashboard = sub.add_parser("dashboard", help="start local visual dashboard")
    sp_dashboard.add_argument("--host", default="127.0.0.1", help="bind host")
    sp_dashboard.add_argument("--port", type=int, default=8765, help="bind port")
    sp_dashboard.add_argument("--open", action="store_true", help="open browser automatically")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"[error] failed to load config: {exc}", file=sys.stderr)
        return 3

    if args.command == "doctor":
        return cmd_doctor(cfg, repair=bool(args.repair))
    if args.command == "init":
        return cmd_init(cfg)
    if args.command == "sample":
        return cmd_sample(cfg)
    if args.command == "enforce":
        return cmd_enforce(cfg, dry_run=bool(args.dry_run))
    if args.command == "agent":
        return cmd_agent(cfg, once=bool(args.once), dry_run=bool(args.dry_run))
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "report":
        if args.report_cmd == "daily":
            return cmd_report_daily(cfg, date_str=args.date)
    if args.command == "dashboard":
        return cmd_dashboard(cfg, host=args.host, port=args.port, open_browser=bool(args.open))

    return 3


if __name__ == "__main__":
    raise SystemExit(main())
