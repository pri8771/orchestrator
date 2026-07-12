#!/usr/bin/env bash
# Build (if needed) and launch the Orchestrator GUI from source.
#
#   bash orchestrator-v2-source/gui/run_gui.sh
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

echo "[run_gui] root=$ROOT"
echo "[run_gui] building (release)…"
cd "$GUI_DIR"
swift build -c release

BIN="$(swift build -c release --show-bin-path)/OrchestratorGUI"
echo "[run_gui] launching $BIN"
exec "$BIN"
