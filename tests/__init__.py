import os

# Keep test runs quiet: engine emit() lines go only to logs/orchestrator.log,
# not the test terminal (orchestrator._quiet() honors this env var).
os.environ.setdefault("ORCH_QUIET", "1")
