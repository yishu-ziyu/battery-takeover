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


def _build_window_summary(cfg: AppConfig, hours: int = 24) -> dict[str, object]:
    history = _build_history(cfg, hours)
    samples = history["samples"]
    actions = history["actions"]
    percents = [int(row["percent"]) for row in samples]
    action_total = len(actions)
    action_failures = sum(1 for row in actions if int(row["success"]) == 0)
    in_target = [
        p
        for p in percents
        if cfg.policy.resume_percent <= p <= cfg.policy.stop_percent
    ]
    above_stop = [p for p in percents if p > cfg.policy.stop_percent]

    return {
        "hours": hours,
        "sample_count": len(samples),
        "action_count": action_total,
        "failure_count": action_failures,
        "success_rate": round(((action_total - action_failures) / action_total) * 100, 1) if action_total else None,
        "min_percent": min(percents) if percents else None,
        "max_percent": max(percents) if percents else None,
        "avg_percent": round(sum(percents) / len(percents), 1) if percents else None,
        "target_band_percent": round((len(in_target) / len(percents)) * 100, 1) if percents else None,
        "above_stop_count": len(above_stop),
    }


def _explain_current_state(
    cfg: AppConfig,
    latest_sample: dict[str, object] | None,
    runtime_state: dict[str, str],
    latest_action: dict[str, object] | None,
) -> dict[str, str]:
    mode = runtime_state.get("mode", "OBSERVE_ONLY")
    if not latest_sample:
        return {
            "level": "warn",
            "title": "还没有采样数据",
            "body": "请先运行采样或等待 Agent 写入第一条记录。",
            "next": "运行 btake sample 或检查 LaunchAgent。",
        }

    percent = int(latest_sample["percent"])
    on_ac = int(latest_sample["on_ac"]) == 1
    charging = int(latest_sample["charging"]) == 1
    charging_paused = runtime_state.get("charging_paused") in {"1", "true", "True"}
    last_error = runtime_state.get("last_error") or ""
    stop = cfg.policy.stop_percent
    resume = cfg.policy.resume_percent

    if mode == "DEGRADED_READONLY":
        return {
            "level": "bad",
            "title": "已降级为只读",
            "body": f"最近错误：{last_error or '执行器或采集状态异常'}。系统不会继续调整充电策略。",
            "next": "先运行 doctor --repair，确认 batt daemon 和采集命令恢复。",
        }

    if not cfg.control.enabled:
        return {
            "level": "warn",
            "title": "项目电池管理已关闭",
            "body": "本工具当前不限制充电，Mac 会按系统设置或其他电池工具运行。",
            "next": "需要长期插电保护时，开启项目电池管理并保存应用。",
        }

    if not on_ac:
        return {
            "level": "warn",
            "title": "未接入电源",
            "body": f"当前 {percent}%，策略保持观察；接入电源后才会按 {resume}-{stop}% 区间控充。",
            "next": "接入电源后等待下一次 Agent 周期，或手动执行策略。",
        }

    if percent >= stop and charging_paused:
        return {
            "level": "ok",
            "title": "已接管并暂停充电",
            "body": f"当前 {percent}%，高于 {stop}% 上限，正在避免继续充电。",
            "next": f"电量低于 {resume}% 后会恢复充电；出门前可关闭接管恢复系统充电。",
        }

    if percent <= resume and not charging_paused:
        return {
            "level": "ok",
            "title": "低于恢复阈值，允许充电",
            "body": f"当前 {percent}%，低于 {resume}% 恢复线，策略允许充到 {stop}%。",
            "next": "保持接通电源即可，Agent 会继续巡检。",
        }

    if charging:
        title = "正在区间内充电"
        body = f"当前 {percent}%，目标区间是 {resume}-{stop}%。"
    else:
        title = "区间内保持"
        body = f"当前 {percent}%，在目标区间附近；最近动作是 {(latest_action or {}).get('action_type') or '无'}。"
    return {
        "level": "ok",
        "title": title,
        "body": body,
        "next": "如状态不符合预期，先查看最近动作和错误列。",
    }


def _build_product_snapshot(cfg: AppConfig) -> dict[str, object]:
    overview = _build_overview(cfg)
    latest_sample = overview["latest_sample"]
    latest_action = overview["latest_action"]
    runtime_state = overview["runtime_state"]
    policy = _read_policy_config(cfg)
    window = _build_window_summary(cfg, hours=24)
    explanation = _explain_current_state(cfg, latest_sample, runtime_state, latest_action)
    return {
        "overview": overview,
        "policy": policy,
        "window": window,
        "explanation": explanation,
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
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>电池接管 Dashboard</title>
  <style>
    :root {
      --bg: #f7f8f8;
      --rail: #eef2f0;
      --surface: #ffffff;
      --surface-soft: #f2f5f3;
      --ink: #17201d;
      --muted: #62706a;
      --quiet: #87938e;
      --accent: #0f7a4f;
      --accent-weak: #e5f3ec;
      --warn: #b56b00;
      --warn-weak: #fff4df;
      --bad: #b03636;
      --bad-weak: #fdecec;
      --line: #0f7a4f;
      --border: rgba(23, 32, 29, 0.12);
      --radius: 8px;
      --shadow: 0 10px 24px rgba(20, 28, 34, 0.07);
      --rail-w: 248px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, \"SF Pro Text\", \"Source Han Sans SC\", \"Noto Sans CJK SC\", sans-serif;
      color: var(--ink);
      background: var(--bg);
      min-height: 100vh;
      font-size: 14px;
      line-height: 1.5;
    }
    button, input { font: inherit; }
    .app {
      min-height: 100vh;
      display: grid;
      grid-template-columns: var(--rail-w) minmax(0, 1fr);
    }
    .rail {
      position: sticky;
      top: 0;
      height: 100vh;
      display: flex;
      flex-direction: column;
      padding: 18px 16px;
      background: var(--rail);
      border-right: 1px solid var(--border);
    }
    .brand { display: flex; align-items: center; gap: 12px; margin-bottom: 28px; }
    .brand-mark {
      width: 44px;
      height: 44px;
      border-radius: var(--radius);
      display: grid;
      place-items: center;
      color: #fff;
      background: var(--accent);
      box-shadow: inset 0 -12px 20px rgba(0,0,0,.08);
    }
    .brand-title { font-size: 22px; font-weight: 690; line-height: 1.1; letter-spacing: -0.01em; }
    .brand-version { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .nav { display: grid; gap: 6px; }
    .nav-item {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      border-radius: var(--radius);
      color: #344039;
      font-weight: 560;
      letter-spacing: 0.01em;
    }
    .nav-item.active { background: rgba(15, 122, 79, .1); color: var(--accent); }
    .nav svg, .brand svg { width: 20px; height: 20px; stroke-width: 1.8; }
    .rail-foot { margin-top: auto; display: grid; gap: 10px; }
    .local-box {
      padding: 12px;
      border-radius: var(--radius);
      background: rgba(255,255,255,.68);
      border: 1px solid var(--border);
    }
    .local-title { display: flex; align-items: center; gap: 8px; color: var(--accent); font-weight: 650; }
    .local-text { color: var(--muted); font-size: 12px; margin-top: 4px; }
    .content { min-width: 0; padding: 18px 24px 30px; }
    .status-strip {
      display: grid;
      grid-template-columns: 1.4fr 1fr 1fr 1fr;
      gap: 1px;
      overflow: hidden;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--border);
      box-shadow: var(--shadow);
      margin-bottom: 14px;
    }
    .status-cell {
      min-width: 0;
      background: rgba(255,255,255,.88);
      padding: 13px 16px;
      display: flex;
      align-items: center;
      gap: 8px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .status-label {
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      flex: none;
    }
    .status-value { font-weight: 650; overflow: hidden; text-overflow: ellipsis; }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--accent); flex: none; }
    .dot.warn { background: var(--warn); }
    .dot.bad { background: var(--bad); }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }
    .panel {
      background: var(--surface);
      border-radius: var(--radius);
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
      min-width: 0;
    }
    .panel-pad { padding: 16px; }
    .hero { grid-column: span 8; padding: 20px 22px; }
    .control-panel { grid-column: span 4; padding: 16px; }
    .chart { grid-column: span 8; padding: 16px; }
    .side { grid-column: span 4; padding: 16px; }
    .wide { grid-column: span 12; padding: 16px; }
    .half { grid-column: span 6; padding: 16px; }
    .k { color: var(--muted); font-size: 12px; margin-bottom: 4px; letter-spacing: 0.02em; }
    .v { font-size: 28px; font-weight: 680; line-height: 1.05; letter-spacing: -0.02em; }
    .hero-grid { display: grid; grid-template-columns: 132px minmax(180px, .8fr) 1px minmax(240px, 1fr); gap: 24px; align-items: center; }
    .hero-checks {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px;
      margin-top: 26px;
    }
    .hero-check {
      border-top: 1px solid var(--border);
      padding-top: 12px;
    }
    .hero-check span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.02em;
    }
    .hero-check strong {
      display: block;
      margin-top: 4px;
      font-size: 16px;
      font-weight: 670;
      letter-spacing: -0.01em;
    }
    .battery-shell {
      width: 104px;
      height: 170px;
      margin: 0 auto;
      padding: 8px;
      border: 6px solid #d4dbd7;
      border-radius: 20px;
      position: relative;
      background: #f3f6f4;
    }
    .battery-shell::before {
      content: "";
      width: 58px;
      height: 12px;
      border-radius: 8px 8px 0 0;
      background: #d4dbd7;
      position: absolute;
      left: 50%;
      top: -18px;
      transform: translateX(-50%);
    }
    .battery-fill {
      position: absolute;
      left: 8px;
      right: 8px;
      bottom: 8px;
      height: 50%;
      border-radius: 12px;
      background: var(--accent);
      transition: height .24s ease;
    }
    .battery-gauge-text {
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: white;
      font-size: 26px;
      font-weight: 700;
      letter-spacing: -0.02em;
      z-index: 1;
      text-shadow: 0 1px 4px rgba(0,0,0,.2);
    }
    .hero-percent { font-size: 46px; font-weight: 700; line-height: 1; letter-spacing: -0.03em; }
    .hero-state { color: var(--accent); font-size: 20px; font-weight: 690; margin: 14px 0 6px; letter-spacing: -0.01em; }
    .hero-copy { color: var(--muted); margin: 0; max-width: 42ch; }
    .divider { width: 1px; height: 100%; background: var(--border); }
    .policy-title { color: #303b35; font-weight: 640; }
    .policy-band { color: var(--accent); font-size: 32px; font-weight: 720; letter-spacing: -0.02em; margin: 4px 0 12px; }
    .policy-list { display: grid; grid-template-columns: minmax(100px, 1fr) auto; gap: 6px 22px; font-size: 13px; }
    .policy-list span:nth-child(odd) { color: var(--muted); }
    .mode { display: inline-block; padding: 4px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
    .mode.ACTIVE_CONTROL { background: var(--accent-weak); color: var(--accent); }
    .mode.OBSERVE_ONLY { background: rgba(176,116,0,.14); color: var(--warn); }
    .mode.DEGRADED_READONLY { background: var(--bad-weak); color: var(--bad); }
    .pill { display: inline-flex; align-items: center; border-radius: 999px; padding: 3px 8px; font-size: 12px; font-weight: 650; }
    .pill.ok { background: var(--accent-weak); color: var(--accent); }
    .pill.warn { background: var(--warn-weak); color: var(--warn); }
    .pill.bad { background: var(--bad-weak); color: var(--bad); }
    #curve {
      width: 100%; height: 220px; display: block;
      background: var(--surface-soft);
      border-radius: 8px; border: 1px solid rgba(15,98,64,0.12);
    }
    .meta { margin-top: 8px; font-size: 12px; color: var(--muted); }
    .table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius); }
    table { width: 100%; min-width: 720px; border-collapse: collapse; font-size: 12px; }
    th, td { padding: 8px 6px; text-align: left; border-bottom: 1px dashed rgba(19,37,26,0.18); }
    th { color: var(--muted); font-weight: 600; }
    .ok { color: var(--accent); font-weight: 600; }
    .bad { color: var(--bad); font-weight: 600; }
    .row { display: flex; justify-content: space-between; gap: 10px; padding: 4px 0; font-size: 13px; }
    .mono { font-family: \"SF Mono\", \"Menlo\", monospace; font-size: 12px; overflow-wrap: anywhere; color: #344039; }
    .explain { display: grid; gap: 8px; }
    .explain-title { font-size: 20px; font-weight: 700; letter-spacing: -0.01em; }
    .explain-body { color: var(--ink); line-height: 1.45; font-size: 14px; }
    .explain-next { color: var(--muted); line-height: 1.4; font-size: 12px; padding-top: 4px; }
    .stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
    .stat { border-top: 1px solid var(--border); padding: 10px 0 6px; }
    .stat .label { color: var(--muted); font-size: 12px; margin-bottom: 5px; }
    .stat .num { font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; }
    .section-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 10px; }
    .ctrl { display: flex; flex-direction: column; gap: 10px; }
    .ctrl-row { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center; }
    .stepper {
      display: grid;
      grid-template-columns: 38px 76px 38px;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      overflow: hidden;
      background: var(--surface);
    }
    .stepper button {
      border: 0;
      background: var(--surface-soft);
      color: #2e3933;
      cursor: pointer;
      font-size: 20px;
    }
    .stepper input {
      border: 0;
      border-inline: 1px solid var(--border);
      text-align: center;
      border-radius: 0;
      width: 76px;
      background: #fff;
    }
    .toggle {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-radius: 8px;
      background: rgba(19, 37, 26, 0.05);
      border: 1px solid rgba(19, 37, 26, 0.08);
    }
    .toggle-copy { display: flex; flex-direction: column; gap: 3px; }
    .toggle-copy strong { font-size: 14px; }
    .toggle-copy span { font-size: 12px; color: var(--muted); }
    .switch {
      appearance: none;
      width: 54px;
      height: 31px;
      border-radius: 999px;
      background: #8fa194;
      position: relative;
      cursor: pointer;
      transition: background .16s ease;
    }
    .switch::after {
      content: "";
      position: absolute;
      width: 23px;
      height: 23px;
      top: 4px;
      left: 4px;
      border-radius: 50%;
      background: #fff;
      box-shadow: 0 2px 8px rgba(0,0,0,.18);
      transition: transform .16s ease;
    }
    .switch:checked { background: #1f7049; }
    .switch:checked::after { transform: translateX(23px); }
    .ctrl input {
      padding: 6px 8px;
      border-radius: 8px;
      border: 1px solid rgba(19, 37, 26, .25);
      font-size: 14px;
      background: rgba(255,255,255,0.95);
      color: var(--ink);
    }
    .btn {
      border: none;
      border-radius: 8px;
      padding: 9px 12px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      background: var(--accent);
      color: #fff;
      transition: transform .14s ease, opacity .14s ease;
    }
    .btn:hover { opacity: .92; }
    .btn:active { transform: translateY(1px); }
    .btn.secondary {
      background: #28483a;
    }
    .btn.ghost {
      background: transparent;
      color: #304139;
      border: 1px solid var(--border);
    }
    .hint { font-size: 12px; color: var(--muted); }
    .notice {
      display: flex;
      gap: 8px;
      align-items: flex-start;
      padding: 10px;
      border-radius: var(--radius);
      border: 1px solid rgba(181,107,0,.24);
      background: var(--warn-weak);
      color: #7b4a00;
      font-size: 12px;
      line-height: 1.45;
    }
    #save-msg { font-size: 12px; min-height: 18px; }
    .empty-row { color: var(--muted); text-align: center; padding: 16px 6px; }
    .skeleton {
      position: relative;
      overflow: hidden;
      background: var(--surface-soft);
      border-radius: 6px;
      min-height: 14px;
    }
    .skeleton::after {
      content: "";
      position: absolute;
      inset: 0;
      transform: translateX(-100%);
      background: linear-gradient(90deg, transparent, rgba(255,255,255,.72), transparent);
      animation: shimmer 1.4s infinite;
    }
    @keyframes shimmer { 100% { transform: translateX(100%); } }

    @media (max-width: 900px) {
      .app { grid-template-columns: 1fr; }
      .rail { position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--border); }
      .nav { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .rail-foot { display: none; }
      .content { padding: 14px 12px 24px; }
      .status-strip { grid-template-columns: 1fr; }
      .hero, .control-panel, .chart, .side, .wide, .half { grid-column: span 12; }
      .hero-grid { grid-template-columns: 1fr; gap: 16px; }
      .hero-checks { grid-template-columns: 1fr; margin-top: 18px; }
      .divider { display: none; }
      .stat-grid { grid-template-columns: repeat(2, 1fr); }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation: none !important; transition: none !important; }
    }
  </style>
</head>
<body>
  <svg width=\"0\" height=\"0\" style=\"position:absolute\" aria-hidden=\"true\">
    <symbol id=\"i-battery\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-linecap=\"round\" stroke-linejoin=\"round\">
      <rect x=\"3\" y=\"7\" width=\"16\" height=\"10\" rx=\"2\"></rect><path d=\"M21 11v2\"></path><path d=\"M7 11h6\"></path>
    </symbol>
    <symbol id=\"i-gauge\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-linecap=\"round\" stroke-linejoin=\"round\">
      <path d=\"M4 13a8 8 0 0 1 16 0\"></path><path d=\"M12 13l4-4\"></path><path d=\"M7 18h10\"></path>
    </symbol>
    <symbol id=\"i-chart\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-linecap=\"round\" stroke-linejoin=\"round\">
      <path d=\"M4 19V5\"></path><path d=\"M4 19h16\"></path><path d=\"m7 15 3-4 4 2 4-7\"></path>
    </symbol>
    <symbol id=\"i-terminal\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-linecap=\"round\" stroke-linejoin=\"round\">
      <path d=\"M4 6h16v12H4z\"></path><path d=\"m7 10 2 2-2 2\"></path><path d=\"M12 15h4\"></path>
    </symbol>
    <symbol id=\"i-control\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-linecap=\"round\" stroke-linejoin=\"round\">
      <path d=\"M4 7h10\"></path><path d=\"M18 7h2\"></path><circle cx=\"16\" cy=\"7\" r=\"2\"></circle><path d=\"M4 17h2\"></path><path d=\"M10 17h10\"></path><circle cx=\"8\" cy=\"17\" r=\"2\"></circle>
    </symbol>
  </svg>
  <div class=\"app\">
    <aside class=\"rail\">
      <div class=\"brand\">
        <div class=\"brand-mark\"><svg><use href=\"#i-battery\"></use></svg></div>
        <div>
          <div class=\"brand-title\">电池接管</div>
          <div class=\"brand-version\">Local Control Console</div>
        </div>
      </div>
      <nav class=\"nav\" aria-label=\"面板导航\">
        <div class=\"nav-item active\"><svg><use href=\"#i-gauge\"></use></svg><span>实时状态</span></div>
        <div class=\"nav-item\"><svg><use href=\"#i-control\"></use></svg><span>策略控制</span></div>
        <div class=\"nav-item\"><svg><use href=\"#i-chart\"></use></svg><span>24h 趋势</span></div>
        <div class=\"nav-item\"><svg><use href=\"#i-terminal\"></use></svg><span>本机日志</span></div>
      </nav>
      <div class=\"rail-foot\">
        <div class=\"local-box\">
          <div class=\"local-title\"><span class=\"dot\" id=\"rail-dot\"></span><span id=\"rail-agent\">Agent 检查中</span></div>
          <div class=\"local-text\" id=\"rail-detail\">仅读取本机运行数据，不连接外部服务。</div>
        </div>
      </div>
    </aside>

    <main class=\"content\">
      <section class=\"status-strip\" aria-label=\"系统状态\">
        <div class=\"status-cell\"><span class=\"dot\" id=\"status-dot\"></span><span class=\"status-label\">State</span><span class=\"status-value\" id=\"top-status-text\">加载中</span></div>
        <div class=\"status-cell\"><span class=\"status-label\">Agent</span><span class=\"status-value\" id=\"top-agent\">-</span></div>
        <div class=\"status-cell\"><span class=\"status-label\">Started</span><span class=\"status-value\" id=\"top-runtime\">-</span></div>
        <div class=\"status-cell\"><span class=\"status-label\">Refresh</span><span class=\"status-value\" id=\"stamp\">加载中...</span></div>
      </section>

      <div class=\"grid\">
      <section class=\"panel hero\" aria-label=\"当前电池状态\">
        <div class=\"hero-grid\">
          <div class=\"battery-shell\" aria-hidden=\"true\">
            <div class=\"battery-fill\" id=\"battery-fill\"></div>
            <div class=\"battery-gauge-text\" id=\"battery-gauge-text\">-</div>
          </div>
          <div>
            <div class=\"k\">当前电量</div>
            <div class=\"hero-percent\" id=\"kpi-percent\">-</div>
            <div class=\"hero-state\" id=\"explain-title\">读取中</div>
            <p class=\"hero-copy\" id=\"explain-body\">正在读取本机电池和策略状态。</p>
          </div>
          <div class=\"divider\"></div>
          <div>
            <div class=\"policy-title\">项目接管区间</div>
            <div class=\"policy-band\" id=\"kpi-band\">-</div>
            <div class=\"policy-list\">
              <span>控制模式</span><span><span class=\"mode\" id=\"kpi-mode\">-</span></span>
              <span>当前动作</span><span id=\"st-action\">-</span>
              <span>执行后端</span><span id=\"st-backend\">-</span>
              <span>接管建议</span><span id=\"explain-next\">-</span>
            </div>
          </div>
        </div>
        <div class=\"hero-checks\">
          <div class=\"hero-check\"><span>电源连接</span><strong id=\"hero-ac\">-</strong></div>
          <div class=\"hero-check\"><span>电池健康</span><strong id=\"hero-health\">-</strong></div>
          <div class=\"hero-check\"><span>刷新节奏</span><strong>10 秒自动刷新</strong></div>
        </div>
      </section>

      <section class=\"panel control-panel\" aria-label=\"策略控制\">
        <div class=\"section-head\"><div class=\"k\">策略控制</div><span class=\"pill warn\">真实写入</span></div>
        <div class=\"ctrl\">
          <div class=\"toggle\">
            <div class=\"toggle-copy\">
              <strong id=\"mgmt-title\">项目电池管理</strong>
              <span id=\"mgmt-hint\">开启后按下方阈值控充；关闭后恢复系统默认充电规则。</span>
            </div>
            <input id=\"enabled-input\" class=\"switch\" type=\"checkbox\" aria-label=\"项目电池管理开关\" />
          </div>
          <div class=\"ctrl-row\">
            <label for=\"stop-input\">停充阈值</label>
            <div class=\"stepper\">
              <button type=\"button\" data-step-target=\"stop-input\" data-step=\"-1\" aria-label=\"降低停充阈值\">-</button>
              <input id=\"stop-input\" type=\"number\" min=\"50\" max=\"100\" />
              <button type=\"button\" data-step-target=\"stop-input\" data-step=\"1\" aria-label=\"提高停充阈值\">+</button>
            </div>
          </div>
          <div class=\"ctrl-row\">
            <label for=\"resume-input\">恢复阈值</label>
            <div class=\"stepper\">
              <button type=\"button\" data-step-target=\"resume-input\" data-step=\"-1\" aria-label=\"降低恢复阈值\">-</button>
              <input id=\"resume-input\" type=\"number\" min=\"40\" max=\"99\" />
              <button type=\"button\" data-step-target=\"resume-input\" data-step=\"1\" aria-label=\"提高恢复阈值\">+</button>
            </div>
          </div>
          <button class=\"btn\" id=\"save-btn\">保存并立即应用</button>
          <button class=\"btn secondary\" id=\"enforce-btn\">立即执行当前策略</button>
          <button class=\"btn ghost\" id=\"system-btn\">交还系统充电</button>
          <div class=\"notice\"><strong>注意</strong><span>这里会写入本机配置并调用 batt。出门前需要充满电时，可以先交还系统充电。</span></div>
          <div id=\"save-msg\" role=\"status\" aria-live=\"polite\"></div>
        </div>
      </section>

      <section class=\"panel chart\" aria-label=\"最近 24 小时电量曲线\">
        <div class=\"section-head\">
          <div class=\"k\">最近 24 小时电量曲线</div>
          <span class=\"pill ok\" id=\"band-chip\">目标区间 -</span>
        </div>
        <svg id=\"curve\" viewBox=\"0 0 1000 220\" preserveAspectRatio=\"none\"></svg>
        <div class=\"meta\" id=\"curve-meta\">-</div>
      </section>

      <section class=\"panel side\" aria-label=\"今日判断\">
        <div class=\"section-head\">
          <div class=\"k\">今日判断</div>
          <span class=\"pill\" id=\"explain-level\">-</span>
        </div>
        <div class=\"explain\">
          <div class=\"explain-title\" id=\"side-explain-title\">-</div>
          <div class=\"explain-body\" id=\"side-explain-body\">-</div>
          <div class=\"explain-next\" id=\"side-explain-next\">-</div>
        </div>
        <hr style=\"border:0;border-top:1px solid var(--border);margin:12px 0\">
        <div class=\"row\"><span>插电</span><span id=\"st-ac\">-</span></div>
        <div class=\"row\"><span>充电中</span><span id=\"st-charging\">-</span></div>
        <div class=\"row\"><span>循环次数</span><span id=\"st-cycle\">-</span></div>
        <div class=\"row\"><span>健康容量</span><span id=\"st-health\">-</span></div>
      </section>

      <section class=\"panel half\" aria-label=\"24 小时接管摘要\">
        <div class=\"section-head\"><div class=\"k\">24h 接管摘要</div><span class=\"pill ok\" id=\"summary-health\">-</span></div>
        <div class=\"stat-grid\">
          <div class=\"stat\"><div class=\"label\">样本</div><div class=\"num\" id=\"sum-samples\">-</div></div>
          <div class=\"stat\"><div class=\"label\">动作</div><div class=\"num\" id=\"sum-actions\">-</div></div>
          <div class=\"stat\"><div class=\"label\">失败</div><div class=\"num\" id=\"sum-failures\">-</div></div>
          <div class=\"stat\"><div class=\"label\">区间内</div><div class=\"num\" id=\"sum-band\">-</div></div>
          <div class=\"stat\"><div class=\"label\">成功率</div><div class=\"num\" id=\"sum-success\">-</div></div>
          <div class=\"stat\"><div class=\"label\">最低</div><div class=\"num\" id=\"sum-min\">-</div></div>
          <div class=\"stat\"><div class=\"label\">最高</div><div class=\"num\" id=\"sum-max\">-</div></div>
          <div class=\"stat\"><div class=\"label\">高于上限</div><div class=\"num\" id=\"sum-above\">-</div></div>
        </div>
      </section>

      <section class=\"panel half\" aria-label=\"运行路径\">
        <div class=\"section-head\"><div class=\"k\">运行路径</div><span class=\"pill warn\">本机</span></div>
        <div class=\"mono\" id=\"paths\">-</div>
      </section>

      <section class=\"panel wide\" aria-label=\"最近动作\">
        <div class=\"k\">最近动作（24h）</div>
        <div class=\"table-wrap\">
          <table>
            <thead>
              <tr><th>时间</th><th>动作</th><th>后端</th><th>目标</th><th>结果</th><th>错误</th></tr>
            </thead>
            <tbody id=\"action-body\"><tr><td class=\"empty-row\" colspan=\"6\">正在读取动作记录...</td></tr></tbody>
          </table>
        </div>
      </section>
      </div>
    </main>
    </div>

<script>
const $ = (id) => document.getElementById(id);

function fmtTs(ts) {
  if (!ts) return '-';
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString();
}

function fmtShortTs(ts) {
  if (!ts) return '-';
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function fmtStarted(ts) {
  if (!ts) return '-';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return '-';
  const hours = Math.max(0, Math.round((Date.now() - d.getTime()) / 36e5));
  if (hours < 1) return '刚启动';
  if (hours < 48) return `${hours} 小时`;
  return `${Math.round(hours / 24)} 天`;
}

function setStatusTone(level) {
  const dot = $('status-dot');
  const railDot = $('rail-dot');
  const cls = level === 'bad' ? 'dot bad' : (level === 'warn' ? 'dot warn' : 'dot');
  dot.className = cls;
  railDot.className = cls;
}

function drawCurve(samples, policy) {
  const svg = $('curve');
  svg.innerHTML = '';
  if (!samples.length) {
    $('curve-meta').textContent = '暂无样本。等待 Agent 写入第一条记录后会显示曲线。';
    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x', '500');
    text.setAttribute('y', '112');
    text.setAttribute('fill', '#62706a');
    text.setAttribute('font-size', '14');
    text.setAttribute('text-anchor', 'middle');
    text.textContent = '暂无 24h 样本';
    svg.appendChild(text);
    return;
  }

  const vals = samples.map(s => Number(s.percent));
  const minV = Math.min(...vals, 0);
  const maxV = Math.max(...vals, 100);
  const range = Math.max(maxV - minV, 1);

  const points = samples.map((s, i) => {
    const x = (i / Math.max(samples.length - 1, 1)) * 1000;
    const y = 210 - ((Number(s.percent) - minV) / range) * 180;
    return `${x},${y}`;
  });

  const areaPoints = [`0,220`, ...points, `1000,220`].join(' ');
  const area = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
  area.setAttribute('points', areaPoints);
  area.setAttribute('fill', 'rgba(15,98,64,0.12)');
  svg.appendChild(area);

  const addThreshold = (value, color, label) => {
    if (value == null) return;
    const y = 210 - ((Number(value) - minV) / range) * 180;
    const threshold = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    threshold.setAttribute('x1', '0');
    threshold.setAttribute('x2', '1000');
    threshold.setAttribute('y1', String(y));
    threshold.setAttribute('y2', String(y));
    threshold.setAttribute('stroke', color);
    threshold.setAttribute('stroke-width', '1.5');
    threshold.setAttribute('stroke-dasharray', '8 7');
    svg.appendChild(threshold);

    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x', '12');
    text.setAttribute('y', String(Math.max(14, y - 6)));
    text.setAttribute('fill', color);
    text.setAttribute('font-size', '13');
    text.setAttribute('font-family', 'IBM Plex Mono, Menlo, monospace');
    text.textContent = `${label} ${value}%`;
    svg.appendChild(text);
  };

  addThreshold(policy?.stop_percent, '#b07400', 'stop');
  addThreshold(policy?.resume_percent, '#237a52', 'resume');

  const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
  line.setAttribute('points', points.join(' '));
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', 'var(--line)');
  line.setAttribute('stroke-width', '3');
  svg.appendChild(line);

  const last = samples[samples.length - 1];
  $('curve-meta').textContent = `样本 ${samples.length} 条，当前 ${last.percent}%（${fmtTs(last.ts)}）`;
}

function applyExplanation(explanation) {
  const level = explanation?.level || 'warn';
  $('explain-title').textContent = explanation?.title || '-';
  $('explain-body').textContent = explanation?.body || '-';
  $('explain-next').textContent = explanation?.next || '-';
  $('side-explain-title').textContent = explanation?.title || '-';
  $('side-explain-body').textContent = explanation?.body || '-';
  $('side-explain-next').textContent = explanation?.next || '-';
  const el = $('explain-level');
  const text = level === 'ok' ? '正常' : (level === 'bad' ? '异常' : '注意');
  el.textContent = text;
  el.className = `pill ${level}`;
  setStatusTone(level);
}

function renderActions(actions) {
  const body = $('action-body');
  body.innerHTML = '';
  const rows = actions.slice(-20).reverse();
  if (!rows.length) {
    body.innerHTML = '<tr><td class="empty-row" colspan="6">24 小时内暂无动作记录</td></tr>';
    return;
  }
  for (const a of rows) {
    const tr = document.createElement('tr');
    const ok = Number(a.success) === 1;
    tr.innerHTML = `
      <td>${fmtTs(a.ts)}</td>
      <td>${a.action_type || '-'}</td>
      <td>${a.backend || '-'}</td>
      <td>${a.target_percent ?? '-'}</td>
      <td class="${ok ? 'ok' : 'bad'}">${ok ? '成功' : '失败'}</td>
      <td>${a.error_msg || ''}</td>
    `;
    body.appendChild(tr);
  }
}

async function loadPolicy() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) {
      throw new Error(`/api/config returned ${res.status}`);
    }
    const cfg = await res.json();
    $('stop-input').value = cfg.stop_percent;
    $('resume-input').value = cfg.resume_percent;
    $('enabled-input').checked = Boolean(cfg.enabled);
    syncControlForm(Boolean(cfg.enabled));
  } catch (err) {
    $('save-msg').textContent = `配置加载失败: ${err}`;
    $('save-msg').className = 'bad';
  }
}

function syncControlForm(enabled) {
  $('mgmt-title').textContent = enabled ? '项目电池管理：开启' : '项目电池管理：关闭';
  $('mgmt-hint').textContent = enabled
    ? '开启后按下方阈值控充。当前更适合长期插电。'
    : '关闭后会清除项目限充，让系统按默认规则继续充电。';
  $('stop-input').disabled = !enabled;
  $('resume-input').disabled = !enabled;
  document.querySelectorAll('.stepper button').forEach((button) => {
    button.disabled = !enabled;
  });
}

async function savePolicy() {
  const stop = Number($('stop-input').value);
  const resume = Number($('resume-input').value);
  const enabled = $('enabled-input').checked;
  const msg = $('save-msg');
  msg.textContent = '保存中...';
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stop_percent: stop, resume_percent: resume, enabled }),
    });
    const payload = await res.json();
    if (!res.ok) {
      msg.textContent = `保存失败: ${payload.error || 'unknown error'}`;
      msg.className = 'bad';
      return;
    }
    syncControlForm(Boolean(payload.policy.enabled));
    const apply = payload.apply || {};
    const appliedText = apply.action ? `；已执行 ${apply.action}` : '';
    msg.textContent = `已保存: ${payload.policy.enabled ? '启用项目管理' : '恢复系统管理'}，stop=${payload.policy.stop_percent}，resume=${payload.policy.resume_percent}${appliedText}`;
    msg.className = payload.apply && payload.apply.success === false ? 'bad' : 'ok';
    await refresh();
  } catch (err) {
    msg.textContent = `保存失败: ${err}`;
    msg.className = 'bad';
  }
}

async function enforceNow() {
  const msg = $('save-msg');
  msg.textContent = '执行中...';
  try {
    const res = await fetch('/api/enforce-now', { method: 'POST' });
    const payload = await res.json();
    if (!res.ok) {
      msg.textContent = `执行失败: ${payload.error || 'unknown error'}`;
      msg.className = 'bad';
      return;
    }
    msg.textContent = `执行结果: ${payload.action} (${payload.message})`;
    msg.className = payload.success ? 'ok' : 'bad';
    await refresh();
  } catch (err) {
    msg.textContent = `执行失败: ${err}`;
    msg.className = 'bad';
  }
}

async function handBackToSystem() {
  $('enabled-input').checked = false;
  syncControlForm(false);
  await savePolicy();
}

function updateBatteryGauge(percent) {
  const value = Number(percent);
  const valid = Number.isFinite(value);
  const pct = valid ? Math.max(0, Math.min(100, value)) : 0;
  $('battery-fill').style.height = `${pct}%`;
  $('battery-gauge-text').textContent = valid ? `${pct}%` : '-';
  $('battery-fill').style.background = pct >= 90 ? 'var(--warn)' : 'var(--accent)';
}

async function refresh() {
  try {
    const [snapRes, histRes] = await Promise.all([
      fetch('/api/snapshot'),
      fetch('/api/history?hours=24'),
    ]);
    if (!snapRes.ok) {
      throw new Error(`/api/snapshot returned ${snapRes.status}`);
    }
    if (!histRes.ok) {
      throw new Error(`/api/history returned ${histRes.status}`);
    }
    const snap = await snapRes.json();
    const hist = await histRes.json();
    const ov = snap.overview || {};
    const policy = snap.policy || {};
    const windowSummary = snap.window || {};

    $('stamp').textContent = fmtShortTs(ov.generated_at);

    const s = ov.latest_sample || {};
    const a = ov.latest_action || {};
    const runtime = ov.runtime_state || {};
    const mode = runtime.mode || '-';

    $('kpi-percent').textContent = s.percent != null ? `${s.percent}%` : '-';
    updateBatteryGauge(s.percent);
    $('kpi-band').textContent = policy.stop_percent != null ? `${policy.resume_percent}-${policy.stop_percent}%` : '-';
    $('band-chip').textContent = policy.stop_percent != null ? `目标区间 ${policy.resume_percent}-${policy.stop_percent}%` : '目标区间 -';
    if (document.activeElement !== $('stop-input')) $('stop-input').value = policy.stop_percent ?? '';
    if (document.activeElement !== $('resume-input')) $('resume-input').value = policy.resume_percent ?? '';
    $('enabled-input').checked = Boolean(policy.enabled);

    const modeEl = $('kpi-mode');
    modeEl.textContent = mode;
    modeEl.className = `mode ${mode}`;

    const agent = ov.agent || {};
    const agentText = agent.running ? `运行中 #${agent.pid}` : '未运行';
    $('top-agent').textContent = agentText;
    $('rail-agent').textContent = agentText;
    $('rail-detail').textContent = agent.running ? 'LaunchAgent 正在后台巡检本机电池。' : 'Agent 未运行，只能查看历史数据。';
    $('top-runtime').textContent = fmtStarted(runtime.observe_started_at);
    $('top-status-text').textContent = mode === 'ACTIVE_CONTROL' ? '接管中' : (mode === 'DEGRADED_READONLY' ? '只读降级' : '观察模式');

    $('st-ac').textContent = Number(s.on_ac) === 1 ? '是' : (s.on_ac == null ? '-' : '否');
    $('st-charging').textContent = Number(s.charging) === 1 ? '是' : (s.charging == null ? '-' : '否');
    $('st-cycle').textContent = s.cycle_count ?? '-';
    $('st-health').textContent = s.max_capacity_pct != null ? `${s.max_capacity_pct}%` : '-';
    $('hero-ac').textContent = Number(s.on_ac) === 1 ? '已接入电源' : (s.on_ac == null ? '-' : '未接入电源');
    $('hero-health').textContent = s.max_capacity_pct != null ? `最大容量 ${s.max_capacity_pct}%` : '-';
    $('st-action').textContent = a.action_type || '-';
    $('st-backend').textContent = a.backend || '-';
    syncControlForm(Boolean(policy.enabled));

    applyExplanation(snap.explanation || {});

    $('sum-samples').textContent = windowSummary.sample_count ?? '-';
    $('sum-actions').textContent = windowSummary.action_count ?? '-';
    $('sum-failures').textContent = windowSummary.failure_count ?? '-';
    $('sum-band').textContent = windowSummary.target_band_percent != null ? `${windowSummary.target_band_percent}%` : '-';
    $('sum-success').textContent = windowSummary.success_rate != null ? `${windowSummary.success_rate}%` : '-';
    $('sum-min').textContent = windowSummary.min_percent != null ? `${windowSummary.min_percent}%` : '-';
    $('sum-max').textContent = windowSummary.max_percent != null ? `${windowSummary.max_percent}%` : '-';
    $('sum-above').textContent = windowSummary.above_stop_count ?? '-';
    const health = Number(windowSummary.failure_count || 0) === 0 ? '无失败' : `${windowSummary.failure_count} 次失败`;
    $('summary-health').textContent = health;
    $('summary-health').className = Number(windowSummary.failure_count || 0) === 0 ? 'pill ok' : 'pill bad';

    $('paths').textContent = `DB: ${ov.paths.db} | LOG: ${ov.paths.log} | REPORTS: ${ov.paths.reports_dir}`;

    drawCurve(hist.samples || [], policy);
    renderActions(hist.actions || []);
  } catch (err) {
    $('stamp').textContent = '失败';
    $('top-status-text').textContent = '数据刷新失败';
    $('curve-meta').textContent = `数据刷新失败：${err}`;
    setStatusTone('bad');
  }
}

$('save-btn').addEventListener('click', savePolicy);
$('enforce-btn').addEventListener('click', enforceNow);
$('system-btn').addEventListener('click', handBackToSystem);
$('enabled-input').addEventListener('change', (event) => {
  syncControlForm(Boolean(event.target.checked));
});
document.querySelectorAll('.stepper button').forEach((button) => {
  button.addEventListener('click', () => {
    const input = $(button.dataset.stepTarget);
    const step = Number(button.dataset.step || 0);
    const min = Number(input.min || 0);
    const max = Number(input.max || 100);
    const next = Math.max(min, Math.min(max, Number(input.value || 0) + step));
    input.value = String(next);
  });
});

loadPolicy();
refresh();
setInterval(refresh, 10000);
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

            if path == "/api/snapshot":
                payload = _build_product_snapshot(self.server.cfg)
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
            f"[dashboard] ERROR: refusing to bind non-loopback host {host!r}; "
            "use 127.0.0.1, localhost, or ::1",
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
