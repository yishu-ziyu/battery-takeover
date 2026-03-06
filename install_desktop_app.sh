#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME_APP_DIR="$HOME/Library/Application Support/BatteryTakeover/app"
APP_BUNDLE="$HOME/Applications/电池接管.app"
DESKTOP_LINK="$HOME/Desktop/电池接管.app"
TMP_SCPT="$(mktemp /tmp/battery_takeover_launcher.XXXXXX.scpt)"

"$ROOT_DIR/install_agent_launchd.sh"
mkdir -p "$HOME/Applications"

cat > "$TMP_SCPT" <<SCPT
on run
  set appDir to "$RUNTIME_APP_DIR"
  set cmd to "\"" & appDir & "/control.sh\" start >/tmp/battery_takeover_click.log 2>&1; " & ¬
    "for i in {1..20}; do /usr/bin/curl -fsS http://127.0.0.1:8765/api/overview >/dev/null 2>&1 && break; /bin/sleep 0.4; done; " & ¬
    "/usr/bin/open http://127.0.0.1:8765"
  do shell script "/bin/zsh -lc " & quoted form of cmd
  activate
end run
SCPT

rm -rf "$APP_BUNDLE"
osacompile -o "$APP_BUNDLE" "$TMP_SCPT"
rm -f "$TMP_SCPT"

ln -sfn "$APP_BUNDLE" "$DESKTOP_LINK" || true

cat <<MSG
[desktop-app] installed
- app bundle: $APP_BUNDLE
- desktop link: $DESKTOP_LINK

Click "电池接管.app" to open dashboard and ensure services are running.
MSG
