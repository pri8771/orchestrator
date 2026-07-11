#!/usr/bin/env bash
# ============================================================================
# run.sh — launch the autonomous orchestrator using ONLY your logged-in CLIs.
#
# This script intentionally UNSETS all pay-as-you-go API key environment
# variables so nothing in this run can incur extra API cost. It then runs the
# orchestrator, and (optionally) commits and pushes the resulting Markdown.
#
# Paths are derived from this script's own location for the engine, while project
# output goes to the shared factory workspace below.
#
# Usage:
#   bash run.sh                # one full scan/process pass + git commit/push
#   bash run.sh --doctor       # environment check only (no git actions)
#   bash run.sh --app myapp    # process only one app
#   bash run.sh --watch 300    # loop every 300s (no commit until you stop it)
# ============================================================================
set -uo pipefail

ORCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="/Users/pchordia/Documents/iOS-App-Factory"
mkdir -p "$ROOT"

# 1) Move into the root workspace.
cd "$ROOT" || { echo "Cannot cd into $ROOT"; exit 1; }

# 2) Unset all API-key / pay-as-you-go credential env vars (belt and braces).
unset OPENAI_API_KEY ANTHROPIC_API_KEY GEMINI_API_KEY GOOGLE_API_KEY \
      OPENAI_API_BASE OPENAI_BASE_URL ANTHROPIC_AUTH_TOKEN \
      ANTHROPIC_BASE_URL GOOGLE_APPLICATION_CREDENTIALS GOOGLE_GENAI_API_KEY \
      2>/dev/null || true

# 3) Run the orchestrator, passing through any extra args. --root wins over the
#    config default; a user-supplied --root in "$@" overrides ours (argparse
#    keeps the last occurrence).
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "python3 not found on PATH"; exit 1
fi
echo "[run.sh] Launching orchestrator with: $*"
"$PY" "$ORCH_DIR/orchestrator.py" --root "$ROOT" "$@"
ORCH_RC=$?
echo "[run.sh] Orchestrator exited with code $ORCH_RC"

# Skip git for doctor-only / watch runs.
case " $* " in
  *" --doctor "*) echo "[run.sh] doctor run — skipping git."; exit $ORCH_RC ;;
  *" --watch "*)  echo "[run.sh] watch run — skipping git.";  exit $ORCH_RC ;;
esac

# Honor runtime.commit_and_push (config.json overrides config.yaml): when set
# to false, skip the whole git step below.
CP_SRC="$ORCH_DIR/config.yaml"
[ -f "$ORCH_DIR/config.json" ] && CP_SRC="$ORCH_DIR/config.json"
if grep -Eq '"?commit_and_push"?["'\'' ]*:[ ]*false' "$CP_SRC" 2>/dev/null; then
  echo "[run.sh] runtime.commit_and_push=false — skipping git."
  exit $ORCH_RC
fi

# 4-6) Git: add, commit if there are staged changes, push if origin exists.
# Never stage secret-shaped files — a stray key file must not reach a remote.
if [ -d "$ROOT/.git" ]; then
  git -C "$ROOT" add -A
  SECRETS="$(git -C "$ROOT" diff --cached --name-only | \
    grep -E '(^|/)(gemini_api_key|config\.json|\.env[^/]*)$|\.(pem|key|p12)$' || true)"
  if [ -n "$SECRETS" ]; then
    echo "[run.sh] REFUSING to commit secret-named files:"
    echo "$SECRETS"
    while IFS= read -r f; do
      [ -n "$f" ] && git -C "$ROOT" reset -q HEAD -- "$f"
    done <<< "$SECRETS"
    echo "[run.sh] Unstaged them. Add these paths to $ROOT/.gitignore."
  fi
  if git -C "$ROOT" diff --cached --quiet; then
    echo "[run.sh] Nothing to commit."
  else
    git -C "$ROOT" commit -m "Run autonomous app orchestration" \
      && echo "[run.sh] Committed."
    if git -C "$ROOT" remote get-url origin >/dev/null 2>&1; then
      BRANCH="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
      if git -C "$ROOT" push origin "$BRANCH"; then
        echo "[run.sh] Pushed to origin/$BRANCH."
      else
        echo "[run.sh] Push failed (non-fatal). Check 'git remote -v' / auth."
      fi
    else
      echo "[run.sh] No 'origin' remote configured — skipping push."
    fi
  fi
else
  echo "[run.sh] $ROOT is not a git repo — skipping git steps."
  echo "[run.sh] To enable: (cd \"$ROOT\" && git init && git add -A && git commit -m init)"
fi

exit $ORCH_RC
