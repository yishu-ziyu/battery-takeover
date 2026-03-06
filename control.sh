#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$ROOT_DIR/config/default.toml"
AGENT_PID_FILE="$ROOT_DIR/state/agent.pid"
DASH_PID_FILE="$ROOT_DIR/state/dashboard.pid"
DASH_PORT="${DASH_PORT:-8765}"
AGENT_LABEL="com.battery.takeover.agent"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"

is_running() {
  local pid="$1"
  if [[ -z "$pid" ]]; then
    return 1
  fi
  kill -0 "$pid" >/dev/null 2>&1
}

read_pid() {
  local file="$1"
  if [[ -f "$file" ]]; then
    tr -d '[:space:]' < "$file"
  fi
}

find_agent_pid() {
  pgrep -f "battery_takeover\\.cli.* agent" | head -n 1 || true
}

find_dashboard_pid() {
  local pid
  pid="$(pgrep -f "battery_takeover\\.cli.* dashboard" | head -n 1 || true)"
  if is_running "$pid"; then
    echo "$pid"
    return
  fi
  lsof -nP -iTCP:"$DASH_PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true
}

is_agent_launchd_loaded() {
  launchctl list | awk -v label="$AGENT_LABEL" '$3==label {found=1} END {exit(found?0:1)}'
}

launchd_agent_pid() {
  launchctl list | awk -v label="$AGENT_LABEL" '$3==label && $1 ~ /^[0-9]+$/ {print $1; exit}'
}

start_agent() {
  local pid
  pid="$(read_pid "$AGENT_PID_FILE")"
  if is_running "$pid"; then
    echo "[agent] already running pid=$pid"
    return
  fi

  pid="$(find_agent_pid)"
  if is_running "$pid"; then
    echo "$pid" > "$AGENT_PID_FILE"
    echo "[agent] already running pid=$pid (process scan)"
    return
  fi

  cd "$ROOT_DIR"
  nohup ./btake --config "$CONFIG" agent > ./logs/agent.nohup.log 2>&1 &
  echo $! > "$AGENT_PID_FILE"
  sleep 1
  pid="$(read_pid "$AGENT_PID_FILE")"
  if is_running "$pid"; then
    echo "[agent] started pid=$pid"
  else
    echo "[agent] failed to start"
    exit 1
  fi
}

start_agent_managed() {
  if [[ ! -f "$LAUNCHD_PLIST" ]]; then
    return 1
  fi
  if ! is_agent_launchd_loaded; then
    launchctl load "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
  fi
  launchctl kickstart -k "gui/$(id -u)/$AGENT_LABEL" >/dev/null 2>&1 || true
  echo "[agent] managed by launchd: $AGENT_LABEL"
  return 0
}

start_dashboard() {
  local pid
  pid="$(read_pid "$DASH_PID_FILE")"
  if is_running "$pid"; then
    echo "[dashboard] already running pid=$pid"
    return
  fi

  pid="$(find_dashboard_pid)"
  if is_running "$pid"; then
    echo "$pid" > "$DASH_PID_FILE"
    echo "[dashboard] already running pid=$pid (process scan) url=http://127.0.0.1:$DASH_PORT"
    return
  fi

  cd "$ROOT_DIR"
  nohup ./btake --config "$CONFIG" dashboard --host 127.0.0.1 --port "$DASH_PORT" > ./logs/dashboard.log 2>&1 &
  echo $! > "$DASH_PID_FILE"
  sleep 1
  pid="$(read_pid "$DASH_PID_FILE")"
  if is_running "$pid"; then
    echo "[dashboard] started pid=$pid url=http://127.0.0.1:$DASH_PORT"
  else
    echo "[dashboard] failed to start"
    exit 1
  fi
}

stop_one() {
  local name="$1"
  local file="$2"
  local finder="$3"
  local pid
  pid="$(read_pid "$file")"
  if is_running "$pid"; then
    kill "$pid" >/dev/null 2>&1 || true
    sleep 1
    if is_running "$pid"; then
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
    echo "[$name] stopped pid=$pid"
  else
    pid="$($finder)"
    if is_running "$pid"; then
      kill "$pid" >/dev/null 2>&1 || true
      sleep 1
      if is_running "$pid"; then
        kill -9 "$pid" >/dev/null 2>&1 || true
      fi
      echo "[$name] stopped pid=$pid (process scan)"
    else
      echo "[$name] not running"
    fi
  fi
  rm -f "$file"
}

status() {
  echo "[doctor]"
  "$ROOT_DIR/btake" --config "$CONFIG" doctor || true
  echo

  echo "[agent]"
  local apid
  apid="$(read_pid "$AGENT_PID_FILE")"
  if ! is_running "$apid"; then
    apid="$(find_agent_pid)"
  fi
  if ! is_running "$apid"; then
    apid="$(launchd_agent_pid)"
  fi
  if is_running "$apid"; then
    echo "running pid=$apid"
  else
    echo "not running"
  fi

  echo "[dashboard]"
  local dpid
  dpid="$(read_pid "$DASH_PID_FILE")"
  if ! is_running "$dpid"; then
    dpid="$(find_dashboard_pid)"
  fi
  if is_running "$dpid"; then
    echo "running pid=$dpid url=http://127.0.0.1:$DASH_PORT"
  else
    echo "not running"
  fi

  echo "[batt]"
  batt status | sed -n '1,24p' || true
}

cmd="${1:-status}"
case "$cmd" in
  start)
    mkdir -p "$ROOT_DIR/state" "$ROOT_DIR/logs" "$ROOT_DIR/reports"
    start_agent_managed || start_agent
    start_dashboard
    ;;
  stop)
    stop_one "dashboard" "$DASH_PID_FILE" find_dashboard_pid
    if is_agent_launchd_loaded; then
      launchctl bootout "gui/$(id -u)/$AGENT_LABEL" >/dev/null 2>&1 || true
      echo "[agent] stopped via launchd bootout: $AGENT_LABEL"
      rm -f "$AGENT_PID_FILE"
    else
      stop_one "agent" "$AGENT_PID_FILE" find_agent_pid
    fi
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    status
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 2
    ;;
esac
