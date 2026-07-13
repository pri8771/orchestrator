#!/usr/bin/env bash
# Build (if needed) and launch the Orchestrator GUI from source.
#
#   bash gui/run_gui.sh   # from the repo root
#
# Passes the factory workspace root + orchestrator dir to the app via env vars
# so it reads the same files orchestrator.py writes.
set -euo pipefail

GUI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCH_DIR="$(cd "$GUI_DIR/.." && pwd)"
# Every direct child folder here is one project. ORCH_ROOT env wins, else a
# portable home-relative default.
ROOT="${ORCH_ROOT:-$HOME/Documents/iOS-App-Factory}"
mkdir -p "$ROOT"

export ORCH_ROOT="$ROOT"
export ORCH_DIR="$ORCH_DIR"

# Package.swift requires .v14 (macOS 14 Sonoma); swift build on an older OS
# fails with a generic compiler error, not this. Fail fast with a clear message.
OS_MAJOR="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"
if [ -n "$OS_MAJOR" ] && [ "$OS_MAJOR" -lt 14 ]; then
  echo "[run_gui] ERROR: this GUI requires macOS 14 (Sonoma) or later;" >&2
  echo "          detected macOS $(sw_vers -productVersion)." >&2
  exit 1
fi

echo "[run_gui] root=$ROOT"
echo "[run_gui] building (release)…"
cd "$GUI_DIR"
swift build -c release

BIN="$(swift build -c release --show-bin-path)/OrchestratorGUI"
echo "[run_gui] launching $BIN"
exec "$BIN"
