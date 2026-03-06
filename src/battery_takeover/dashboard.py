from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import os
import subprocess

from .agent import run_cycle, setup_logging
from .config import AppConfig, ensure_runtime_dirs, load_config, update_policy_thresholds
from .notifier import Notifier
from .storage import Storage


def _row_to_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


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
        return {"running": True, "pid": pid, "source": "pid_permission_denied"}


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
    latest_sample = _row_to_dict(st.latest_sample())
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


def _html() -> str:
    return """<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>电池接管 Dashboard</title>
  <style>
    :root {
      --bg-0: #f4f7f2;
      --bg-1: #dce9d7;
      --card: rgba(255,255,255,0.86);
      --ink: #13251a;
      --muted: #4f6657;
      --ok: #2f8f5b;
      --warn: #b07400;
      --bad: #b03636;
      --line: #0f6240;
      --line-soft: #9bc4ad;
      --radius: 14px;
      --shadow: 0 18px 38px rgba(19, 37, 26, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: \"IBM Plex Sans\", \"Source Han Sans SC\", \"Noto Sans CJK SC\", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(1200px 400px at 90% -10%, #b9d6bf 0%, transparent 70%),
        radial-gradient(800px 300px at -5% -10%, #e5f0dd 0%, transparent 60%),
        linear-gradient(145deg, var(--bg-0), var(--bg-1));
      min-height: 100vh;
    }
    .wrap { max-width: 1120px; margin: 0 auto; padding: 20px 16px 36px; }
    .head { display: flex; justify-content: space-between; align-items: end; gap: 12px; margin-bottom: 14px; }
    .title { margin: 0; font-size: clamp(22px, 3vw, 34px); font-weight: 700; letter-spacing: .2px; }
    .sub { margin: 0; color: var(--muted); font-size: 13px; }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; }
    .card {
      background: var(--card);
      backdrop-filter: blur(6px);
      border-radius: var(--radius);
      padding: 14px;
      box-shadow: var(--shadow);
      border: 1px solid rgba(19, 37, 26, 0.08);
    }
    .kpi { grid-column: span 3; }
    .chart { grid-column: span 8; }
    .side { grid-column: span 4; }
    .wide { grid-column: span 12; }
    .k { color: var(--muted); font-size: 12px; margin-bottom: 4px; }
    .v { font-size: 27px; font-weight: 700; line-height: 1.05; }
    .mode { display: inline-block; padding: 4px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
    .mode.ACTIVE_CONTROL { background: rgba(47,143,91,.14); color: var(--ok); }
    .mode.OBSERVE_ONLY { background: rgba(176,116,0,.14); color: var(--warn); }
    .mode.DEGRADED_READONLY { background: rgba(176,54,54,.14); color: var(--bad); }
    #curve {
      width: 100%; height: 220px; display: block;
      background: linear-gradient(180deg, rgba(15,98,64,0.09), rgba(15,98,64,0.01));
      border-radius: 10px; border: 1px solid rgba(15,98,64,0.12);
    }
    .meta { margin-top: 8px; font-size: 12px; color: var(--muted); }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { padding: 8px 6px; text-align: left; border-bottom: 1px dashed rgba(19,37,26,0.18); }
    th { color: var(--muted); font-weight: 600; }
    .ok { color: var(--ok); font-weight: 600; }
    .bad { color: var(--bad); font-weight: 600; }
    .row { display: flex; justify-content: space-between; gap: 10px; padding: 4px 0; font-size: 13px; }
    .mono { font-family: \"IBM Plex Mono\", \"Menlo\", monospace; font-size: 12px; }
    .ctrl { display: flex; flex-direction: column; gap: 10px; }
    .ctrl-row { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center; }
    .ctrl input {
      width: 92px;
      padding: 6px 8px;
      border-radius: 10px;
      border: 1px solid rgba(19, 37, 26, .25);
      font-size: 14px;
      background: rgba(255,255,255,0.95);
      color: var(--ink);
    }
    .btn {
      border: none;
      border-radius: 10px;
      padding: 8px 12px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      background: #1f7049;
      color: #fff;
    }
    .btn.secondary {
      background: #28483a;
    }
    .hint { font-size: 12px; color: var(--muted); }
    #save-msg { font-size: 12px; min-height: 16px; }

    @media (max-width: 900px) {
      .kpi { grid-column: span 6; }
      .chart, .side, .wide { grid-column: span 12; }
    }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"head\">
      <div>
        <h1 class=\"title\">电池接管 · 实时面板</h1>
        <p class=\"sub\" id=\"stamp\">加载中...</p>
      </div>
      <div class=\"sub\">自动刷新: 10s</div>
    </div>

    <div class=\"grid\">
      <div class=\"card kpi\"><div class=\"k\">当前电量</div><div class=\"v\" id=\"kpi-percent\">-</div></div>
      <div class=\"card kpi\"><div class=\"k\">运行模式</div><div class=\"v\"><span class=\"mode\" id=\"kpi-mode\">-</span></div></div>
      <div class=\"card kpi\"><div class=\"k\">Agent 状态</div><div class=\"v\" id=\"kpi-agent\">-</div></div>
      <div class=\"card kpi\"><div class=\"k\">24h 样本 / 动作</div><div class=\"v\" id=\"kpi-24h\">-</div></div>

      <div class=\"card chart\">
        <div class=\"k\">最近 24 小时电量曲线</div>
        <svg id=\"curve\" viewBox=\"0 0 1000 220\" preserveAspectRatio=\"none\"></svg>
        <div class=\"meta\" id=\"curve-meta\">-</div>
      </div>

      <div class=\"card side\">
        <div class=\"k\">当前状态</div>
        <div class=\"row\"><span>插电</span><span id=\"st-ac\">-</span></div>
        <div class=\"row\"><span>充电中</span><span id=\"st-charging\">-</span></div>
        <div class=\"row\"><span>循环次数</span><span id=\"st-cycle\">-</span></div>
        <div class=\"row\"><span>健康容量</span><span id=\"st-health\">-</span></div>
        <div class=\"row\"><span>最近动作</span><span id=\"st-action\">-</span></div>
        <div class=\"row\"><span>执行后端</span><span id=\"st-backend\">-</span></div>
      </div>

      <div class=\"card side\">
        <div class=\"k\">阈值控制</div>
        <div class=\"ctrl\">
          <div class=\"ctrl-row\">
            <label for=\"stop-input\">停充阈值(%)</label>
            <input id=\"stop-input\" type=\"number\" min=\"50\" max=\"100\" />
          </div>
          <div class=\"ctrl-row\">
            <label for=\"resume-input\">恢复阈值(%)</label>
            <input id=\"resume-input\" type=\"number\" min=\"40\" max=\"99\" />
          </div>
          <button class=\"btn\" id=\"save-btn\">保存阈值</button>
          <button class=\"btn secondary\" id=\"enforce-btn\">立即执行策略</button>
          <div class=\"hint\">保存后 agent 下一周期生效；点击“立即执行策略”可马上下发。</div>
          <div id=\"save-msg\"></div>
        </div>
      </div>

      <div class=\"card wide\">
        <div class=\"k\">最近动作（24h）</div>
        <table>
          <thead>
            <tr><th>时间</th><th>动作</th><th>后端</th><th>目标</th><th>结果</th><th>错误</th></tr>
          </thead>
          <tbody id=\"action-body\"></tbody>
        </table>
      </div>

      <div class=\"card wide mono\" id=\"paths\">-</div>
    </div>
  </div>

<script>
const $ = (id) => document.getElementById(id);

function fmtTs(ts) {
  if (!ts) return '-';
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString();
}

function drawCurve(samples) {
  const svg = $('curve');
  svg.innerHTML = '';
  if (!samples.length) {
    $('curve-meta').textContent = '暂无样本';
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

  const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
  line.setAttribute('points', points.join(' '));
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', 'var(--line)');
  line.setAttribute('stroke-width', '3');
  svg.appendChild(line);

  const last = samples[samples.length - 1];
  $('curve-meta').textContent = `样本 ${samples.length} 条，当前 ${last.percent}%（${fmtTs(last.ts)}）`;
}

function renderActions(actions) {
  const body = $('action-body');
  body.innerHTML = '';
  const rows = actions.slice(-20).reverse();
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="6">无动作</td></tr>';
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
  } catch (err) {
    $('save-msg').textContent = `配置加载失败: ${err}`;
    $('save-msg').className = 'bad';
  }
}

async function savePolicy() {
  const stop = Number($('stop-input').value);
  const resume = Number($('resume-input').value);
  const msg = $('save-msg');
  msg.textContent = '保存中...';
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stop_percent: stop, resume_percent: resume }),
    });
    const payload = await res.json();
    if (!res.ok) {
      msg.textContent = `保存失败: ${payload.error || 'unknown error'}`;
      msg.className = 'bad';
      return;
    }
    msg.textContent = `已保存: stop=${payload.policy.stop_percent}, resume=${payload.policy.resume_percent}`;
    msg.className = 'ok';
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

async function refresh() {
  try {
    const [ovRes, histRes] = await Promise.all([
      fetch('/api/overview'),
      fetch('/api/history?hours=24'),
    ]);
    if (!ovRes.ok) {
      throw new Error(`/api/overview returned ${ovRes.status}`);
    }
    if (!histRes.ok) {
      throw new Error(`/api/history returned ${histRes.status}`);
    }
    const ov = await ovRes.json();
    const hist = await histRes.json();

    $('stamp').textContent = `最后刷新：${fmtTs(ov.generated_at)}`;

    const s = ov.latest_sample || {};
    const a = ov.latest_action || {};
    const mode = (ov.runtime_state || {}).mode || '-';

    $('kpi-percent').textContent = s.percent != null ? `${s.percent}%` : '-';
    $('kpi-24h').textContent = `${ov.sample_count_24h} / ${ov.action_count_24h}`;

    const modeEl = $('kpi-mode');
    modeEl.textContent = mode;
    modeEl.className = `mode ${mode}`;

    const agent = ov.agent || {};
    $('kpi-agent').textContent = agent.running ? `运行中 (#${agent.pid})` : '未运行';

    $('st-ac').textContent = Number(s.on_ac) === 1 ? '是' : (s.on_ac == null ? '-' : '否');
    $('st-charging').textContent = Number(s.charging) === 1 ? '是' : (s.charging == null ? '-' : '否');
    $('st-cycle').textContent = s.cycle_count ?? '-';
    $('st-health').textContent = s.max_capacity_pct != null ? `${s.max_capacity_pct}%` : '-';
    $('st-action').textContent = a.action_type || '-';
    $('st-backend').textContent = a.backend || '-';

    $('paths').textContent = `DB: ${ov.paths.db} | LOG: ${ov.paths.log} | REPORTS: ${ov.paths.reports_dir}`;

    drawCurve(hist.samples || []);
    renderActions(hist.actions || []);
  } catch (err) {
    $('stamp').textContent = `数据刷新失败：${err}`;
  }
}

$('save-btn').addEventListener('click', savePolicy);
$('enforce-btn').addEventListener('click', enforceNow);

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
                cfg = update_policy_thresholds(
                    self.server.cfg.config_path,
                    stop_percent=stop,
                    resume_percent=resume,
                )
                self.server.cfg = cfg
                self._send_json(
                    {
                        "ok": True,
                        "policy": {
                            "stop_percent": cfg.policy.stop_percent,
                            "resume_percent": cfg.policy.resume_percent,
                        },
                    }
                )
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


def run_dashboard(cfg: AppConfig, host: str, port: int, open_browser: bool) -> int:
    server = DashboardServer(host=host, port=port, cfg=cfg)
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
