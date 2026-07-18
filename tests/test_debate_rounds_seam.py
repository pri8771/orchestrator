"""V3 board 2.2a: the _run_debate_rounds seam — the four values that cross
it with no other coverage (any_agent_output init, guard wiring, streak
break/recovery, and the zero-iteration parallel-build echo)."""
import os
import tempfile
import unittest

import orchestrator as orch
import workflows as wf

KEY = "design_discussion"


class SeamBase(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self._orig_sessioned = orch.call_agent_sessioned
        self._orig_avail = orch._agent_available
        orch._agent_available = lambda a, cfg=None: a == "codex"

    def tearDown(self):
        orch.call_agent_sessioned = self._orig_sessioned
        orch._agent_available = self._orig_avail

    def _cfg(self):
        return {"agents": {"codex_enabled": True, "claude_enabled": False,
                           "gemini_enabled": False},
                "runtime": {"parallel_discussion_rounds": False,
                            "phase_quality_gates_enabled": False,
                            "phase_independent_first_round_enabled": False},
                "_workflow_target": "app", "_app_dir": self.app_dir,
                "root": self.app_dir}

    def _phase(self, rounds=5):
        return wf.Phase(KEY, ".", "d.md", "decide", rounds=rounds)

    def _state(self, **over):
        st = {"current_phase": None, "current_round": 0,
              "completed_phases": [], "phase_outputs": {},
              "consensus_status": {}, "vote_results": {}, "prompt_hash": "h"}
        st.update(over)
        return st


class TestDebateRoundsSeam(SeamBase):
    def test_all_agents_down_fresh_phase_raises(self):
        # any_agent_output=False must cross the seam and fire the guard
        # (after the empty-round streak breaks the loop at 3).
        def dead(cfg, app, phase, rnd, agent, prompt,
                 delta_prompt=None, session_key=None):
            raise orch.AgentError("CLI down")
        orch.call_agent_sessioned = dead
        with self.assertRaisesRegex(orch.AgentError,
                                    "No enabled agent could produce output"):
            orch.process_phase(self._cfg(), "seam", self.app_dir, self._phase(),
                               "p", [], self._state())

    def test_all_agents_down_resumed_phase_does_not_raise(self):
        # A resumed phase has real output on disk: any_agent_output starts
        # as `resuming`, so the guard must NOT fire — the recovered
        # transcript is adopted as the working decision instead.
        existing = ("# seam — Design Discussion\n\n## Transcript\n\n"
                    "\n### Round 1\n\n"
                    "**Codex — Round 1**\n\nrecovered position\n"
                    "\n**Coordinator (Codex) — decision after round 1**\n\n"
                    "CONSENSUS: NO\n")
        with open(os.path.join(self.app_dir, "d.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(existing)

        def dead(cfg, app, phase, rnd, agent, prompt,
                 delta_prompt=None, session_key=None):
            raise orch.AgentError("CLI down")
        orch.call_agent_sessioned = dead
        out = orch.process_phase(self._cfg(), "seam", self.app_dir,
                                 self._phase(),
                                 "p", [], self._state(current_phase=KEY,
                                                      current_round=2))
        self.assertIn("recovered position", out,
                      "the recovered transcript stands as the decision")

    def test_agents_recover_after_empty_rounds(self):
        # Two dead rounds, then recovery: the streak resets and the phase
        # completes on real output — resilience must survive the seam.
        def flaky(cfg, app, phase, rnd, agent, prompt,
                  delta_prompt=None, session_key=None):
            if rnd < 3:
                raise orch.AgentError("cooldown")
            if (session_key or "").endswith(":coord"):
                return ("Back online, converged.\n\nCONSENSUS: YES\n\n"
                        "## Final Output\n\nRecovered decision.\n")
            return "recovered take, round %d" % rnd
        orch.call_agent_sessioned = flaky
        out = orch.process_phase(self._cfg(), "seam", self.app_dir,
                                 self._phase(rounds=5), "p", [], self._state())
        self.assertIn("Recovered decision", out)

    def test_zero_iteration_echoes_seeds_verbatim(self):
        # The parallel-build path calls the loop with rounds_iter=[] and its
        # already-computed results as seeds — the function must echo them
        # untouched, with the exact return arity/order.
        result = orch._run_debate_rounds(
            self._cfg(), "seam", self.app_dir, self._phase(), "p", [],
            self._state(),
            md_path=os.path.join(self.app_dir, "d.md"), transcript="t",
            extra="", personas={}, active=["codex"], coord="codex",
            rounds_iter=[], resuming=False, unit="round", is_build=True,
            is_verify_repair=False, unlimited_rounds=False, max_rounds=3,
            independent_first=False, quality_repair_limit=1,
            step_in_marker=os.path.join(self.app_dir, ".step_in"),
            consensus=True, final_output="sentinel")
        self.assertEqual(result, (True, "sentinel", "t", {}, False))


if __name__ == "__main__":
    unittest.main()
