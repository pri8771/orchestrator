#!/usr/bin/env bash
# Bundle the GUI into a double-clickable Orchestrator.app.
#
#   bash .orchestrator/gui/build_app.sh
#
# Produces .orchestrator/gui/dist/Orchestrator.app. Because it runs from your
# own machine (unsigned, no sandbox), it can read the workspace and launch
# python3 just like the from-source build.
set -euo pipefail

GUI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$GUI_DIR/dist/Orchestrator.app"

cd "$GUI_DIR"
echo "[build_app] building (release)…"
swift build -c release
BIN="$(swift build -c release --show-bin-path)/OrchestratorGUI"

echo "[build_app] assembling bundle…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Orchestrator"

# Bundle the Python engine INSIDE the app so it's self-contained (double-click /
# DMG). The app copies this to Application Support on first launch (it needs to be
# writable for logs/config), keyed by the VERSION written here.
ENGINE_SRC="$(cd "$GUI_DIR/.." && pwd)"      # orchestrator-v2-source
ENGINE_DEST="$APP/Contents/Resources/engine"
echo "[build_app] bundling engine from ${ENGINE_SRC}..."
mkdir -p "$ENGINE_DEST"
# Copy the engine, excluding the GUI sources, build output, and local runtime.
# CRITICAL: exclude secret-shaped files. This bundle is redistributed by
# make_dmg.sh, so a stray API key / credential file in the engine dir must never
# ship inside the .app. Mirrors run.sh's commit block-list plus config.json
# (per-machine settings, not for distribution).
( cd "$ENGINE_SRC" && \
  find . -type f \
    -not -path './gui/*' -not -path './.git/*' \
    -not -path './logs/*' -not -path './locks/*' -not -path '*/__pycache__/*' \
    -not -path './tests/*' -not -path './sample-run/*' \
    -not -path './.orchestrator/*' \
    -not -name 'config.json' \
    -not -name 'gemini_api_key' -not -name '*_api_key' \
    -not -name '.env' -not -name '.env.*' \
    -not -name '*.pem' -not -name '*.key' -not -name '*.p12' \
    -not -name '*.secret' \
    -print0 | while IFS= read -r -d '' f; do
      mkdir -p "$ENGINE_DEST/$(dirname "$f")"
      cp "$f" "$ENGINE_DEST/$f"
  done )
# Version stamp so the app re-copies the engine when it changes.
date -u +"%Y%m%d%H%M%S" > "$ENGINE_DEST/VERSION"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Orchestrator</string>
  <key>CFBundleDisplayName</key><string>Orchestrator</string>
  <key>CFBundleIdentifier</key><string>com.orchestrator.gui</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Orchestrator</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Ad-hoc sign so Gatekeeper lets it run locally.
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true

echo "[build_app] done: $APP"
echo "Open with:  open \"$APP\""
echo "Note: the engine is bundled inside the app and copied to"
echo "      ~/Library/Application Support/Orchestrator/engine on first launch."
echo "      Workspace defaults to ~/Documents/iOS-App-Factory (change it in Settings)."
echo "      Still needs python3, Xcode, and logged-in codex/claude/gemini CLIs."
