#!/usr/bin/env bash
set -euo pipefail

TARGET="$HOME/Library/LaunchAgents/com.battery.takeover.agent.plist"
RUNTIME_DIR="$HOME/Library/Application Support/BatteryTakeover"

if [[ -f "$TARGET" ]]; then
  launchctl unload "$TARGET" >/dev/null 2>&1 || true
  rm -f "$TARGET"
  echo "[launchd] removed: $TARGET"
else
  echo "[launchd] not installed"
fi

echo "[runtime] remains at: $RUNTIME_DIR"
echo "[runtime] remove manually if needed: rm -rf '$RUNTIME_DIR'"
