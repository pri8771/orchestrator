#!/usr/bin/env bash
# ============================================================================
# install_launch_agent.sh — install a macOS LaunchAgent that runs the
# orchestrator on a schedule using your logged-in CLIs (no API keys).
#
#   bash install_launch_agent.sh            # install + load (default: every 30m)
#   INTERVAL=900 bash install_launch_agent.sh   # custom interval (seconds)
#   bash install_launch_agent.sh uninstall  # stop + remove
#
# Logs go to .orchestrator/logs/launchagent.out/.err
# ============================================================================
set -uo pipefail

LABEL="${ORCH_LAUNCH_LABEL:-com.orchestrator.autonomous}"
# Derive the engine from this script's location. Project output goes to the
# workspace below (ORCH_ROOT env wins, else a portable home-relative default).
ORCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ORCH_ROOT:-$HOME/Documents/iOS-App-Factory}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
INTERVAL="${INTERVAL:-1800}"

if [ "${1:-}" = "uninstall" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Uninstalled $LABEL."
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$ORCH_DIR/logs" "$ROOT"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$ORCH_DIR/run.sh</string>
        <string>--once</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>StartInterval</key>
    <integer>$INTERVAL</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$ORCH_DIR/logs/launchagent.out</string>
    <key>StandardErrorPath</key>
    <string>$ORCH_DIR/logs/launchagent.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed and loaded $LABEL (every ${INTERVAL}s)."
echo "Status : launchctl list | grep $LABEL"
echo "Stop   : bash install_launch_agent.sh uninstall"
echo "NOTE   : The LaunchAgent inherits a minimal PATH. If a CLI lives elsewhere,"
echo "         add its directory to the PATH string in $PLIST and reload."
