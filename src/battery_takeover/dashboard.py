from __future__ import annotations

from datetime import datetime, timedelta, timezone
import ipaddress
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import os
import subprocess
import sys

from .agent import run_cycle, setup_logging
from .config import AppConfig, ensure_runtime_dirs, load_config, update_dashboard_settings
from .executors import BattExecutor, BatteryExecutor, ExecutorRouter
from .models import ActionType
from .notifier import Notifier
from .policy import PolicyEngine, utc_now
from .storage import Storage

MAX_BODY_SIZE = 1 * 1024 * 1024  # 1MB limit for request bodies


def _row_to_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _sample_row_to_public_dict(row):
    if row is None:
        return None
    payload = _row_to_dict(row)
    payload.pop("source_raw", None)
    return payload


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _agent_status(cfg: AppConfig) -> dict[str, object]:
    fallback_pid = _find_agent_pid_by_ps()
    pid_file = cfg.paths.db.parent / "agent.pid"
    if not pid_file.exists():
        if fallback_pid is not None:
            return {"running": True, "pid": fallback_pid, "source": "process_scan"}
        return {"running": False, "pid": None, "source": "pid_file_missing"}

    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        if fallback_pid is not None:
            return {"running": True, "pid": fallback_pid, "source": "process_scan"}
        return {"running": False, "pid": None, "source": "pid_file_invalid"}

    try:
        os.kill(pid, 0)
        return {"running": True, "pid": pid, "source": "pid_file"}
    except ProcessLookupError:
        if fallback_pid is not None:
            return {"running": True, "pid": fallback_pid, "source": "process_scan"}
        return {"running": False, "pid": pid, "source": "pid_dead"}
    except PermissionError:
        return {"running": True, "pid": pid, "accessible": False, "source": "pid_permission_denied"}


def _find_agent_pid_by_ps() -> int | None:
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "battery_takeover.cli --config .* agent"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None

    if proc.returncode != 0:
        return None

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return int(line)
        except ValueError:
            continue
    return None


def _build_overview(cfg: AppConfig) -> dict[str, object]:
    st = Storage(cfg.paths.db)
    st.init_db()

    runtime_state = st.get_state_map()
    latest_sample = _sample_row_to_public_dict(st.latest_sample())
    latest_action = _row_to_dict(st.latest_action())
    sample_count = st.count_samples()
    action_count = st.count_actions()

    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(hours=24)).isoformat()
    now_iso = now.isoformat()
    recent_samples = st.list_samples(day_ago, now_iso)
    recent_actions = st.list_actions(day_ago, now_iso)

    return {
        "generated_at": now_iso,
        "runtime_state": runtime_state,
        "latest_sample": latest_sample,
        "latest_action": latest_action,
        "sample_count": sample_count,
        "action_count": action_count,
        "sample_count_24h": len(recent_samples),
        "action_count_24h": len(recent_actions),
        "agent": _agent_status(cfg),
        "paths": {
            "db": str(cfg.paths.db),
            "log": str(cfg.paths.log),
            "reports_dir": str(cfg.paths.reports_dir),
        },
    }


def _build_history(cfg: AppConfig, hours: int) -> dict[str, object]:
    st = Storage(cfg.paths.db)
    st.init_db()

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)

    samples = [
        {
            "ts": row["ts"],
            "percent": row["percent"],
            "on_ac": row["on_ac"],
            "charging": row["charging"],
        }
        for row in st.list_samples(start.isoformat(), now.isoformat())
    ]

    actions = [
        {
            "ts": row["ts"],
            "action_type": row["action_type"],
            "backend": row["backend"],
            "target_percent": row["target_percent"],
            "success": row["success"],
            "error_msg": row["error_msg"],
        }
        for row in st.list_actions(start.isoformat(), now.isoformat())
    ]

    return {
        "hours": hours,
        "generated_at": now.isoformat(),
        "samples": samples,
        "actions": actions,
    }


def _read_policy_config(cfg: AppConfig) -> dict[str, int]:
    fresh = load_config(cfg.config_path)
    return {
        "stop_percent": fresh.policy.stop_percent,
        "resume_percent": fresh.policy.resume_percent,
        "observe_hours": fresh.policy.observe_hours,
        "min_action_interval_sec": fresh.policy.min_action_interval_sec,
        "enabled": fresh.control.enabled,
    }


def _enforce_once(cfg: AppConfig) -> dict[str, object]:
    fresh = load_config(cfg.config_path)
    ensure_runtime_dirs(fresh)
    setup_logging(str(fresh.paths.log))
    storage = Storage(fresh.paths.db)
    storage.init_db()
    notifier = Notifier(fresh.notify)
    result = run_cycle(cfg=fresh, storage=storage, notifier=notifier, dry_run=False)
    return {
        "ts": result.ts,
        "mode": result.mode,
        "backend": result.backend,
        "action": result.action,
        "success": result.success,
        "message": result.message,
    }


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


def _clear_limit_now(cfg: AppConfig) -> dict[str, object]:
    fresh = load_config(cfg.config_path)
    ensure_runtime_dirs(fresh)
    setup_logging(str(fresh.paths.log))
    storage = Storage(fresh.paths.db)
    storage.init_db()

    router = _build_router(fresh)
    backend_name, executor, status = router.choose()
    if executor is None or status is None or not status.available:
        raise RuntimeError("no available executor to clear charging limit")

    result = executor.clear_limit()
    now = utc_now()
    ts = now.isoformat()

    storage.insert_action(
        ts=ts,
        action_type=ActionType.CLEAR_LIMIT.value,
        backend=result.backend,
        target_percent=None,
        success=result.success,
        error_code=None if result.success else result.error_code or "EXEC_FAILED",
        error_msg=result.error_msg,
    )

    engine = PolicyEngine(cfg=fresh, storage=storage)
    snapshot = engine.load_runtime(now=now)
    if result.success:
        snapshot.charging_paused = False
        snapshot.consecutive_failures = 0
        snapshot.last_error = None
        snapshot.last_action_at = ts
        snapshot.last_backend = result.backend
    else:
        snapshot.last_error = result.error_msg or "failed to clear limit"
        snapshot.last_action_at = ts
        snapshot.last_backend = result.backend
    engine.persist_runtime(snapshot, now=now)

    return {
        "ts": ts,
        "mode": snapshot.mode.value,
        "backend": result.backend,
        "action": ActionType.CLEAR_LIMIT.value,
        "success": result.success,
        "message": "project control disabled; restored system charging" if result.success else (result.error_msg or "clear limit failed"),
    }


def _save_settings_and_apply(
    cfg: AppConfig,
    *,
    stop_percent: int,
    resume_percent: int,
    enabled: bool,
) -> dict[str, object]:
    updated = update_dashboard_settings(
        cfg.config_path,
        stop_percent=stop_percent,
        resume_percent=resume_percent,
        enabled=enabled,
    )

    if not enabled:
        result = _clear_limit_now(updated)
    else:
        result = _enforce_once(updated)

    return {
        "ok": True,
        "policy": {
            "stop_percent": updated.policy.stop_percent,
            "resume_percent": updated.policy.resume_percent,
            "enabled": updated.control.enabled,
        },
        "apply": result,
    }


def _html() -> str:
    return """<!doctype html>
<html lang=\"zh-CN\" data-theme=\"dark\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>电池接管 Dashboard</title>
  <style>
    :root {
      --bg-primary: #0a0f0d;
      --bg-secondary: #111a15;
      --bg-card: rgba(255,255,255,0.04);
      --text-primary: #e8f0e9;
      --text-secondary: #7a9e85;
      --accent: #3ddc84;
      --accent-dim: #2a9d5c;
      --warn: #f0a030;
      --bad: #e04545;
      --border: rgba(255,255,255,0.08);
      --radius: 16px;
      --shadow: 0 8px 32px rgba(0,0,0,0.4);
      --transition: all 0.3s ease;
    }
    [data-theme=\"light\"] {
      --bg-primary: #f5f7f5;
      --bg-secondary: #e8f0e8;
      --bg-card: rgba(255,255,255,0.72);
      --text-primary: #13251a;
      --text-secondary: #4f6657;
      --accent: #2a9d5c;
      --accent-dim: #1f7049;
      --warn: #b07400;
      --bad: #b03636;
      --border: rgba(0,0,0,0.08);
      --shadow: 0 8px 32px rgba(0,0,0,0.08);
    }
    * { box-sizing: border-box; }
    html { transition: var(--transition); }
    body {
      margin: 0;
      font-family: \"IBM Plex Sans\", \"Source Han Sans SC\", \"Noto Sans CJK SC\", sans-serif;
      color: var(--text-primary);
      background: var(--bg-primary);
      min-height: 100vh;
      transition: var(--transition);
    }
    .wrap { max-width: 1200px; margin: 0 auto; padding: 24px 20px 40px; }

    /* Header */
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 24px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }
    .header-left { display: flex; flex-direction: column; gap: 4px; }
    .title { margin: 0; font-size: clamp(24px, 3vw, 32px); font-weight: 700; letter-spacing: -0.5px; }
    .subtitle { margin: 0; color: var(--text-secondary); font-size: 13px; display: flex; align-items: center; gap: 8px; }
    .header-actions { display: flex; align-items: center; gap: 12px; }
    .icon-btn {
      width: 36px; height: 36px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: var(--bg-card);
      color: var(--text-secondary);
      cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      transition: var(--transition);
    }
    .icon-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
    .icon-btn.spinning svg { animation: spin 1s linear infinite; }
    @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
    .refresh-countdown {
      font-size: 12px;
      color: var(--text-secondary);
      min-width: 28px;
      text-align: center;
    }

    /* Grid Layout */
    .grid {
      display: grid;
      grid-template-columns: 280px 1fr 260px;
      gap: 16px;
      margin-bottom: 16px;
    }
    .grid-bottom {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-bottom: 16px;
    }
    .card {
      background: var(--bg-card);
      backdrop-filter: blur(12px);
      border-radius: var(--radius);
      padding: 20px;
      box-shadow: var(--shadow);
      border: 1px solid var(--border);
      transition: var(--transition);
    }
    .card-title {
      font-size: 12px;
      font-weight: 600;
      color: var(--text-secondary);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 16px;
    }

    /* Battery Ring */
    .battery-ring-container {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 20px 0;
    }
    .battery-ring-svg { transform: rotate(-90deg); }
    .battery-ring-track { fill: none; stroke: var(--border); stroke-width: 8; }
    .battery-ring-fill {
      fill: none;
      stroke-width: 8;
      stroke-linecap: round;
      transition: stroke-dashoffset 0.8s ease, stroke 0.5s ease;
    }
    .battery-ring-text {
      position: absolute;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
    }
    .battery-percent {
      font-size: 48px;
      font-weight: 700;
      line-height: 1;
      transition: color 0.5s ease;
    }
    .battery-label {
      font-size: 13px;
      color: var(--text-secondary);
      margin-top: 4px;
    }
    .battery-status {
      margin-top: 16px;
      font-size: 14px;
      color: var(--text-secondary);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .status-dot {
      width: 8px; height: 8px;
      border-radius: 50%;
      background: var(--accent);
      animation: pulse 2s ease-in-out infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.5; transform: scale(0.8); }
    }

    /* Chart */
    .chart-container { position: relative; }
    #curve {
      width: 100%; height: 260px;
      display: block;
      border-radius: 12px;
      overflow: visible;
    }
    .chart-tooltip {
      position: absolute;
      background: var(--bg-secondary);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 14px;
      font-size: 12px;
      pointer-events: none;
      opacity: 0;
      transition: opacity 0.2s;
      z-index: 100;
      box-shadow: var(--shadow);
      white-space: nowrap;
    }
    .chart-tooltip.visible { opacity: 1; }
    .chart-legend {
      display: flex;
      gap: 16px;
      margin-top: 12px;
      font-size: 12px;
      color: var(--text-secondary);
    }
    .legend-item { display: flex; align-items: center; gap: 6px; }
    .legend-line {
      width: 20px; height: 2px;
      border-radius: 1px;
    }
    .legend-line.dashed {
      background: repeating-linear-gradient(90deg, currentColor, currentColor 4px, transparent 4px, transparent 8px);
    }

    /* Side Panel */
    .side-panel { display: flex; flex-direction: column; gap: 20px; }
    .mode-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 16px;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 600;
      width: fit-content;
    }
    .mode-badge.ACTIVE_CONTROL {
      background: rgba(61,220,132,0.12);
      color: var(--accent);
      border: 1px solid rgba(61,220,132,0.2);
    }
    .mode-badge.ACTIVE_CONTROL .pulse-dot {
      background: var(--accent);
      animation: pulse 2s ease-in-out infinite;
    }
    .mode-badge.OBSERVE_ONLY {
      background: rgba(240,160,48,0.12);
      color: var(--warn);
      border: 1px solid rgba(240,160,48,0.2);
    }
    .mode-badge.OBSERVE_ONLY .pulse-dot { background: var(--warn); }
    .mode-badge.DEGRADED_READONLY {
      background: rgba(224,69,69,0.12);
      color: var(--bad);
      border: 1px solid rgba(224,69,69,0.2);
    }
    .mode-badge.DEGRADED_READONLY .pulse-dot { background: var(--bad); }
    .pulse-dot {
      width: 8px; height: 8px;
      border-radius: 50%;
    }
    .stat-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 10px 0;
      border-bottom: 1px solid var(--border);
      font-size: 13px;
    }
    .stat-row:last-child { border-bottom: none; }
    .stat-label { color: var(--text-secondary); }
    .stat-value { font-weight: 600; }
    .stat-value.ok { color: var(--accent); }
    .stat-value.bad { color: var(--bad); }
    .stat-value.warn { color: var(--warn); }

    /* Controls */
    .toggle-switch {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-radius: 12px;
      background: var(--bg-secondary);
      border: 1px solid var(--border);
      margin-bottom: 16px;
    }
    .toggle-info { display: flex; flex-direction: column; gap: 3px; }
    .toggle-info strong { font-size: 14px; }
    .toggle-info span { font-size: 12px; color: var(--text-secondary); }
    .switch-input {
      appearance: none;
      width: 52px;
      height: 28px;
      border-radius: 999px;
      background: var(--border);
      position: relative;
      cursor: pointer;
      transition: background 0.2s ease;
      flex-shrink: 0;
    }
    .switch-input::after {
      content: \"\";
      position: absolute;
      width: 22px;
      height: 22px;
      top: 3px;
      left: 3px;
      border-radius: 50%;
      background: #fff;
      box-shadow: 0 2px 6px rgba(0,0,0,0.2);
      transition: transform 0.2s ease;
    }
    .switch-input:checked { background: var(--accent); }
    .switch-input:checked::after { transform: translateX(24px); }

    .slider-group { margin-bottom: 16px; }
    .slider-label {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      font-size: 13px;
    }
    .slider-value {
      font-weight: 700;
      color: var(--accent);
      font-size: 16px;
      min-width: 32px;
      text-align: right;
    }
    .range-input {
      width: 100%;
      height: 6px;
      border-radius: 3px;
      -webkit-appearance: none;
      appearance: none;
      background: var(--bg-secondary);
      outline: none;
      cursor: pointer;
    }
    .range-input::-webkit-slider-thumb {
      -webkit-appearance: none;
      appearance: none;
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: var(--accent);
      cursor: pointer;
      box-shadow: 0 2px 8px rgba(61,220,132,0.3);
      transition: transform 0.15s ease;
    }
    .range-input::-webkit-slider-thumb:hover { transform: scale(1.2); }
    .range-input::-moz-range-thumb {
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: var(--accent);
      cursor: pointer;
      border: none;
      box-shadow: 0 2px 8px rgba(61,220,132,0.3);
    }

    .btn-group { display: flex; gap: 10px; margin-top: 16px; }
    .btn {
      flex: 1;
      border: none;
      border-radius: 10px;
      padding: 10px 16px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      background: var(--accent);
      color: #0a0f0d;
      transition: all 0.2s ease;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
    }
    .btn:hover { filter: brightness(1.1); transform: translateY(-1px); }
    .btn:active { transform: translateY(0); }
    .btn.secondary {
      background: var(--bg-secondary);
      color: var(--text-primary);
      border: 1px solid var(--border);
    }
    .btn.secondary:hover { background: var(--border); }
    .btn.loading { opacity: 0.7; pointer-events: none; }
    .hint { font-size: 12px; color: var(--text-secondary); margin-top: 12px; line-height: 1.5; }
    .msg { font-size: 13px; min-height: 20px; margin-top: 12px; font-weight: 500; }
    .msg.ok { color: var(--accent); }
    .msg.bad { color: var(--bad); }

    /* Timeline */
    .timeline { position: relative; padding-left: 24px; }
    .timeline::before {
      content: \"\";
      position: absolute;
      left: 7px;
      top: 0;
      bottom: 0;
      width: 2px;
      background: var(--border);
    }
    .timeline-item {
      position: relative;
      padding: 12px 0;
      border-bottom: 1px solid var(--border);
    }
    .timeline-item:last-child { border-bottom: none; }
    .timeline-dot {
      position: absolute;
      left: -20px;
      top: 16px;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--bg-secondary);
      border: 2px solid var(--border);
    }
    .timeline-dot.success { background: var(--accent); border-color: var(--accent); }
    .timeline-dot.error { background: var(--bad); border-color: var(--bad); }
    .timeline-time {
      font-size: 11px;
      color: var(--text-secondary);
      margin-bottom: 4px;
    }
    .timeline-content {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
    }
    .timeline-badge {
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 600;
      background: var(--bg-secondary);
    }
    .timeline-badge.success { color: var(--accent); }
    .timeline-badge.error { color: var(--bad); }
    .timeline-empty {
      color: var(--text-secondary);
      font-size: 13px;
      padding: 20px 0;
    }

    /* Footer */
    .footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 16px 20px;
      font-size: 12px;
      color: var(--text-secondary);
      border-top: 1px solid var(--border);
      margin-top: 8px;
    }
    .footer-paths { font-family: \"IBM Plex Mono\", \"Menlo\", monospace; }

    /* Toast */
    .toast-container {
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 1000;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .toast {
      background: var(--bg-secondary);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px 20px;
      box-shadow: var(--shadow);
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 13px;
      animation: toastIn 0.3s ease, toastOut 0.3s ease 2.7s forwards;
      max-width: 360px;
    }
    .toast.success { border-left: 3px solid var(--accent); }
    .toast.error { border-left: 3px solid var(--bad); }
    @keyframes toastIn {
      from { transform: translateX(100%); opacity: 0; }
      to { transform: translateX(0); opacity: 1; }
    }
    @keyframes toastOut {
      from { transform: translateX(0); opacity: 1; }
      to { transform: translateX(100%); opacity: 0; }
    }

    /* Skeleton */
    .skeleton {
      background: linear-gradient(90deg, var(--bg-secondary) 25%, var(--border) 50%, var(--bg-secondary) 75%);
      background-size: 200% 100%;
      animation: shimmer 1.5s infinite;
      border-radius: 6px;
    }
    @keyframes shimmer {
      0% { background-position: 200% 0; }
      100% { background-position: -200% 0; }
    }
    .skeleton-text { height: 14px; margin: 8px 0; }
    .skeleton-title { height: 12px; width: 60%; margin-bottom: 16px; }
    .skeleton-ring { width: 160px; height: 160px; border-radius: 50%; margin: 20px auto; }
    .skeleton-chart { height: 200px; margin: 10px 0; }

    /* Responsive */
    @media (max-width: 1024px) {
      .grid { grid-template-columns: 240px 1fr 220px; }
    }
    @media (max-width: 900px) {
      .grid { grid-template-columns: 1fr 1fr; }
      .grid-bottom { grid-template-columns: 1fr; }
      .chart-area { grid-column: span 2; }
      .side-panel { grid-column: span 2; flex-direction: row; flex-wrap: wrap; }
      .side-panel .card { flex: 1; min-width: 200px; }
    }
    @media (max-width: 600px) {
      .wrap { padding: 16px 12px 24px; }
      .grid { grid-template-columns: 1fr; }
      .grid-bottom { grid-template-columns: 1fr; }
      .chart-area { grid-column: span 1; }
      .side-panel { grid-column: span 1; flex-direction: column; }
      .header { flex-direction: column; align-items: flex-start; gap: 12px; }
      .btn-group { flex-direction: column; }
    }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <!-- Header -->
    <div class=\"header\">
      <div class=\"header-left\">
        <h1 class=\"title\">电池接管</h1>
        <p class=\"subtitle\">
          <span id=\"stamp\">加载中...</span>
          <span style=\"color: var(--border);\">|</span>
          <span>自动刷新 <span id=\"countdown\">10</span>s</span>
        </p>
      </div>
      <div class=\"header-actions\">
        <button class=\"icon-btn\" id=\"theme-btn\" title=\"切换主题\">
          <svg width=\"18\" height=\"18\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\">
            <circle cx=\"12\" cy=\"12\" r=\"5\"/>
            <line x1=\"12\" y1=\"1\" x2=\"12\" y2=\"3\"/><line x1=\"12\" y1=\"21\" x2=\"12\" y2=\"23\"/>
            <line x1=\"4.22\" y1=\"4.22\" x2=\"5.64\" y2=\"5.64\"/><line x1=\"18.36\" y1=\"18.36\" x2=\"19.78\" y2=\"19.78\"/>
            <line x1=\"1\" y1=\"12\" x2=\"3\" y2=\"12\"/><line x1=\"21\" y1=\"12\" x2=\"23\" y2=\"12\"/>
            <line x1=\"4.22\" y1=\"19.78\" x2=\"5.64\" y2=\"18.36\"/><line x1=\"18.36\" y1=\"5.64\" x2=\"19.78\" y2=\"4.22\"/>
          </svg>
        </button>
        <button class=\"icon-btn\" id=\"refresh-btn\" title=\"立即刷新\">
          <svg width=\"18\" height=\"18\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\">
            <polyline points=\"23 4 23 10 17 10\"/><polyline points=\"1 20 1 14 7 14\"/>
            <path d=\"M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15\"/>
          </svg>
        </button>
        <span class=\"refresh-countdown\" id=\"countdown-display\">10</span>
      </div>
    </div>

    <!-- Main Grid -->
    <div class=\"grid\">
      <!-- Battery Ring -->
      <div class=\"card\">
        <div class=\"card-title\">当前电量</div>
        <div class=\"battery-ring-container\" id=\"battery-skeleton\">
          <div style=\"position: relative; width: 180px; height: 180px;\">
            <svg class=\"battery-ring-svg\" width=\"180\" height=\"180\" viewBox=\"0 0 180 180\">
              <circle class=\"battery-ring-track\" cx=\"90\" cy=\"90\" r=\"78\" />
              <circle class=\"battery-ring-fill\" id=\"battery-ring\" cx=\"90\" cy=\"90\" r=\"78\"
                stroke-dasharray=\"490\" stroke-dashoffset=\"490\" />
            </svg>
            <div class=\"battery-ring-text\" style=\"position: absolute; top: 0; left: 0; right: 0; bottom: 0;\">
              <div class=\"battery-percent\" id=\"kpi-percent\">--</div>
              <div class=\"battery-label\">剩余电量</div>
            </div>
          </div>
          <div class=\"battery-status\">
            <span class=\"status-dot\" id=\"charging-dot\"></span>
            <span id=\"charging-status\">--</span>
          </div>
        </div>
      </div>

      <!-- Chart -->
      <div class=\"card chart-area\" style=\"position: relative;\">
        <div class=\"card-title\">24 小时电量趋势</div>
        <div id=\"chart-skeleton\">
          <svg id=\"curve\" viewBox=\"0 0 1000 260\" preserveAspectRatio=\"none\"></svg>
          <div class=\"chart-tooltip\" id=\"chart-tooltip\"></div>
        </div>
        <div class=\"chart-legend\">
          <div class=\"legend-item\">
            <div class=\"legend-line\" style=\"background: var(--accent);\"></div>
            <span>电量曲线</span>
          </div>
          <div class=\"legend-item\">
            <div class=\"legend-line dashed\" style=\"color: var(--bad);\"></div>
            <span>停充阈值</span>
          </div>
          <div class=\"legend-item\">
            <div class=\"legend-line dashed\" style=\"color: var(--accent);\"></div>
            <span>恢复阈值</span>
          </div>
        </div>
      </div>

      <!-- Side Panel -->
      <div class=\"side-panel\">
        <!-- Mode & Agent -->
        <div class=\"card\">
          <div class=\"card-title\">运行状态</div>
          <div style=\"display: flex; flex-direction: column; gap: 14px;\">
            <div>
              <div style=\"font-size: 12px; color: var(--text-secondary); margin-bottom: 6px;\">运行模式</div>
              <div class=\"mode-badge\" id=\"kpi-mode\">--</div>
            </div>
            <div>
              <div style=\"font-size: 12px; color: var(--text-secondary); margin-bottom: 6px;\">Agent 状态</div>
              <div class=\"stat-value\" id=\"kpi-agent\">--</div>
            </div>
            <div>
              <div style=\"font-size: 12px; color: var(--text-secondary); margin-bottom: 6px;\">24h 统计</div>
              <div class=\"stat-value\" id=\"kpi-24h\">--</div>
            </div>
          </div>
        </div>

        <!-- Current Status -->
        <div class=\"card\">
          <div class=\"card-title\">设备状态</div>
          <div class=\"stat-row\"><span class=\"stat-label\">插电状态</span><span class=\"stat-value\" id=\"st-ac\">--</span></div>
          <div class=\"stat-row\"><span class=\"stat-label\">循环次数</span><span class=\"stat-value\" id=\"st-cycle\">--</span></div>
          <div class=\"stat-row\"><span class=\"stat-label\">健康容量</span><span class=\"stat-value\" id=\"st-health\">--</span></div>
          <div class=\"stat-row\"><span class=\"stat-label\">最近动作</span><span class=\"stat-value\" id=\"st-action\">--</span></div>
          <div class=\"stat-row\"><span class=\"stat-label\">执行后端</span><span class=\"stat-value\" id=\"st-backend\">--</span></div>
        </div>
      </div>
    </div>

    <!-- Bottom Grid -->
    <div class=\"grid-bottom\">
      <!-- Controls -->
      <div class=\"card\">
        <div class=\"card-title\">阈值控制</div>
        <div class=\"toggle-switch\">
          <div class=\"toggle-info\">
            <strong id=\"mgmt-title\">项目电池管理</strong>
            <span id=\"mgmt-hint\">开启后按下方阈值控充；关闭后恢复系统默认充电规则。</span>
          </div>
          <input id=\"enabled-input\" class=\"switch-input\" type=\"checkbox\" />
        </div>
        <div class=\"slider-group\">
          <div class=\"slider-label\">
            <span>停充阈值</span>
            <span class=\"slider-value\" id=\"stop-value\">--%</span>
          </div>
          <input id=\"stop-input\" class=\"range-input\" type=\"range\" min=\"50\" max=\"100\" />
        </div>
        <div class=\"slider-group\">
          <div class=\"slider-label\">
            <span>恢复阈值</span>
            <span class=\"slider-value\" id=\"resume-value\">--%</span>
          </div>
          <input id=\"resume-input\" class=\"range-input\" type=\"range\" min=\"40\" max=\"99\" />
        </div>
        <div class=\"btn-group\">
          <button class=\"btn\" id=\"save-btn\">
            <svg width=\"14\" height=\"14\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\">
              <path d=\"M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z\"/><polyline points=\"17 21 17 13 7 13 7 21\"/><polyline points=\"7 3 7 8 15 8\"/>
            </svg>
            保存并应用
          </button>
          <button class=\"btn secondary\" id=\"enforce-btn\">
            <svg width=\"14\" height=\"14\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\">
              <polygon points=\"5 3 19 12 5 21 5 3\"/>
            </svg>
            立即执行
          </button>
        </div>
        <div class=\"hint\">居家时保持开关开启并设置 92/88；出门前关闭开关，系统会恢复正常充到 100%。</div>
        <div class=\"msg\" id=\"save-msg\"></div>
      </div>

      <!-- Timeline -->
      <div class=\"card\">
        <div class=\"card-title\">动作历史（最近 20 条）</div>
        <div class=\"timeline\" id=\"timeline-container\">
          <div class=\"timeline-empty\">加载中...</div>
        </div>
      </div>
    </div>

    <!-- Footer -->
    <div class=\"footer\">
      <span class=\"footer-paths\" id=\"paths\">--</span>
      <span>电池接管 v1.0</span>
    </div>
  </div>

  <!-- Toast Container -->
  <div class=\"toast-container\" id=\"toast-container\"></div>

<script>
const $ = (id) => document.getElementById(id);
let countdownValue = 10;
let countdownInterval;
let currentTheme = localStorage.getItem('theme') || 'dark';

document.documentElement.setAttribute('data-theme', currentTheme);

function fmtTs(ts) {
  if (!ts) return '-';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  const now = new Date();
  const isToday = d.toDateString() === now.toDateString();
  const timeStr = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  if (isToday) return `今天 ${timeStr}`;
  return `${d.getMonth()+1}/${d.getDate()} ${timeStr}`;
}

function fmtTsFull(ts) {
  if (!ts) return '-';
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString('zh-CN');
}

function getBatteryColor(percent) {
  if (percent <= 20) return 'var(--bad)';
  if (percent <= 50) return 'var(--warn)';
  return 'var(--accent)';
}

function animateNumber(el, target, suffix = '') {
  const start = parseFloat(el.textContent) || 0;
  if (isNaN(start) && el.textContent !== '--') return;
  const duration = 600;
  const startTime = performance.now();
  function update(currentTime) {
    const elapsed = currentTime - startTime;
    const progress = Math.min(elapsed / duration, 1);
    const easeOut = 1 - Math.pow(1 - progress, 3);
    const current = start + (target - start) * easeOut;
    if (Number.isInteger(target)) {
      el.textContent = Math.round(current) + suffix;
    } else {
      el.textContent = current.toFixed(1) + suffix;
    }
    if (progress < 1) requestAnimationFrame(update);
  }
  requestAnimationFrame(update);
}

function showToast(message, type = 'success') {
  const container = $('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  const icon = type === 'success'
    ? '<svg width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"var(--accent)\" stroke-width=\"2\"><polyline points=\"20 6 9 17 4 12\"/></svg>'
    : '<svg width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"var(--bad)\" stroke-width=\"2\"><circle cx=\"12\" cy=\"12\" r=\"10\"/><line x1=\"15\" y1=\"9\" x2=\"9\" y2=\"15\"/><line x1=\"9\" y1=\"9\" x2=\"15\" y2=\"15\"/></svg>';
  toast.innerHTML = `${icon}<span>${message}</span>`;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 3000);
}

function updateBatteryRing(percent) {
  const ring = $('battery-ring');
  const text = $('kpi-percent');
  if (percent == null) {
    text.textContent = '--';
    ring.style.strokeDashoffset = 490;
    return;
  }
  const circumference = 2 * Math.PI * 78;
  const offset = circumference - (percent / 100) * circumference;
  ring.style.strokeDasharray = circumference;
  ring.style.strokeDashoffset = offset;
  ring.style.stroke = getBatteryColor(percent);
  text.style.color = getBatteryColor(percent);
  animateNumber(text, percent, '%');
}

function drawCurve(samples, stopPercent, resumePercent) {
  const svg = $('curve');
  svg.innerHTML = '';
  if (!samples || !samples.length) {
    svg.innerHTML = '<text x=\"500\" y=\"130\" text-anchor=\"middle\" fill=\"var(--text-secondary)\" font-size=\"14\">暂无数据</text>';
    return;
  }

  const vals = samples.map(s => Number(s.percent));
  const minV = Math.max(0, Math.min(...vals) - 5);
  const maxV = Math.min(100, Math.max(...vals) + 5);
  const range = Math.max(maxV - minV, 1);

  const width = 1000;
  const height = 260;
  const padding = { top: 20, bottom: 40, left: 10, right: 10 };
  const chartW = width - padding.left - padding.right;
  const chartH = height - padding.top - padding.bottom;

  // Grid lines
  for (let i = 0; i <= 4; i++) {
    const y = padding.top + (chartH / 4) * i;
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', padding.left);
    line.setAttribute('y1', y);
    line.setAttribute('x2', width - padding.right);
    line.setAttribute('y2', y);
    line.setAttribute('stroke', 'var(--border)');
    line.setAttribute('stroke-width', '1');
    line.setAttribute('stroke-dasharray', '4,4');
    svg.appendChild(line);
  }

  // Threshold lines
  function drawThreshold(value, color) {
    const y = padding.top + chartH - ((value - minV) / range) * chartH;
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', padding.left);
    line.setAttribute('y1', y);
    line.setAttribute('x2', width - padding.right);
    line.setAttribute('y2', y);
    line.setAttribute('stroke', color);
    line.setAttribute('stroke-width', '2');
    line.setAttribute('stroke-dasharray', '8,4');
    line.setAttribute('opacity', '0.6');
    svg.appendChild(line);
  }
  if (stopPercent != null) drawThreshold(stopPercent, 'var(--bad)');
  if (resumePercent != null) drawThreshold(resumePercent, 'var(--accent)');

  // Area gradient
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  const gradient = document.createElementNS('http://www.w3.org/2000/svg', 'linearGradient');
  gradient.setAttribute('id', 'areaGradient');
  gradient.setAttribute('x1', '0');
  gradient.setAttribute('y1', '0');
  gradient.setAttribute('x2', '0');
  gradient.setAttribute('y2', '1');
  const stop1 = document.createElementNS('http://www.w3.org/2000/svg', 'stop');
  stop1.setAttribute('offset', '0%');
  stop1.setAttribute('stop-color', 'var(--accent)');
  stop1.setAttribute('stop-opacity', '0.2');
  const stop2 = document.createElementNS('http://www.w3.org/2000/svg', 'stop');
  stop2.setAttribute('offset', '100%');
  stop2.setAttribute('stop-color', 'var(--accent)');
  stop2.setAttribute('stop-opacity', '0');
  gradient.appendChild(stop1);
  gradient.appendChild(stop2);
  defs.appendChild(gradient);
  svg.appendChild(defs);

  // Points
  const points = samples.map((s, i) => {
    const x = padding.left + (i / Math.max(samples.length - 1, 1)) * chartW;
    const y = padding.top + chartH - ((Number(s.percent) - minV) / range) * chartH;
    return { x, y, percent: s.percent, ts: s.ts, on_ac: s.on_ac, charging: s.charging };
  });

  // Area
  const areaPoints = [
    `${points[0].x},${padding.top + chartH}`,
    ...points.map(p => `${p.x},${p.y}`),
    `${points[points.length - 1].x},${padding.top + chartH}`
  ].join(' ');
  const area = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
  area.setAttribute('points', areaPoints);
  area.setAttribute('fill', 'url(#areaGradient)');
  svg.appendChild(area);

  // Line
  const linePoints = points.map(p => `${p.x},${p.y}`).join(' ');
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
  line.setAttribute('points', linePoints);
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', 'var(--accent)');
  line.setAttribute('stroke-width', '2.5');
  line.setAttribute('stroke-linecap', 'round');
  line.setAttribute('stroke-linejoin', 'round');
  svg.appendChild(line);

  // Data points
  points.forEach((p, i) => {
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', p.x);
    circle.setAttribute('cy', p.y);
    circle.setAttribute('r', '3');
    circle.setAttribute('fill', 'var(--bg-primary)');
    circle.setAttribute('stroke', 'var(--accent)');
    circle.setAttribute('stroke-width', '2');
    circle.setAttribute('class', 'data-point');
    circle.style.cursor = 'pointer';
    circle.style.transition = 'r 0.2s';

    circle.addEventListener('mouseenter', () => {
      circle.setAttribute('r', '6');
      showTooltip(p, circle);
    });
    circle.addEventListener('mouseleave', () => {
      circle.setAttribute('r', '3');
      hideTooltip();
    });
    svg.appendChild(circle);
  });

  // X-axis labels
  const labelCount = Math.min(6, samples.length);
  for (let i = 0; i < labelCount; i++) {
    const idx = Math.floor((i / (labelCount - 1)) * (samples.length - 1));
    const x = padding.left + (idx / Math.max(samples.length - 1, 1)) * chartW;
    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x', x);
    text.setAttribute('y', height - 8);
    text.setAttribute('text-anchor', 'middle');
    text.setAttribute('fill', 'var(--text-secondary)');
    text.setAttribute('font-size', '11');
    const d = new Date(samples[idx].ts);
    text.textContent = isNaN(d.getTime()) ? '' : `${d.getHours()}:${String(d.getMinutes()).padStart(2,'0')}`;
    svg.appendChild(text);
  }
}

function showTooltip(data, target) {
  const tooltip = $('chart-tooltip');
  const svg = $('curve');
  const rect = svg.getBoundingClientRect();
  tooltip.innerHTML = `
    <div style=\"font-weight: 600; margin-bottom: 4px;\">${fmtTsFull(data.ts)}</div>
    <div style=\"display: flex; align-items: center; gap: 8px;\">
      <span style=\"color: var(--accent); font-weight: 700; font-size: 16px;\">${data.percent}%</span>
      <span style=\"color: var(--text-secondary);\">${data.charging ? '充电中' : (data.on_ac ? '插电' : '电池')}</span>
    </div>
  `;
  tooltip.classList.add('visible');

  const tooltipRect = tooltip.getBoundingClientRect();
  let left = rect.left + (data.x / 1000) * rect.width - tooltipRect.width / 2;
  let top = rect.top + (data.y / 260) * rect.height - tooltipRect.height - 12;
  left = Math.max(8, Math.min(left, window.innerWidth - tooltipRect.width - 8));
  top = Math.max(8, top);
  tooltip.style.left = left + 'px';
  tooltip.style.top = top + 'px';
}

function hideTooltip() {
  $('chart-tooltip').classList.remove('visible');
}

function renderTimeline(actions) {
  const container = $('timeline-container');
  container.innerHTML = '';
  const rows = actions.slice(-20).reverse();
  if (!rows.length) {
    container.innerHTML = '<div class=\"timeline-empty\">暂无动作记录</div>';
    return;
  }
  for (const a of rows) {
    const ok = Number(a.success) === 1;
    const item = document.createElement('div');
    item.className = 'timeline-item';
    item.innerHTML = `
      <div class=\"timeline-dot ${ok ? 'success' : 'error'}\"></div>
      <div class=\"timeline-time\">${fmtTsFull(a.ts)}</div>
      <div class=\"timeline-content\">
        <span class=\"timeline-badge ${ok ? 'success' : 'error'}\">${ok ? '成功' : '失败'}</span>
        <span>${a.action_type || '-'}</span>
        ${a.target_percent != null ? `<span style=\"color: var(--text-secondary);\">目标 ${a.target_percent}%</span>` : ''}
        ${a.backend ? `<span style=\"color: var(--text-secondary); font-size: 11px;\">${a.backend}</span>` : ''}
      </div>
      ${a.error_msg ? `<div style=\"font-size: 11px; color: var(--bad); margin-top: 4px;\">${a.error_msg}</div>` : ''}
    `;
    container.appendChild(item);
  }
}

function syncControlForm(enabled) {
  $('mgmt-title').textContent = enabled ? '项目电池管理：开启' : '项目电池管理：关闭';
  $('mgmt-hint').textContent = enabled
    ? '开启后按下方阈值控充。当前更适合长期插电。'
    : '关闭后会清除项目限充，让系统按默认规则继续充电。';
  $('stop-input').disabled = !enabled;
  $('resume-input').disabled = !enabled;
  $('save-btn').disabled = !enabled;
  if (!enabled) {
    $('save-btn').style.opacity = '0.5';
    $('save-btn').style.pointerEvents = 'none';
  } else {
    $('save-btn').style.opacity = '1';
    $('save-btn').style.pointerEvents = 'auto';
  }
}

async function loadPolicy() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) throw new Error(`/api/config returned ${res.status}`);
    const cfg = await res.json();
    $('stop-input').value = cfg.stop_percent;
    $('resume-input').value = cfg.resume_percent;
    $('stop-value').textContent = cfg.stop_percent + '%';
    $('resume-value').textContent = cfg.resume_percent + '%';
    $('enabled-input').checked = Boolean(cfg.enabled);
    syncControlForm(Boolean(cfg.enabled));
    return cfg;
  } catch (err) {
    showToast('配置加载失败: ' + err.message, 'error');
    return null;
  }
}

async function savePolicy() {
  const btn = $('save-btn');
  btn.classList.add('loading');
  const stop = Number($('stop-input').value);
  const resume = Number($('resume-input').value);
  const enabled = $('enabled-input').checked;
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stop_percent: stop, resume_percent: resume, enabled }),
    });
    const payload = await res.json();
    if (!res.ok) {
      showToast('保存失败: ' + (payload.error || 'unknown error'), 'error');
      return;
    }
    syncControlForm(Boolean(payload.policy.enabled));
    showToast('设置已保存并应用');
    await refresh();
  } catch (err) {
    showToast('保存失败: ' + err.message, 'error');
  } finally {
    btn.classList.remove('loading');
  }
}

async function enforceNow() {
  const btn = $('enforce-btn');
  btn.classList.add('loading');
  try {
    const res = await fetch('/api/enforce-now', { method: 'POST' });
    const payload = await res.json();
    if (!res.ok) {
      showToast('执行失败: ' + (payload.error || 'unknown error'), 'error');
      return;
    }
    showToast(`执行结果: ${payload.action} (${payload.message})`, payload.success ? 'success' : 'error');
    await refresh();
  } catch (err) {
    showToast('执行失败: ' + err.message, 'error');
  } finally {
    btn.classList.remove('loading');
  }
}

async function refresh() {
  const refreshBtn = $('refresh-btn');
  refreshBtn.classList.add('spinning');
  try {
    const [ovRes, histRes] = await Promise.all([
      fetch('/api/overview'),
      fetch('/api/history?hours=24'),
    ]);
    if (!ovRes.ok) throw new Error(`/api/overview returned ${ovRes.status}`);
    if (!histRes.ok) throw new Error(`/api/history returned ${histRes.status}`);
    const ov = await ovRes.json();
    const hist = await histRes.json();

    $('stamp').textContent = `最后刷新：${fmtTsFull(ov.generated_at)}`;

    const s = ov.latest_sample || {};
    const a = ov.latest_action || {};
    const mode = (ov.runtime_state || {}).mode || '-';

    updateBatteryRing(s.percent);
    animateNumber($('kpi-24h'), ov.sample_count_24h, ' / ' + ov.action_count_24h);

    const modeEl = $('kpi-mode');
    modeEl.textContent = mode;
    modeEl.className = `mode-badge ${mode}`;
    modeEl.innerHTML = `<span class=\"pulse-dot\"></span>${mode === 'ACTIVE_CONTROL' ? '主动控制' : mode === 'OBSERVE_ONLY' ? '仅观察' : '降级只读'}`;

    const agent = ov.agent || {};
    const agentEl = $('kpi-agent');
    agentEl.textContent = agent.running ? `运行中 (#${agent.pid})` : '未运行';
    agentEl.className = `stat-value ${agent.running ? 'ok' : 'bad'}`;

    $('st-ac').textContent = Number(s.on_ac) === 1 ? '已插电' : (s.on_ac == null ? '-' : '未插电');
    $('st-cycle').textContent = s.cycle_count ?? '-';
    $('st-health').textContent = s.max_capacity_pct != null ? `${s.max_capacity_pct}%` : '-';
    $('st-action').textContent = a.action_type || '-';
    $('st-backend').textContent = a.backend || '-';

    const chargingStatus = $('charging-status');
    const chargingDot = $('charging-dot');
    if (Number(s.charging) === 1) {
      chargingStatus.textContent = '充电中';
      chargingDot.style.background = 'var(--accent)';
      chargingDot.style.animation = 'pulse 2s ease-in-out infinite';
    } else {
      chargingStatus.textContent = s.on_ac ? '已充满 / 未充电' : '使用电池';
      chargingDot.style.background = 'var(--text-secondary)';
      chargingDot.style.animation = 'none';
    }

    const cfg = await (await fetch('/api/config')).json();
    syncControlForm(Boolean(cfg.enabled));

    $('paths').textContent = `DB: ${ov.paths.db} | LOG: ${ov.paths.log} | REPORTS: ${ov.paths.reports_dir}`;

    drawCurve(hist.samples || [], cfg.stop_percent, cfg.resume_percent);
    renderTimeline(hist.actions || []);
  } catch (err) {
    $('stamp').textContent = `数据刷新失败：${err.message}`;
    showToast('数据刷新失败: ' + err.message, 'error');
  } finally {
    refreshBtn.classList.remove('spinning');
    countdownValue = 10;
  }
}

function startCountdown() {
  if (countdownInterval) clearInterval(countdownInterval);
  countdownInterval = setInterval(() => {
    countdownValue--;
    if (countdownValue <= 0) countdownValue = 10;
    $('countdown').textContent = countdownValue;
    $('countdown-display').textContent = countdownValue;
  }, 1000);
}

function toggleTheme() {
  currentTheme = currentTheme === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', currentTheme);
  localStorage.setItem('theme', currentTheme);
}

// Event listeners
$('save-btn').addEventListener('click', savePolicy);
$('enforce-btn').addEventListener('click', enforceNow);
$('refresh-btn').addEventListener('click', refresh);
$('theme-btn').addEventListener('click', toggleTheme);
$('enabled-input').addEventListener('change', (e) => syncControlForm(Boolean(e.target.checked)));
$('stop-input').addEventListener('input', (e) => $('stop-value').textContent = e.target.value + '%');
$('resume-input').addEventListener('input', (e) => $('resume-value').textContent = e.target.value + '%');

// Init
loadPolicy();
refresh();
setInterval(refresh, 10000);
startCountdown();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    server: "DashboardServer"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self._send_html(_html())
                return

            if path == "/api/overview":
                payload = _build_overview(self.server.cfg)
                self._send_json(payload)
                return

            if path == "/api/history":
                query = parse_qs(parsed.query)
                try:
                    hours = int(query.get("hours", ["24"])[0])
                except Exception:
                    hours = 24
                hours = max(1, min(hours, 168))
                payload = _build_history(self.server.cfg, hours)
                self._send_json(payload)
                return

            if path == "/api/config":
                payload = _read_policy_config(self.server.cfg)
                self._send_json(payload)
                return

            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
        except Exception as exc:
            if path.startswith("/api/"):
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/config":
            data = self._read_json_body()
            if data is None:
                self._send_json({"error": "invalid json body"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                stop = int(data.get("stop_percent"))
                resume = int(data.get("resume_percent"))
                enabled = bool(data.get("enabled", True))
                # Input validation for bounds
                if not (0 <= stop <= 100):
                    self._send_json({"error": "stop_percent must be between 0 and 100"}, status=HTTPStatus.BAD_REQUEST)
                    return
                if not (0 <= resume <= 100):
                    self._send_json({"error": "resume_percent must be between 0 and 100"}, status=HTTPStatus.BAD_REQUEST)
                    return
                payload = _save_settings_and_apply(
                    self.server.cfg,
                    stop_percent=stop,
                    resume_percent=resume,
                    enabled=enabled,
                )
                self.server.cfg = load_config(self.server.cfg.config_path)
                self._send_json(payload)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

        if path == "/api/enforce-now":
            try:
                payload = _enforce_once(self.server.cfg)
                self._send_json(payload)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def log_message(self, format: str, *args) -> None:
        # Keep dashboard output clean for terminal users.
        return

    def _send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict[str, object] | None:
        try:
            raw_len = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if raw_len <= 0:
            return {}
        if raw_len > MAX_BODY_SIZE:
            self._send_json({"error": "request body too large"}, status=HTTPStatus.PAYLOAD_TOO_LARGE)
            return None
        raw = self.rfile.read(raw_len)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        return data


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, host: str, port: int, cfg: AppConfig):
        self.cfg = cfg
        super().__init__((host, port), _Handler)

    def server_bind(self):
        try:
            super().server_bind()
        except OSError as e:
            if e.errno == 48 or "Address already in use" in str(e):  # EADDRINUSE
                raise OSError(
                    f"Port {self.server_address[1]} is already in use by another process. "
                    f"Please stop the other application before starting BatteryTakeover Dashboard."
                )
            raise


def run_dashboard(cfg: AppConfig, host: str, port: int, open_browser: bool) -> int:
    if not _is_loopback_host(host):
        print(
            f"[dashboard] ERROR: refusing to bind control dashboard to non-loopback host {host!r}",
            file=sys.stderr,
        )
        return 2
    try:
        server = DashboardServer(host=host, port=port, cfg=cfg)
    except OSError as e:
        if "Port" in str(e) and "already in use" in str(e):
            print(f"[dashboard] ERROR: {e}", file=sys.stderr)
            return 1
        raise
    url = f"http://{host}:{port}"
    print(f"[dashboard] serving on {url}")
    if open_browser:
        try:
            subprocess.run(["open", url], check=False)
        except Exception:
            pass
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
