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

# Package.swift requires .v14 (macOS 14 Sonoma); swift build on an older OS
# fails with a generic compiler error, not this. Fail fast with a clear message.
OS_MAJOR="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"
if [ -n "$OS_MAJOR" ] && [ "$OS_MAJOR" -lt 14 ]; then
  echo "[build_app] ERROR: this GUI requires macOS 14 (Sonoma) or later;" >&2
  echo "            detected macOS $(sw_vers -productVersion)." >&2
  exit 1
fi

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
# NOTE: the -not -name filters below match by filename only, not full path —
# any file named e.g. config.yaml or *_api_key anywhere under $ENGINE_SRC is
# dropped, even in a subdirectory unrelated to the top-level ones this is
# meant for. That's the safer failure mode for the secret-shaped names (better
# to over-exclude a credential than ship it), and today's tree only has one
# config.yaml/config.json at the root, so it's a non-issue in practice. If the
# engine ever grows nested config.yaml/config.json files that should ship,
# switch those two filters to -not -path "./config.yaml" etc.
( cd "$ENGINE_SRC" && \
  find . -type f \
    -not -path './gui/*' -not -path './.git/*' \
    -not -path './logs/*' -not -path './locks/*' -not -path '*/__pycache__/*' \
    -not -path './tests/*' -not -path './sample-run/*' \
    -not -path './.orchestrator/*' \
    -not -name 'config.json' -not -name 'config.yaml' \
    -not -name 'gemini_api_key' -not -name '*_api_key' \
    -not -name '.env' -not -name '.env.*' \
    -not -name '*.pem' -not -name '*.key' -not -name '*.p12' \
    -not -name '*.secret' \
    -print0 | while IFS= read -r -d '' f; do
      mkdir -p "$ENGINE_DEST/$(dirname "$f")"
      cp "$f" "$ENGINE_DEST/$f"
  done )
# config.yaml is excluded from the generic copy above and handled separately:
# it's the file the GUI's Settings panel writes into (root path, model roster,
# reasoning effort, etc.), so the BUILDER's live working-tree copy can carry
# personal customization. Ship the clean, git-tracked version instead so a
# distributed .app/DMG never bakes in whoever built it's local settings.
if git -C "$ENGINE_SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    && git -C "$ENGINE_SRC" show HEAD:config.yaml > "$ENGINE_DEST/config.yaml" 2>/dev/null; then
  echo "[build_app] bundled config.yaml from git HEAD (not the working-tree copy)."
else
  echo "[build_app] WARNING: could not read config.yaml from git HEAD — falling"
  echo "            back to the working-tree copy, which may carry local settings."
  cp "$ENGINE_SRC/config.yaml" "$ENGINE_DEST/config.yaml"
fi
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
  <key>LSMinimumSystemVersion</key><string>14.0</string>
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
