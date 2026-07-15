import os
import tempfile

# Keep test runs quiet: engine emit() lines go only to logs/orchestrator.log,
# not the test terminal (orchestrator._quiet() honors this env var).
os.environ.setdefault("ORCH_QUIET", "1")

# Repo-hygiene guard: pipeline tests drive full runs whose run-finished path
# refreshes the fleet anti-pattern ledger at <here>/knowledge/anti_patterns.md
# — with the real engine HERE that mutates the repo's own tracked file on
# every test run. ORCH_LEDGER_DIR (honored inside fleetlearn.build_ledger)
# redirects every ledger write to a throwaway dir, and being an env var it
# also covers engines spawned as subprocesses. Tests that exercise
# build_ledger's real path pass their own tmp `here` AND may clear this var.
_LEDGER_SANDBOX = tempfile.mkdtemp(prefix="orch-test-ledger-")
os.environ.setdefault("ORCH_LEDGER_DIR", _LEDGER_SANDBOX)
