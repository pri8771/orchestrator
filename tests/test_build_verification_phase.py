import json
import os
import tempfile
import unittest

import orchestrator as orch
import workflows as wf


class TestBuildVerificationPhase(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app_dir, "app_build"))
        with open(os.path.join(self.app_dir, "app_build", "ok.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("ok\n")
        self._orig_call_agent = orch.call_agent
        self._orig_available = orch._agent_available

        orch._agent_available = lambda agent, cfg=None: True

        def fake_agent(_cfg, _app, _phase, _rnd, _agent, _prompt):
            return "We agree. CONSENSUS: YES\n## Final Output\nready to verify."

        orch.call_agent = fake_agent

    def tearDown(self):
        orch.call_agent = self._orig_call_agent
        orch._agent_available = self._orig_available

    def test_non_build_phase_runs_verification_against_existing_app_build(self):
        phase = wf.Phase(
            "build_verification", "build_verification", "build_verification.md",
            "verify it", rounds=1,
            verify={"type": "shell", "command": "test -f ok.txt",
                    "repair_iterations": 0},
            requires_verification=True,
        )
        cfg = {
            "agents": {"codex_enabled": True, "claude_enabled": False,
                       "gemini_enabled": False, "ollama_enabled": False},
            "runtime": {"build_code_changes_enabled": True,
                        "verify_build_enabled": True,
                        "phase_quality_gates_enabled": False},
            "ios": {"enforce_signing": False},
            "root": self.app_dir,
            "_workflow_target": "app",
            "_workflow_name": "app_build",
            "_agent_role_overrides": {},
        }
        state = {"prompt_hash": "H", "phase_outputs": {}, "consensus_status": {},
                 "vote_results": {}, "completed_phases": []}

        out = orch.process_phase(cfg, "demo", self.app_dir, phase,
                                 "Build a tiny app.", [], state)

        self.assertIn("VERIFICATION: VERIFIED", out)
        with open(os.path.join(self.app_dir, "verify_results.json"),
                  encoding="utf-8") as fh:
            records = json.load(fh)
        self.assertEqual(records[-1]["phase"], "build_verification")
        self.assertEqual(records[-1]["status"], "verified")


if __name__ == "__main__":
    unittest.main()
