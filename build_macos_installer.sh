#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$ROOT_DIR/dist"
BUILD_DIR="$ROOT_DIR/.build/macos-installer"
PKG_ROOT="$BUILD_DIR/root"
SCRIPTS_DIR="$BUILD_DIR/scripts"
PAYLOAD_DIR="$PKG_ROOT/Library/Application Support/BatteryTakeoverInstaller/payload"
COMPONENT_PKG="$BUILD_DIR/battery-takeover-component.pkg"

VERSION="$(python3 - <<'PY'
from pathlib import Path
import tomllib

data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
print(data["project"]["version"])
PY
)"

FINAL_PKG="$DIST_DIR/battery-takeover-${VERSION}-installer.pkg"

mkdir -p "$DIST_DIR" "$PAYLOAD_DIR" "$SCRIPTS_DIR"
rm -rf "$PKG_ROOT" "$SCRIPTS_DIR" "$COMPONENT_PKG" "$FINAL_PKG"
mkdir -p "$PAYLOAD_DIR" "$SCRIPTS_DIR"

rsync -a --delete \
  --exclude '.DS_Store' \
  --exclude '.git/' \
  --exclude '.github/' \
  --exclude '.venv/' \
  --exclude '.build/' \
  --exclude 'dist/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'logs/' \
  --exclude 'state/' \
  --exclude 'reports/' \
  --exclude 'docs/assets/png/' \
  --exclude 'docs/assets/screens/dashboard-window.png' \
  --exclude 'docs/assets/screens/dashboard-headless.png' \
  "$ROOT_DIR/" "$PAYLOAD_DIR/"

cp "$ROOT_DIR/packaging/macos/postinstall" "$SCRIPTS_DIR/postinstall"
chmod +x "$SCRIPTS_DIR/postinstall"
chmod +x "$PAYLOAD_DIR/"*.sh "$PAYLOAD_DIR/btake"

pkgbuild \
  --root "$PKG_ROOT" \
  --identifier "com.battery.takeover.installer" \
  --version "$VERSION" \
  --install-location "/" \
  --scripts "$SCRIPTS_DIR" \
  "$COMPONENT_PKG"

productbuild \
  --package "$COMPONENT_PKG" \
  "$FINAL_PKG"

pkgutil --check-signature "$FINAL_PKG" || true

cat <<MSG
[installer] built
- version: $VERSION
- pkg: $FINAL_PKG

Suggested distribution path:
  GitHub Releases -> upload $(basename "$FINAL_PKG")
MSG
