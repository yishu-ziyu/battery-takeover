#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME_DIR="$HOME/Library/Application Support/BatteryTakeover"
APP_DIR="$RUNTIME_DIR/app"
TARGET_PLIST="$HOME/Library/LaunchAgents/com.battery.takeover.agent.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$RUNTIME_DIR" "$APP_DIR"

rsync -a --delete \
  --exclude '.DS_Store' \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude 'logs/' \
  --exclude 'state/' \
  --exclude 'reports/' \
  --exclude '*.pyc' \
  "$SOURCE_DIR/" "$APP_DIR/"

mkdir -p "$APP_DIR/logs" "$APP_DIR/state" "$APP_DIR/reports"

cat > "$TARGET_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.battery.takeover.agent</string>

  <key>ProgramArguments</key>
  <array>
    <string>$APP_DIR/btake</string>
    <string>--config</string>
    <string>$APP_DIR/config/default.toml</string>
    <string>agent</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$APP_DIR</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>$APP_DIR/logs/launchd.out.log</string>

  <key>StandardErrorPath</key>
  <string>$APP_DIR/logs/launchd.err.log</string>
</dict>
</plist>
PLIST

launchctl unload "$TARGET_PLIST" >/dev/null 2>&1 || true
launchctl load "$TARGET_PLIST"

cat <<MSG
[launchd] installed and loaded
- source: $SOURCE_DIR
- runtime: $APP_DIR
- plist: $TARGET_PLIST

Use these checks:
  launchctl list | rg com.battery.takeover.agent
  tail -n 60 '$APP_DIR/logs/launchd.err.log'
MSG
