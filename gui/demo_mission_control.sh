#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 WORKSPACE_ROOT" >&2
  exit 2
fi

workspace=$1
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$script_dir/.." && pwd)
fixture=$script_dir/Tests/Fixtures/MissionControl
recorded=$repo/tests/conductor_evals/fixtures/happy_pipeline/stream.jsonl

if [ ! -f "$recorded" ]; then
  echo "7.9 recorded stream fixture is missing: $recorded" >&2
  exit 1
fi
if [ -e "$workspace/.conductor" ]; then
  echo "refusing to overwrite existing $workspace/.conductor" >&2
  exit 1
fi

mkdir -p "$workspace/.conductor/approvals" \
  "$workspace/mission-demo/ideas/brainstorm" \
  "$workspace/mission-demo/research/brief"
cp "$fixture/conductor_state.json" "$workspace/.conductor/conductor_state.json"
cp "$fixture/conductor_ledger.jsonl" "$workspace/.conductor/conductor_ledger.jsonl"
cp "$fixture/pending.json" \
  "$workspace/.conductor/approvals/2222222222222222.pending"
cp "$fixture/costs.jsonl" \
  "$workspace/mission-demo/ideas/brainstorm/costs.jsonl"
cp "$fixture/events.jsonl" \
  "$workspace/mission-demo/ideas/brainstorm/events.jsonl"

echo "Mission Control demo seeded in $workspace"
echo "Open that workspace in the GUI and choose Conductor."
