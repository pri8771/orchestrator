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


class TestForcedVoteSeam(SeamBase):
    """V3 board 2.2b: the two vote-seam values nothing else covers."""

    def test_no_tally_agent_keeps_the_recap_and_writes_no_vote_state(self):
        # Ballots unparseable AND every tally turn fails: final_output must
        # remain the coordinator's last recap (seeded IN param), the footer
        # is VOTE_DECISION: NO, and state gains NO vote bookkeeping.
        orch._agent_available = lambda a, cfg=None: a in ("codex", "claude")

        def sessioned(cfg, app, phase, rnd, agent, prompt,
                      delta_prompt=None, session_key=None):
            if (session_key or "").endswith(":coord"):
                return "Split decision — the recap stands. CONSENSUS: NO"
            return "%s position." % agent

        def dead_vote(cfg, app, phase, rnd, agent, prompt):
            raise orch.AgentError("no ballots today")
        orch.call_agent_sessioned = sessioned
        self._orig_call = orch.call_agent
        orch.call_agent = dead_vote
        try:
            cfg = self._cfg()
            cfg["agents"]["claude_enabled"] = True
            state = self._state()
            out = orch.process_phase(cfg, "seam", self.app_dir,
                                     self._phase(rounds=1), "p", [], state)
        finally:
            orch.call_agent = self._orig_call
        self.assertIn("the recap stands", out,
                      "the seeded recap must survive the no-tally path")
        # The distinguishing assertion: a lost seed would return "" and the
        # adoption fallback would REPLACE the recap — whose adopted text
        # happens to embed the transcript (and thus the recap words), so the
        # positive assertion alone cannot catch the drift.
        self.assertNotIn("No coordinator was reachable", out)
        with open(os.path.join(self.app_dir, "d.md"), encoding="utf-8") as fh:
            self.assertIn("VOTE_DECISION: NO", fh.read())
        self.assertNotIn(KEY, state.get("vote_results", {}))
        self.assertNotIn(KEY, state.get("phase_resolutions", {}))

    def test_direct_call_contract_threads_transcript_and_ballots(self):
        # The 3-tuple contract + transcript threading — the drift byte-green
        # cannot catch (append_md writes the .md independently).
        def ballots(cfg, app, phase, rnd, agent, prompt):
            other = "claude" if agent == "codex" else "codex"
            return ('```vote-json\n{"choice": "%s", "confidence": 4, '
                    '"reason": "better"}\n```' % other)
        self._orig_call = orch.call_agent
        orch.call_agent = ballots
        orch._agent_available = lambda a, cfg=None: a in ("codex", "claude")
        try:
            result = orch._run_forced_vote(
                self._cfg(), "seam", self.app_dir, self._phase(), "p", [],
                self._state(),
                md_path=os.path.join(self.app_dir, "d.md"), transcript="T0",
                unit="round", coord="codex",
                available_active=["codex", "claude"],
                last_substantive={"codex": "codex proposal",
                                  "claude": "claude proposal"},
                final_output="RECAP")
        finally:
            orch.call_agent = self._orig_call
        self.assertEqual(len(result), 3)
        final_output, transcript, vote = result
        self.assertTrue(transcript.startswith("T0"),
                        "prior transcript must be preserved")
        self.assertIn("— vote**", transcript,
                      "ballot blocks must thread through the RETURNED transcript")
        self.assertEqual(vote.get("method"), "ballots")
        self.assertIn("commits to", final_output)
