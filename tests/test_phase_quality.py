import os
import tempfile
import unittest

import orchestrator as orch
import workflows as wf


class TestPhaseQualityGate(unittest.TestCase):
    def setUp(self):
        self._orig_call_agent = orch.call_agent

    def tearDown(self):
        orch.call_agent = self._orig_call_agent

    def test_quality_gate_forces_repair_round_and_independent_first(self):
        app_dir = tempfile.mkdtemp()
        phase = wf.Phase(
            "app_features", "app_features", "app_features.md",
            "Prioritize features.", rounds=2)
        cfg = {
            "agents": {
                "codex_enabled": True,
                "claude_enabled": True,
                "gemini_enabled": False,
                "ollama_enabled": False,
            },
            "runtime": {
                "phase_independent_first_round_enabled": True,
                "phase_quality_gates_enabled": True,
                "phase_quality_repair_rounds": 1,
            },
            "_workflow_target": "app",
        }
        state = orch.load_state(app_dir)
        prompts = []
        quality_calls = []

        def fake_call_agent(_cfg, _app, _phase, rnd, agent, prompt):
            prompts.append((_phase, str(rnd), agent, prompt))
            if "PHASE QUALITY EVALUATOR" in prompt:
                quality_calls.append(str(rnd))
                if len(quality_calls) == 1:
                    return "QUALITY: FAIL\n## Feedback\nMissing acceptance criteria."
                return "QUALITY: PASS\n## Feedback\nThe repaired output is concrete."
            if "wrapping up round" in prompt:
                if str(rnd) == "1":
                    return "CONSENSUS: YES\n## Final Output\nA vague feature list."
                return (
                    "CONSENSUS: YES\n## Final Output\nMust-have feature: guided "
                    "capture. User story: as a busy user I can capture one item "
                    "in under 20 seconds. Acceptance criteria: create, edit, "
                    "delete, and empty-state flows all work."
                )
            if str(rnd) == "1" and agent == "codex":
                return "FIRST_AGENT_ONLY_TOKEN"
            return "agent discussion"

        orch.call_agent = fake_call_agent
        out = orch.process_phase(
            cfg, "demo", app_dir, phase, "Build a useful iOS app.",
            [], state, phase_index=0)

        self.assertIn("guided capture", out)
        self.assertEqual(quality_calls, ["quality-1", "quality-2"])

        claude_round_one = [
            p for (_ph, rnd, agent, p) in prompts
            if rnd == "1" and agent == "claude" and "YOUR TURN" in p
        ]
        self.assertTrue(claude_round_one)
        self.assertNotIn("FIRST_AGENT_ONLY_TOKEN", claude_round_one[0])

        round_two_agent_prompts = [
            p for (_ph, rnd, _agent, p) in prompts
            if rnd == "2" and "YOUR TURN" in p
        ]
        self.assertTrue(any("QUALITY: FAIL" in p for p in round_two_agent_prompts))

        md_path = os.path.join(app_dir, "app_features", "app_features.md")
        with open(md_path, encoding="utf-8") as fh:
            transcript = fh.read()
        self.assertIn("Quality Gate", transcript)
        self.assertIn("requested another round", transcript)


if __name__ == "__main__":
    unittest.main()
