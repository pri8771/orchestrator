"""V3 board 2.2d: _run_roster_turns — the byte surfaces no fixture pins
(skip notes in both modes, chat PASS slip, the Iteration header variant)
plus the delta-offset semantics that ARE the ctx_transcript seam."""
import os
import tempfile
import unittest

import orchestrator as orch
import workflows as wf


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class RosterBase(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self._orig_sessioned = orch.call_agent_sessioned
        self._orig_avail = orch._agent_available
        self._orig_delta = orch._delta_discuss_prompt

    def tearDown(self):
        orch.call_agent_sessioned = self._orig_sessioned
        orch._agent_available = self._orig_avail
        orch._delta_discuss_prompt = self._orig_delta

    def _cfg(self, agents=("codex",), **runtime_over):
        runtime = {"parallel_discussion_rounds": False,
                   "phase_quality_gates_enabled": False,
                   "phase_independent_first_round_enabled": False,
                   "approval_timeout_seconds": 30}
        runtime.update(runtime_over)
        return {"agents": {"codex_enabled": "codex" in agents,
                           "claude_enabled": "claude" in agents,
                           "gemini_enabled": "gemini" in agents},
                "runtime": runtime,
                "_workflow_target": "app", "_app_dir": self.app_dir,
                "root": self.app_dir}

    def _state(self):
        return {"current_phase": None, "current_round": 0,
                "completed_phases": [], "phase_outputs": {},
                "consensus_status": {}, "vote_results": {}, "prompt_hash": "h"}

    def _md(self, name):
        with open(os.path.join(self.app_dir, name), encoding="utf-8") as fh:
            return fh.read()


class TestSkipNotes(RosterBase):
    def test_debate_skip_note_bytes(self):
        # Two agents; claude's turn raises — the exact skip-note block must
        # land, once, with the error text. No fixture covers this surface.
        orch._agent_available = lambda a, cfg=None: a in ("codex", "claude")

        def flaky(cfg, app, phase, rnd, agent, prompt,
                  delta_prompt=None, session_key=None):
            if agent == "claude":
                raise orch.AgentError("boom")
            if (session_key or "").endswith(":coord"):
                return "Done.\n\nCONSENSUS: YES\n\n## Final Output\n\nX.\n"
            return "codex take"
        orch.call_agent_sessioned = flaky
        orch.process_phase(self._cfg(agents=("codex", "claude")), "r",
                           self.app_dir,
                           wf.Phase("design_discussion", ".", "d.md", "p",
                                    rounds=2),
                           "p", [], self._state())
        text = self._md("d.md")
        self.assertIn("**Claude — Round 1 (skipped: CLI unavailable)**\n\n_boom_\n",
                      text)

    def test_conversational_skip_note_bytes(self):
        orch._agent_available = lambda a, cfg=None: a in ("codex", "claude")
        _write(os.path.join(self.app_dir, "human_inbox.txt"), "hello")

        def flaky(cfg, app, phase, rnd, agent, prompt,
                  delta_prompt=None, session_key=None):
            _write(os.path.join(self.app_dir, "approvals", "chat.ok"), "")
            if agent == "claude":
                raise orch.AgentError("logged out")
            return "codex chat reply"
        orch.call_agent_sessioned = flaky
        orch.process_phase(self._cfg(agents=("codex", "claude")), "r",
                           self.app_dir,
                           wf.Phase("chat", ".", "chat.md", "p", rounds=0,
                                    conversational=True),
                           "p", [], self._state())
        self.assertIn(
            "**Claude — Round 1 (skipped: CLI unavailable)**\n\n_logged out_\n",
            self._md("chat.md"))


class TestSequentialUnexpectedError(RosterBase):
    def test_non_agenterror_skips_the_turn_not_the_phase(self):
        # A-74: the sequential path (parallel_discussion_rounds=False — also
        # every single-agent round) caught only AgentError, so one unexpected
        # exception (session-file OSError, a prompt-builder bug) aborted the
        # whole phase — unlike the parallel branch's belt-and-suspenders
        # handler. It must degrade to the same skip note instead.
        orch._agent_available = lambda a, cfg=None: a in ("codex", "claude")

        def broken(cfg, app, phase, rnd, agent, prompt,
                   delta_prompt=None, session_key=None):
            if (session_key or "").endswith(":coord"):
                return "Done.\n\nCONSENSUS: YES\n\n## Final Output\n\nX.\n"
            if agent == "claude":
                raise OSError("session file vanished")
            return "codex take"
        orch.call_agent_sessioned = broken
        out = orch.process_phase(self._cfg(agents=("codex", "claude")), "r",
                                 self.app_dir,
                                 wf.Phase("design_discussion", ".", "d.md",
                                          "p", rounds=2),
                                 "p", [], self._state())
        text = self._md("d.md")
        self.assertIn("unexpected turn error: session file vanished", text)
        self.assertIn("codex take", text)   # the healthy agent still spoke
        self.assertIn("X.", out)            # and the phase completed
    def test_pass_slip_gated_on_round_two(self):
        # Round 1 PASS records as a normal block (the rnd > 1 gate); round 2
        # PASS renders the slip. Neither surface had chat coverage.
        orch._agent_available = lambda a, cfg=None: a == "codex"
        _write(os.path.join(self.app_dir, "human_inbox.txt"), "first")
        calls = {"n": 0}

        def passer(cfg, app, phase, rnd, agent, prompt,
                   delta_prompt=None, session_key=None):
            calls["n"] += 1
            if rnd == 1:
                _write(os.path.join(self.app_dir, "human_inbox.txt"), "second")
                return "PASS"
            _write(os.path.join(self.app_dir, "approvals", "chat.ok"), "")
            return "PASS"
        orch.call_agent_sessioned = passer
        orch.process_phase(self._cfg(), "r", self.app_dir,
                           wf.Phase("chat", ".", "chat.md", "p", rounds=0,
                                    conversational=True),
                           "p", [], self._state())
        text = self._md("chat.md")
        # Round 1: literal PASS as a normal block, no slip.
        r1 = text[text.index("### Round 1"):text.index("### Round 2")]
        self.assertIn("PASS", r1)
        self.assertNotIn("_PASS — nothing new to add._", r1)
        # Round 2: the slip.
        r2 = text[text.index("### Round 2"):]
        self.assertIn("_PASS — nothing new to add._", r2)


class TestDeltaOffsets(RosterBase):
    def test_independent_first_offsets_restart_and_recover(self):
        # Debate under independent_first: round 1's claude delta is measured
        # against "" (offset restarts at 0), so round 2's delta re-covers
        # round 1's blocks. This IS the ctx_transcript seam.
        orch._agent_available = lambda a, cfg=None: a == "claude"
        deltas = []

        def rec_delta(cfg, agent, new_transcript, rnd, extra="", persona=""):
            deltas.append((rnd, new_transcript))
            return self._orig_delta(cfg, agent, new_transcript, rnd,
                                    extra=extra, persona=persona)
        orch._delta_discuss_prompt = rec_delta

        def sessioned(cfg, app, phase, rnd, agent, prompt,
                      delta_prompt=None, session_key=None):
            if (session_key or "").endswith(":coord"):
                if rnd >= 2:
                    return "Done.\n\nCONSENSUS: YES\n\n## Final Output\n\nX.\n"
                return "go on. CONSENSUS: NO"
            return "claude take round %d" % rnd
        orch.call_agent_sessioned = sessioned
        cfg = self._cfg(agents=("claude",),
                        phase_independent_first_round_enabled=True)
        orch.process_phase(cfg, "r", self.app_dir,
                           wf.Phase("design_discussion", ".", "d.md", "p",
                                    rounds=3),
                           "p", [], self._state())
        r1 = [d for r, d in deltas if r == 1]
        r2 = [d for r, d in deltas if r == 2]
        self.assertEqual(r1, [""], "round-1 delta measured against the blank room")
        self.assertTrue(any("claude take round 1" in d for d in r2),
                        "round 2's delta must re-cover round 1's blocks")

    def test_conversational_delta_includes_drained_human_block(self):
        orch._agent_available = lambda a, cfg=None: a == "claude"
        deltas = []

        def rec_delta(cfg, agent, new_transcript, rnd, extra="", persona=""):
            deltas.append((rnd, new_transcript))
            return self._orig_delta(cfg, agent, new_transcript, rnd,
                                    extra=extra, persona=persona)
        orch._delta_discuss_prompt = rec_delta
        _write(os.path.join(self.app_dir, "human_inbox.txt"), "hi claude")

        def sessioned(cfg, app, phase, rnd, agent, prompt,
                      delta_prompt=None, session_key=None):
            _write(os.path.join(self.app_dir, "approvals", "chat.ok"), "")
            return "reply"
        orch.call_agent_sessioned = sessioned
        orch.process_phase(self._cfg(agents=("claude",)), "r", self.app_dir,
                           wf.Phase("chat", ".", "chat.md", "p", rounds=0,
                                    conversational=True),
                           "p", [], self._state())
        self.assertTrue(any("hi claude" in d for _r, d in deltas),
                        "the drained human block must ride the live-transcript delta")


class TestIterationHeader(RosterBase):
    def test_sequential_build_iteration_bytes(self):
        # A writes=True phase with code changes OFF runs the sequential loop
        # with Iteration labels — the one header variant no fixture covers.
        orch._agent_available = lambda a, cfg=None: a == "codex"

        def sessioned(cfg, app, phase, rnd, agent, prompt,
                      delta_prompt=None, session_key=None):
            if (session_key or "").endswith(":coord"):
                return "Built.\n\nCONSENSUS: YES\n\n## Final Output\n\nOK.\n"
            return "build step"
        orch.call_agent_sessioned = sessioned
        orch.process_phase(self._cfg(), "r", self.app_dir,
                           wf.Phase("build_coordination", ".", "b.md", "p",
                                    rounds=2, writes=True),
                           "p", [], self._state())
        text = self._md("b.md")
        self.assertIn("### Iteration 1", text)
        self.assertIn("— Iteration 1**", text)


if __name__ == "__main__":
    unittest.main()
