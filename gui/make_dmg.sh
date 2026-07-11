#!/usr/bin/env bash
# Build Orchestrator.app and wrap it in a distributable DMG.
#
#   bash orchestrator-v2-source/gui/make_dmg.sh
#
# Produces .../gui/dist/Orchestrator.dmg with the app + an Applications
# symlink, so installing is the usual "drag to Applications" gesture.
#
# The DMG is self-contained for the GUI + engine, but on the target Mac it
# still needs: python3, Xcode (for simulator builds), and logged-in codex /
# claude / gemini CLIs. It is unsigned + ad-hoc-signed, so first launch is
# right-click ▸ Open (or `xattr -dr com.apple.quarantine` on the .app).
set -euo pipefail

GUI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$GUI_DIR/dist"
APP="$DIST/Orchestrator.app"
DMG="$DIST/Orchestrator.dmg"
STAGE="$DIST/dmg-stage"

# 1. Build the app (bundles the engine).
bash "$GUI_DIR/build_app.sh"

echo "[make_dmg] staging…"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

echo "[make_dmg] creating DMG…"
hdiutil create -volname "Orchestrator" \
  -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null

rm -rf "$STAGE"
echo "[make_dmg] done: $DMG"
echo "Install:  open \"$DMG\"  → drag Orchestrator to Applications."
echo "First launch: right-click Orchestrator ▸ Open (unsigned app)."
