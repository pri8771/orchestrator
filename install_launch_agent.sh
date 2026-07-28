#!/usr/bin/env bash
# ============================================================================
# install_launch_agent.sh — install a macOS LaunchAgent that runs the
# orchestrator on a schedule using your logged-in CLIs (no API keys).
#
#   bash install_launch_agent.sh            # install + load (default: every 30m)
#   INTERVAL=900 bash install_launch_agent.sh   # custom interval (seconds)
#   bash install_launch_agent.sh uninstall  # stop + remove
#
# Logs go to <engine dir>/logs/launchagent.out/.err (the plist's StandardOutPath)
# ============================================================================
set -uo pipefail

# LaunchAgents are a macOS mechanism. On Linux, schedule run.sh with cron or a
# systemd timer instead.
if [ "$(uname)" != "Darwin" ]; then
  echo "install_launch_agent.sh installs a macOS LaunchAgent, but this is $(uname)." >&2
  echo "On Linux, schedule 'bash run.sh --once' with cron or a systemd timer." >&2
  exit 1
fi

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

# Plist <string>/<integer> bodies are raw XML character data: a path like
# "~/Documents/Apps & Tools/orchestrator" (or a label/interval carrying '<')
# interpolated verbatim yields an unparseable plist that launchctl rejects.
# '&' must be escaped FIRST so it can't double-escape the entities it creates;
# &, < and > are the full set XML requires inside element content. sed, not
# ${var//pat/rep}: bash 5.2's patsub_replacement expands '&' in the replacement
# to the match (turning &lt; into <lt;), while this script also targets 3.2 —
# sed's \& is the one spelling that is literal on every version.
xml_escape(){ printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
LABEL_XML=$(xml_escape "$LABEL")
ORCH_DIR_XML=$(xml_escape "$ORCH_DIR")
ROOT_XML=$(xml_escape "$ROOT")
INTERVAL_XML=$(xml_escape "$INTERVAL")

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL_XML</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$ORCH_DIR_XML/run.sh</string>
        <string>--once</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT_XML</string>
    <key>StartInterval</key>
    <integer>$INTERVAL_XML</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$ORCH_DIR_XML/logs/launchagent.out</string>
    <key>StandardErrorPath</key>
    <string>$ORCH_DIR_XML/logs/launchagent.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
PLIST

# Validate the generated XML BEFORE handing it to launchd: a bad plist must
# fail loudly here with plutil's parse diagnostic, not as launchctl weirdness.
plutil -lint "$PLIST" >/dev/null || { echo "install_launch_agent: generated plist failed plutil -lint: $PLIST" >&2; exit 1; }

launchctl unload "$PLIST" 2>/dev/null || true
# launchctl's own failure used to be swallowed — the script printed 'Installed
# and loaded' regardless of exit status. Surface it as the install failure it is.
launchctl load "$PLIST" || { echo "install_launch_agent: launchctl load failed for $PLIST" >&2; exit 1; }
echo "Installed and loaded $LABEL (every ${INTERVAL}s)."
echo "Status : launchctl list | grep $LABEL"
echo "Stop   : bash install_launch_agent.sh uninstall"
echo "NOTE   : The LaunchAgent inherits a minimal PATH. If a CLI lives elsewhere,"
echo "         add its directory to the PATH string in $PLIST and reload."
