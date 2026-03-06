#!/usr/bin/env bash
set -euo pipefail

APP_BUNDLE="$HOME/Applications/电池接管.app"
DESKTOP_LINK="$HOME/Desktop/电池接管.app"

rm -rf "$APP_BUNDLE"
rm -f "$DESKTOP_LINK"

echo "[desktop-app] removed"
