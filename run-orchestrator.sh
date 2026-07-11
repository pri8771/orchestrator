#!/usr/bin/env bash
# Launch Orchestrator V2 (the macOS app) against the shared factory workspace.
#   bash run-orchestrator.sh
# Each direct child of /Users/pchordia/Documents/iOS-App-Factory is one project.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export ORCH_DIR="$REPO/orchestrator-v2-source"
export ORCH_ROOT="/Users/pchordia/Documents/iOS-App-Factory"
mkdir -p "$ORCH_ROOT"
GUI_DIR="$ORCH_DIR/gui"
cd "$GUI_DIR"
echo "[run] root=$ORCH_ROOT"
echo "[run] building (release)…"
swift build -c release
BIN="$(swift build -c release --show-bin-path)/OrchestratorGUI"
echo "[run] launching $BIN"
exec "$BIN"
